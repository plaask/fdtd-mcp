# -*- coding: utf-8 -*-
"""The `?` -> write(num2str(...)) rewrite used by execute(capture=true)."""
from fdtd_mcp.handlers.engine_tools import _rewrite_query_lines


def test_rewrite_query_lines():
    code = 'a = 3;\n?num2str(a);\n# ?not a query\ns = "?not";\n  ? a + 1;\n'
    script, count = _rewrite_query_lines(code, 'C:/tmp/cap.txt')
    assert count == 2
    assert 'write("C:/tmp/cap.txt", num2str(num2str(a)));' in script
    assert 'write("C:/tmp/cap.txt", num2str(a + 1));' in script
    assert '# ?not a query' in script
    assert 's = "?not";' in script
    assert 'a = 3;' in script


def test_rewrite_query_lines_ignores_empty_marker():
    script, count = _rewrite_query_lines('?;\n?   # comment\n', 'C:/tmp/cap.txt')
    assert count == 0
    assert 'write(' not in script
