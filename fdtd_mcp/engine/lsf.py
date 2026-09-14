# -*- coding: utf-8 -*-
"""LSF text primitives: quoting, temp-variable hygiene, `?expr` parsing.

Runs on Lumerical's bundled Python 3.6.8 (no f-strings, no type hints).

Why this module exists
----------------------
Every LSF snippet this bridge builds is assembled as text, so two failure modes
were scattered across the code base:

  * object/monitor names interpolated raw (a name containing a quote produced a
    broken script and a confusing "Failed to evaluate code"),
  * engine-global scratch variables with fixed names (`brd`, `brrl`) that
    clobbered same-named user variables and leaked on error paths.

`lsf_quote()` and `TempVar` are the single place those are handled now; the
`?expr` parser lives here too so its (previously fragile) rules are testable
without an engine.
"""
from __future__ import print_function
import os
import re

# LSF identifiers may not start with '_' (v202 rejects those), so temp names use
# a letter prefix plus a per-process/per-call suffix.
_TEMP_COUNTER = [0]

_QUOTED = re.compile(r'^"(?:[^"\\]|\\.)*"$|^\'(?:[^\'\\]|\\.)*\'$')
_NUMBER = re.compile(r'^[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?$')


def lsf_quote(value):
    """Return `value` as a safe LSF string literal (including the quotes).

    Backslashes are doubled because LSF treats them as escapes (verified: paths
    must be written as "D:\\\\dir\\\\file.txt"), and embedded quotes are
    backslash-escaped.
    """
    if isinstance(value, bytes):
        value = value.decode('utf-8', 'replace')
    text = value if isinstance(value, str) else str(value)
    text = text.replace('\\', '\\\\').replace('"', '\\"')
    return '"' + text + '"'


def lsf_expr(value):
    """Render a Python value as an LSF literal (numbers/bools as-is, else quoted)."""
    if isinstance(value, bool):
        return '1' if value else '0'
    if isinstance(value, (int, float)):
        return repr(value)
    return lsf_quote(value)


class TempVar(object):
    """Context manager handing out a collision-free LSF variable name.

    The variable lives in the engine session's global namespace for the duration
    of the `with` block and is cleared on exit, including when the body raises.
    """

    def __init__(self, fsp, hint='br'):
        _TEMP_COUNTER[0] += 1
        self.name = '%s%d_%d' % (hint, os.getpid() % 100000, _TEMP_COUNTER[0])
        self._fsp = fsp

    def __enter__(self):
        return self.name

    def __exit__(self, exc_type, exc, tb):
        try:
            self._fsp.eval('clear(%s);' % self.name)
        except Exception:
            pass
        return False


def split_args(raw):
    """Split an LSF argument list at top level (quote- and bracket-aware).

    Handles nested parentheses/brackets/braces and commas inside strings, so
    `getresult("mon","E")`, `foo(bar(1,2), [1,2,3])` and `set("a,b", 1)` all
    split correctly.
    """
    args = []
    cur = []
    depth = 0
    quote = None
    i = 0
    while i < len(raw):
        ch = raw[i]
        if quote:
            cur.append(ch)
            if ch == '\\' and i + 1 < len(raw):
                cur.append(raw[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in '"\'':
            quote = ch
            cur.append(ch)
        elif ch in '([{':
            depth += 1
            cur.append(ch)
        elif ch in ')]}':
            depth -= 1
            cur.append(ch)
        elif ch == ',' and depth == 0:
            args.append(''.join(cur).strip())
            cur = []
        else:
            cur.append(ch)
        i += 1
    tail = ''.join(cur).strip()
    if tail:
        args.append(tail)
    return [a for a in args if a != '']


def literal(token):
    """Convert one argument token into the Python value to hand to appCall.

    Quoted tokens become strings (escapes resolved), numeric tokens become
    numbers, and anything else is passed through as its raw text — bare words
    stay strings, which is what `getnamed("FDTD", x span)`-style calls need.
    """
    if _QUOTED.match(token):
        body = token[1:-1]
        # Exact inverse of lsf_quote(): a doubled backslash is one literal
        # backslash (LSF escapes every backslash, not just path separators).
        out = []
        i = 0
        while i < len(body):
            ch = body[i]
            if ch == '\\' and i + 1 < len(body) and body[i + 1] in '\\"\'':
                out.append(body[i + 1])
                i += 2
                continue
            out.append(ch)
            i += 1
        return ''.join(out)
    if _NUMBER.match(token):
        try:
            return int(token)
        except ValueError:
            return float(token)
    if token in ('true', 'false'):
        return token == 'true'
    return token


def parse_query(expr):
    """Parse a `?expr` query.

    Returns ('call', (func_name, [args])) for `name(...)`, or None when the
    expression is not a call (a bare variable/name lookup). Zero-argument calls
    such as `listresults()` are supported, unlike the earlier regex which
    required at least one character between the parentheses.
    """
    expr = expr.strip()
    m = re.match(r'^([A-Za-z_]\w*)\s*\(', expr)
    if not m:
        return None
    name = m.group(1)
    # The closing paren must be the final character (balanced, nothing after).
    depth = 0
    quote = None
    for i in range(m.end() - 1, len(expr)):
        ch = expr[i]
        if quote:
            if ch == '\\':
                continue
            if ch == quote:
                quote = None
            continue
        if ch in '"\'':
            quote = ch
        elif ch == '(':
            depth += 1
        elif ch == ')':
            depth -= 1
            if depth == 0:
                if expr[i + 1:].strip():
                    return None
                raw = expr[m.end():i]
                return ('call', (name, [literal(a) for a in split_args(raw)]))
    return None


def strip_comments_and_strings(code):
    """Blank out LSF comments and string literals, preserving offsets.

    Verified live on v202: `#` starts a comment (a script whose statement is
    preceded by `# text` runs fine), while `%` does NOT — a `%` line makes the
    whole script fail with 'Failed to evaluate code'. `%` is blanked here too
    because the only consumer is a side-effect scan ("does this snippet call
    run()?"), and a `%` line is never valid code.
    """
    out = []
    quote = None
    i = 0
    while i < len(code):
        ch = code[i]
        if quote:
            if ch == '\\' and i + 1 < len(code):
                out.append(' ')
                out.append(' ')
                i += 2
                continue
            out.append('\n' if ch == '\n' else ' ')
            if ch == quote:
                quote = None
        elif ch in '"\'':
            quote = ch
            out.append(' ')
        elif ch == '#' or ch == '%':
            while i < len(code) and code[i] != '\n':
                out.append(' ')
                i += 1
            continue
        else:
            out.append(ch)
        i += 1
    return ''.join(out)


def percent_comment_lines(code):
    """1-based line numbers of lines that start with `%` — invalid LSF on v202.

    Returns [] when the script uses no `%` line comments. Callers turn this into
    an advisory warning, because the engine's own complaint is the unhelpful
    'Failed to evaluate code'.
    """
    hits = []
    for number, line in enumerate(code.split('\n'), 1):
        if line.strip().startswith('%'):
            hits.append(number)
    return hits
