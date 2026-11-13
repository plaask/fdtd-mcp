# -*- coding: utf-8 -*-
"""The object-type registry is the single source for model_add.

It feeds toolspec's enum, handlers/model.py's dispatch, the fake engine's
adder table and the scanner allowlist. These tests fail when one of them
drifts.
"""
import importlib.util
from pathlib import Path

from fdtd_mcp.engine.capabilities import OBJECT_TYPES
from fdtd_mcp.toolspec import spec_by_name


def _fake_engine():
    path = Path(__file__).resolve().parent / 'fake_engine.py'
    spec = importlib.util.spec_from_file_location('fake_engine_for_test', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_fake_engine_covers_every_registry_adder():
    fake = _fake_engine()
    fake_adders = set(fake.FakeFDTD.ADD)
    registry_adders = set(spec['add'] for spec in OBJECT_TYPES.values())
    assert fake_adders == registry_adders, (
        'fake_engine ADD drifted from OBJECT_TYPES: missing %s, extra %s'
        % (sorted(registry_adders - fake_adders),
           sorted(fake_adders - registry_adders)))
    for adder in registry_adders:
        expected = sorted({spec['type'] for spec in OBJECT_TYPES.values()
                           if spec['add'] == adder})
        assert fake.FakeFDTD.ADD[adder][0] in expected, adder


def test_model_add_enum_matches_registry():
    spec = spec_by_name()['model_add']
    assert set(spec.schema['properties']['type']['enum']) == set(OBJECT_TYPES)


def test_registry_adders_are_allowlisted():
    import fdtd_mcp.server as srv
    missing = sorted({spec['add'] for spec in OBJECT_TYPES.values()}
                     - set(srv._LSF_BUILTINS))
    assert not missing, 'scanner allowlist is missing: %s' % missing


def test_every_type_has_the_metadata_the_tools_use():
    for name, spec in OBJECT_TYPES.items():
        assert spec.get('add'), name
        assert spec.get('type'), name
        assert spec.get('group'), name
        assert spec.get('since'), name
