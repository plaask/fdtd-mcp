# -*- coding: utf-8 -*-
"""Static guard: never depend on LSF commands the engine does not have.

The ::model parameter bug had three enablers that all agreed with each other
and disagreed with the engine:

  * bridge.py called ``addvar`` (removed in v202),
  * tests/fake_engine.py emulated it, so the suite stayed green,
  * server.py's advisory allowlist blessed it, so scripts using it raised no
    warning.

``fdtd_mcp/cheatsheet/lumapi_ref.json`` records what was verified against a real
engine (entries whose signature says "NOT AVAILABLE");
``fdtd_mcp/engine/capabilities.py`` is the machine-readable copy the bridge uses
at runtime. These tests make both load-bearing:

  1. the two records agree,
  2. no bridge module calls an unavailable command,
  3. no inline ``eval`` snippet uses one either,
  4. the server's allowlist does not bless one.
"""
import ast
import json
import re
from pathlib import Path

from fdtd_mcp.engine.capabilities import DEAD_COMMANDS

PKG = Path(__file__).resolve().parents[1] / 'fdtd_mcp'

# Every module that may talk to the engine.
BRIDGE_SOURCES = [PKG / 'bridge.py']
for _sub in ('engine', 'handlers'):
    _d = PKG / _sub
    if _d.is_dir():
        BRIDGE_SOURCES.extend(sorted(_d.glob('*.py')))


def _cheatsheet():
    with open(PKG / 'cheatsheet' / 'lumapi_ref.json', 'r', encoding='utf-8') as f:
        return json.load(f)


def _unavailable_commands():
    """LSF commands the cheatsheet marks as gone (signature says NOT AVAILABLE)."""
    return {name for name, entry in _cheatsheet().items()
            if name != 'meta'
            and 'NOT AVAILABLE' in str(entry.get('signature', '')).upper()}


def _bridge_trees():
    return [(path, ast.parse(path.read_text(encoding='utf-8')))
            for path in BRIDGE_SOURCES]


def _called_via_helper(tree):
    """String literals passed as the function name to _call_lsf/appCall."""
    names = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        fname = (func.attr if isinstance(func, ast.Attribute)
                 else func.id if isinstance(func, ast.Name) else '')
        if fname not in ('_call_lsf', 'appCall'):
            continue
        if node.args and isinstance(node.args[0], ast.Constant) \
                and isinstance(node.args[0].value, str):
            names.add(node.args[0].value)
    return names


def _eval_snippets(tree):
    """Code strings handed to <obj>.eval(...) — scanned for command names."""
    snippets = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == 'eval' \
                and node.args and isinstance(node.args[0], ast.Constant) \
                and isinstance(node.args[0].value, str):
            snippets.append(node.args[0].value)
    return snippets


def _server_allowlist():
    """Extract the _LSF_BUILTINS set literal from server.py without importing it."""
    tree = ast.parse((PKG / 'server.py').read_text(encoding='utf-8'))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == '_LSF_BUILTINS'
                for t in node.targets):
            return {el.value for el in node.value.args[0].elts}
    raise AssertionError('_LSF_BUILTINS not found in server.py')


def test_cheatsheet_flags_the_removed_v202_commands():
    # Sanity: the guard is only meaningful while the cheatsheet records them.
    assert {'addvar', 'setvar', 'getvar'} <= _unavailable_commands()


def test_runtime_and_cheatsheet_capability_records_agree():
    """capabilities.DEAD_COMMANDS is exactly what the cheatsheet calls dead."""
    assert set(DEAD_COMMANDS) == _unavailable_commands()


def test_bridge_does_not_call_unavailable_commands():
    offenders = {}
    dead = _unavailable_commands()
    for path, tree in _bridge_trees():
        hit = sorted(_called_via_helper(tree) & dead)
        if hit:
            offenders[path.name] = hit
    assert not offenders, (
        'bridge code calls LSF command(s) the engine does not have: %s. '
        'See fdtd_mcp/cheatsheet/lumapi_ref.json for the working replacement.'
        % offenders)


def test_bridge_eval_snippets_avoid_unavailable_commands():
    offenders = set()
    dead = _unavailable_commands()
    for _path, tree in _bridge_trees():
        for snippet in _eval_snippets(tree):
            for name in re.findall(r'\b(\w+)\s*\(', snippet):
                if name in dead:
                    offenders.add(name)
    assert not offenders, (
        'bridge eval() snippet(s) use unavailable commands: '
        + ', '.join(sorted(offenders)))


def test_server_allowlist_does_not_bless_unavailable_commands():
    offenders = sorted(_server_allowlist() & _unavailable_commands())
    assert not offenders, (
        '_LSF_BUILTINS allowlists command(s) that do not exist, so the '
        'anti-hallucination warning stays silent for them: '
        + ', '.join(offenders))
