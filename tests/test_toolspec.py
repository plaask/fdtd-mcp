# -*- coding: utf-8 -*-
"""The declarative tool catalogue is the single source for the MCP surface.

These tests keep toolspec.py honest: unique names, schemas that agree with their
declared defaults, annotations that cannot contradict each other, and a
tool -> bridge method mapping that matches dispatch.json exactly (the same file
the bridge derives its handler table from).
"""
import json
from pathlib import Path

from fdtd_mcp.toolspec import SPECS, spec_by_name, visible_specs

PKG = Path(__file__).resolve().parents[1] / 'fdtd_mcp'
MODULES = {'session', 'model', 'material', 'attribute', 'sweep', 'result',
           'engine', 'library'}


def _dispatch():
    with open(PKG / 'dispatch.json', 'r', encoding='utf-8') as f:
        return json.load(f)


def test_names_are_unique_and_modules_known():
    names = [s.name for s in SPECS]
    assert len(names) == len(set(names)), 'duplicate tool name in SPECS'
    for spec in SPECS:
        assert spec.module in MODULES, '%s has module %r' % (spec.name, spec.module)


def test_schemas_are_well_formed():
    for spec in SPECS:
        assert spec.description, spec.name
        schema = spec.schema
        assert schema.get('type') == 'object', spec.name
        props = schema.get('properties', {})
        for required in schema.get('required', []):
            assert required in props, '%s: required %r missing from properties' % (
                spec.name, required)
        for key in spec.defaults:
            assert key in props, '%s: default for undeclared param %r' % (
                spec.name, key)
        # A defaulted parameter may not also be required (it is never missing).
        overlap = set(spec.defaults) & set(schema.get('required', []))
        assert not overlap, '%s: %s both required and defaulted' % (spec.name, overlap)


def test_annotations_are_exclusive():
    for spec in SPECS:
        assert not (spec.read_only and spec.destructive), spec.name


def test_dispatch_matches_specs():
    raw = _dispatch()
    server_only = set(raw.get('_server_only', []))
    hidden = set(raw.get('_hidden', []))
    mapping = {k: v for k, v in raw.items()
               if k not in ('_server_only', '_hidden')}

    for spec in SPECS:
        if spec.server_only:
            assert spec.name in server_only, '%s must be listed _server_only' % spec.name
            continue
        assert spec.name in mapping, '%s missing from dispatch.json' % spec.name
        assert mapping[spec.name] == spec.bridge_method, (
            '%s: dispatch says %s, spec says %s'
            % (spec.name, mapping[spec.name], spec.bridge_method))

    spec_names = {s.name for s in SPECS}
    for name in list(mapping) + list(server_only):
        assert name in spec_names, 'dispatch.json references unknown tool %s' % name
    for name in hidden:
        assert name in spec_names, 'hidden tool %s has no spec' % name


def test_method_override_is_used_for_save_as():
    # session_save_as reuses the bridge's session_save handler.
    spec = spec_by_name()['session_save_as']
    assert spec.bridge_method == 'session_save'


def test_visible_specs_excludes_hidden():
    visible = {s.name for s in visible_specs()}
    hidden = {s.name for s in SPECS if s.hidden}
    assert visible == {s.name for s in SPECS} - hidden


def test_session_launch_tools_have_launch_flag():
    """Opening/closing an engine gets the short launch timeout."""
    for name in ('session_open', 'session_new', 'session_close'):
        assert spec_by_name()[name].launch, name


def test_long_running_tools_declared():
    flagged = {s.name for s in SPECS if s.long_running}
    assert flagged == {'run', 'sweep_run', 'execute', 'execute_file'}
