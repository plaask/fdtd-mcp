# -*- coding: utf-8 -*-
"""Guard against server<->bridge dispatch drift.

The 6 dead tools bug happened because server.py's method_map and bridge.py's
_method_map were two hand-written tables that could disagree. dispatch.json is
now the single source; these tests enforce that:
  1. every MCP tool in server.py TOOLS has a dispatch.json entry,
  2. dispatch.json has no entries for tools that don't exist,
  3. every bridge method dispatch.json references has a _cmd_<method> handler.

Both files are parsed with ast so the tests run without lumapi, mcp, or a
Lumerical installation.
"""
import ast
import json
from pathlib import Path

PKG = Path(__file__).resolve().parents[1] / 'fdtd_mcp'


def _load_dispatch():
    with open(PKG / 'dispatch.json', 'r', encoding='utf-8') as f:
        raw = json.load(f)
    return {
        'server_only': frozenset(raw.get('_server_only', [])),
        'hidden': frozenset(raw.get('_hidden', [])),
        'dispatch': {k: v for k, v in raw.items()
                     if k not in ('_server_only', '_hidden')},
    }


def _tool_names_from_server():
    src = (PKG / 'server.py').read_text(encoding='utf-8')
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == 'TOOLS'
                for t in node.targets):
            names = []
            assert isinstance(node.value, ast.List), 'TOOLS is not a list'
            for item in node.value.elts:
                for kw in item.keywords:
                    if kw.arg == 'name' and isinstance(kw.value, ast.Constant):
                        names.append(kw.value.value)
            return names
    raise AssertionError('TOOLS list not found in server.py')


def _bridge_handlers():
    src = (PKG / 'bridge.py').read_text(encoding='utf-8')
    tree = ast.parse(src)
    return {n.name for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name.startswith('_cmd_')}


def test_every_tool_has_dispatch_entry():
    data = _load_dispatch()
    tools = _tool_names_from_server()
    missing = (set(tools) - data['hidden'] - data['server_only']
               - set(data['dispatch'].keys()))
    assert not missing, (
        'Tools missing from dispatch.json: ' + ', '.join(sorted(missing)))


def test_dispatch_has_no_unknown_tools():
    data = _load_dispatch()
    tools = set(_tool_names_from_server())
    extra = ((set(data['dispatch'].keys()) | data['server_only'])
             - tools - data['hidden'])
    assert not extra, (
        'dispatch.json has unknown tools: ' + ', '.join(sorted(extra)))


def test_every_dispatch_method_has_bridge_handler():
    data = _load_dispatch()
    handlers = _bridge_handlers()
    for tool, method in data['dispatch'].items():
        assert '_cmd_' + method in handlers, (
            'dispatch.json maps %s -> %s but bridge.py has no _cmd_%s'
            % (tool, method, method))


def test_tool_and_dispatch_counts_agree():
    data = _load_dispatch()
    tools = set(_tool_names_from_server()) - data['hidden']
    assert len(tools) == len(data['dispatch']) + len(data['server_only']) - len(data['hidden']), (
        '%d visible tools but %d dispatch entries + %d server-only - %d hidden in '
        'dispatch.json' % (len(tools), len(data['dispatch']),
                           len(data['server_only']), len(data['hidden'])))
