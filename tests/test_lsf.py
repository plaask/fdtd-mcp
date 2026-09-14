# -*- coding: utf-8 -*-
"""Unit tests for the LSF text primitives (no engine required).

These cover the three failure modes that used to be scattered through bridge.py:
raw string interpolation, fixed-name engine scratch variables, and the fragile
`?expr` regex.
"""
from fdtd_mcp.engine.lsf import (TempVar, literal, lsf_expr, lsf_quote,
                                 parse_query, percent_comment_lines, split_args,
                                 strip_comments_and_strings)


# ---------------------------------------------------------------------------
# quoting
# ---------------------------------------------------------------------------

def test_lsf_quote_escapes_quotes_and_backslashes():
    assert lsf_quote('plain') == '"plain"'
    assert lsf_quote('a"b') == '"a\\"b"'
    # LSF treats backslash as an escape, so Windows paths must be doubled.
    assert lsf_quote('D:\\tmp\\x.txt') == '"D:\\\\tmp\\\\x.txt"'


def test_lsf_quote_roundtrip_through_literal():
    for text in ['::model::substrate', 'a"b', 'D:\\data\\nk.txt', 'x span']:
        assert literal(lsf_quote(text)) == text


def test_lsf_expr_renders_scalars():
    assert lsf_expr(1.5) == '1.5'
    assert lsf_expr(3) == '3'
    assert lsf_expr(True) == '1'
    assert lsf_expr(False) == '0'
    assert lsf_expr('DFT') == '"DFT"'


# ---------------------------------------------------------------------------
# argument splitting / literals
# ---------------------------------------------------------------------------

def test_split_args_is_quote_and_bracket_aware():
    assert split_args('"a", "b"') == ['"a"', '"b"']
    assert split_args('a, b(c,d), "e,f"') == ['a', 'b(c,d)', '"e,f"']
    assert split_args('[1,2,3], 4') == ['[1,2,3]', '4']
    assert split_args('') == []


def test_parse_query_accepts_zero_arg_calls():
    # The old regex required at least one character between the parens.
    assert parse_query('listresults()') == ('call', ('listresults', []))
    assert parse_query('getresult( )') == ('call', ('getresult', []))


def test_parse_query_splits_spaced_and_nested_args():
    assert parse_query('getnamed("::model","gap")') == (
        'call', ('getnamed', ['::model', 'gap']))
    # A space inside an unquoted argument no longer splits it in two.
    assert parse_query('getnamed("FDTD", x span)') == (
        'call', ('getnamed', ['FDTD', 'x span']))
    assert parse_query('foo(bar(1,2), [1,2])') == (
        'call', ('foo', ['bar(1,2)', '[1,2]']))


def test_parse_query_numbers_and_bare_names():
    assert parse_query('f(1, 2.5e-7, -3)') == ('call', ('f', [1, 2.5e-07, -3]))
    # A bare name is not a call — the caller falls back to getv/getnamed.
    assert parse_query('gap') is None
    assert parse_query('::model::gap') is None
    # Trailing text after the closing paren is not a call either.
    assert parse_query('f(1); g(2)') is None
    assert parse_query('f(1) extra') is None


def test_literal_keeps_unknown_tokens_as_text():
    assert literal('"quoted"') == 'quoted'
    assert literal('DFT') == 'DFT'
    assert literal('12') == 12
    assert literal('1.5') == 1.5


# ---------------------------------------------------------------------------
# comment/string stripping (side-effect scanning)
# ---------------------------------------------------------------------------

def test_strip_comments_hides_run_in_comments():
    # '#' is the LSF comment character on v202 (verified live: a script whose
    # statement is preceded by a '#' line runs; a '%' line makes it fail).
    code = '# run this later\naddrect();'
    assert 'run' not in strip_comments_and_strings(code)
    assert 'addrect' in strip_comments_and_strings(code)
    # '%' is invalid syntax rather than a comment, but the side-effect scan
    # stays quiet about its content too.
    assert 'run' not in strip_comments_and_strings('% run this later\naddrect();')


def test_percent_comment_lines_detects_invalid_syntax():
    assert percent_comment_lines('# fine\nx = 1;') == []
    assert percent_comment_lines('% nope\nx = 1;') == [1]
    assert percent_comment_lines('x = 1;\n   % indented\n') == [2]


def test_strip_strings_hides_parentheses():
    stripped = strip_comments_and_strings('set("x span (um)", 1);')
    assert '(' not in stripped.split('set')[0]
    assert 'set' in stripped
    assert stripped.count('(') == 1  # only the real call parenthesis


def test_strip_preserves_length_and_newlines():
    code = 'a = "x";\n% comment\nb = 2;'
    out = strip_comments_and_strings(code)
    assert len(out) == len(code)
    assert out.split('\n')[1].strip() == ''


# ---------------------------------------------------------------------------
# temp variable hygiene
# ---------------------------------------------------------------------------

class _FakeFsp(object):
    def __init__(self):
        self.evals = []

    def eval(self, code):
        self.evals.append(code)


def test_temp_var_unique_and_cleared():
    fsp = _FakeFsp()
    with TempVar(fsp, 'brd') as name:
        assert name.startswith('brd')
        assert not name.startswith('_')       # v202 rejects leading underscores
    assert fsp.evals == ['clear(%s);' % name]


def test_temp_var_names_do_not_collide():
    fsp = _FakeFsp()
    with TempVar(fsp, 'brd') as a:
        with TempVar(fsp, 'brd') as b:
            assert a != b


def test_temp_var_cleared_even_on_error():
    fsp = _FakeFsp()
    try:
        with TempVar(fsp, 'brd'):
            raise ValueError('boom')
    except ValueError:
        pass
    assert len(fsp.evals) == 1 and fsp.evals[0].startswith('clear(')
