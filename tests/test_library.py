# -*- coding: utf-8 -*-
"""Object library: catalog reading + `library_insert` / `library_list`.

Facts pinned here come from live v202 probes:

  * the library is <LUMERICAL_HOME>/Resources/fdtd-library, one .fsp per item
    plus catalog.xml (129 items: 96 "Structures" + 33 "Analysis");
  * `addobject("<id>")` copies the item in and re-runs its setup script, so a
    library analysis group arrives with its monitors built;
  * an unknown id is a silent no-op in the engine — which is exactly why the
    tool validates against the catalog BEFORE calling it.

Engine coverage uses the scripted fake engine with the same three item shapes as
the real library (group with children / analysis group / bare primitive).
"""
import os
import sys
from pathlib import Path

import pytest

_PKG = str(Path(__file__).resolve().parents[1])
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, _PKG)
os.environ.setdefault('LUMERICAL_HOME', _PKG)

import fake_engine  # noqa: E402

fake_engine.install()

from fdtd_mcp.bridge import FdtdBridge                    # noqa: E402
from fdtd_mcp.engine import library                       # noqa: E402
import fdtd_mcp.server as srv                             # noqa: E402

FIXTURE_HOME = str(Path(__file__).resolve().parent / 'fixtures' / 'lumhome')


@pytest.fixture(autouse=True)
def _fake_lumapi():
    """Re-assert the fake engine before each test.

    Other modules (tests/test_sanitize.py, tests/test_bridge_protocol.py) put a
    stub lumapi into sys.modules whose FDTD() returns None; relying on import
    order would make these tests depend on which module pytest imported last.
    """
    fake_engine.install()
    yield


@pytest.fixture
def home(monkeypatch):
    """Point both the bridge-side and server-side readers at the fixture."""
    monkeypatch.setenv('LUMERICAL_HOME', FIXTURE_HOME)
    library._CACHE.clear()
    yield FIXTURE_HOME
    library._CACHE.clear()


# ---------------------------------------------------------------------------
# catalog.xml reading
# ---------------------------------------------------------------------------

def test_catalog_reads_ids_types_and_categories(home):
    items = library.catalog(home)
    # sorted by (category, id): Integrated optics, Resonators and modal
    # analysis, then Uncategorized structures
    assert [i['id'] for i in items] == ['ghost_item', 'ring_resonator',
                                       'Qanalysis', 'sphere']
    by_id = dict((i['id'], i) for i in items)
    assert by_id['Qanalysis']['type'] == 'Analysis'
    assert by_id['ring_resonator']['type'] == 'Structures'
    assert by_id['Qanalysis']['category'] == 'Resonators and modal analysis'
    assert library.counts(items) == {'Analysis': 1, 'Structures': 3}


def test_catalog_flags_missing_project_files(home):
    by_id = dict((i['id'], i) for i in library.catalog(home))
    assert by_id['Qanalysis']['installed'] is True
    assert by_id['ghost_item']['installed'] is False
    assert by_id['sphere']['installed'] is True


def test_catalog_is_empty_without_an_install(monkeypatch, tmp_path=None):
    monkeypatch.setenv('LUMERICAL_HOME', str(Path(_PKG) / 'no-such-install'))
    library._CACHE.clear()
    assert library.catalog() == []
    assert library.library_dir() is None


def test_find_is_case_insensitive_and_suggest_helps(home):
    assert library.find('qanalysis', home)['id'] == 'Qanalysis'
    assert library.find('nope', home) is None
    assert library.suggest('ring_res', home) == ['ring_resonator']
    assert 'Qanalysis' in library.suggest('qanalisys', home)


# ---------------------------------------------------------------------------
# library_insert (bridge, against the fake engine)
# ---------------------------------------------------------------------------

def test_insert_analysis_group_builds_its_monitors(home):
    b = FdtdBridge()
    b._cmd_session_new({})
    r = b._cmd_library_insert({'name': 'Qanalysis'})
    assert r['status'] == 'ok'
    assert r['library_id'] == 'Qanalysis'
    assert r['library_type'] == 'Analysis'
    assert r['created'] == ['Qanalysis'] + ['Qanalysis::t%d' % i for i in (1, 2, 3, 4)]
    # the group's own scripts came along — that is the whole point of the item
    assert 'setup script' in str(b._cmd_model_get({'name': 'Qanalysis'}))


def test_insert_structure_group_builds_its_geometry(home):
    b = FdtdBridge()
    b._cmd_session_new({})
    r = b._cmd_library_insert({'name': 'ring_resonator'})
    assert 'ring resonator' in r['created']
    assert 'ring resonator::arc1' in r['created']
    assert r['library_type'] == 'Structures'


def test_insert_primitive_item(home):
    b = FdtdBridge()
    b._cmd_session_new({})
    r = b._cmd_library_insert({'name': 'sphere'})
    assert r['created'] == ['sphere']


def test_unknown_id_is_rejected_before_the_engine(home):
    b = FdtdBridge()
    b._cmd_session_new({})
    with pytest.raises(Exception) as err:
        b._cmd_library_insert({'name': 'ring_res'})
    assert 'ring_resonator' in str(err.value)     # suggestion included


def test_missing_name_is_a_clear_error(home):
    b = FdtdBridge()
    b._cmd_session_new({})
    with pytest.raises(Exception) as err:
        b._cmd_library_insert({})
    assert 'library_list' in str(err.value)


def test_item_without_project_file_is_refused(home):
    b = FdtdBridge()
    b._cmd_session_new({})
    with pytest.raises(Exception) as err:
        b._cmd_library_insert({'name': 'ghost_item'})
    assert 'install is incomplete' in str(err.value)


def test_insert_resets_group_scope_first(home, monkeypatch):
    """addobject acts on the CURRENT scope: without a reset the item can land
    inside whatever group was visited last. Live bug: Qanalysis was inserted
    into transmission_calc and then rolled back by the engine."""
    events = []
    orig_reset = FdtdBridge._reset_to_model_root
    orig_call = FdtdBridge._call_lsf

    def spy_reset(self):
        events.append('reset')
        return orig_reset(self)

    def spy_call(self, func, *args):
        if func == 'addobject':
            events.append('addobject')
        return orig_call(self, func, *args)

    monkeypatch.setattr(FdtdBridge, '_reset_to_model_root', spy_reset)
    monkeypatch.setattr(FdtdBridge, '_call_lsf', spy_call)

    b = FdtdBridge()
    b._cmd_session_new({})
    b._cmd_library_insert({'name': 'sphere'})
    assert 'addobject' in events
    assert events[events.index('addobject') - 1] == 'reset'


def test_traverse_restores_the_model_scope(home):
    """A walk must not leave the engine scoped inside the last group visited."""
    b = FdtdBridge()
    b._cmd_session_new({})
    b._fsp._eval_log = []
    objects, seen = [], set()
    b._traverse('::model', ['type'], False, objects, seen)
    assert b._fsp._eval_log[-1] == 'groupscope("::model");'


def test_rolled_back_insert_is_not_reported_as_ok(home, monkeypatch):
    """A name diff can contain phantoms; only real objects may be reported."""
    b = FdtdBridge()
    b._cmd_session_new({})
    # 'phantom_obj' is never created by the engine, yet shows up in the diff
    seq = [set(), {'::model::phantom_obj'}]      # before / after snapshots

    def fake_scene(self):
        return seq.pop(0) if seq else set()

    monkeypatch.setattr(FdtdBridge, '_scene_names', fake_scene)
    with pytest.raises(Exception) as err:
        b._cmd_library_insert({'name': 'Qanalysis'})
    assert 'added nothing' in str(err.value)


# ---------------------------------------------------------------------------
# library_list (server-side)
# ---------------------------------------------------------------------------

def test_server_list_reports_counts_and_categories(home, monkeypatch):
    monkeypatch.setattr(srv, 'LUMERICAL_HOME', home)
    out = srv._handle_library_list({})['result']
    assert out['total'] == 4
    assert out['counts_by_type'] == {'Analysis': 1, 'Structures': 3}
    assert out['matched'] == 4
    assert 'ring_resonator' in [i['id'] for i in out['items']]


def test_server_list_filters_by_type_and_category(home, monkeypatch):
    monkeypatch.setattr(srv, 'LUMERICAL_HOME', home)
    only_analysis = srv._handle_library_list({'type': 'Analysis'})['result']
    assert [i['id'] for i in only_analysis['items']] == ['Qanalysis']
    by_cat = srv._handle_library_list({'category': 'Integrated'})['result']
    assert [i['id'] for i in by_cat['items']] == ['ghost_item', 'ring_resonator']


def test_server_list_single_id_and_typo_hint(home, monkeypatch):
    monkeypatch.setattr(srv, 'LUMERICAL_HOME', home)
    one = srv._handle_library_list({'id': 'Qanalysis'})['result']
    assert one['count'] == 1 and one['items'][0]['type'] == 'Analysis'
    miss = srv._handle_library_list({'id': 'qanalisys'})['result']
    assert miss['count'] == 0
    assert 'Qanalysis' in miss['did_you_mean']


def test_server_list_limit(monkeypatch, home):
    monkeypatch.setattr(srv, 'LUMERICAL_HOME', home)
    out = srv._handle_library_list({'limit': 1})['result']
    assert out['returned'] == 1 and out['truncated'] is True
