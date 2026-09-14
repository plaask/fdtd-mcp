# -*- coding: utf-8 -*-
"""Engine-process hygiene: a session transition or bridge exit must not leave
an fdtd-solutions.exe behind.

The bug this pins down: `lumapi.FDTD()` spawns the engine as a CHILD of the
bridge, and Windows kills nothing when a parent dies. The bridge used to exit
on EOF (MCP server recycled) with the engine still running — every server
restart orphaned one engine, which kept an ANSYS license seat and showed up as
a windowless FDTD instance. Production evidence: 7 orphaned engines whose
parent pids were all dead bridges.

Driven in-process against the scripted fake engine, with proctree's
process-killing calls recorded so no real process is touched.
"""
import os
import sys
from pathlib import Path

import pytest

_PKG = str(Path(__file__).resolve().parents[1])
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, _PKG)
# bridge.py refuses to import without an installation path; the fake engine
# supplies everything else, so point it at the repo root (as the functional
# runner does).
os.environ.setdefault('LUMERICAL_HOME', _PKG)

import fake_engine  # noqa: E402

fake_engine.install()

from fdtd_mcp.bridge import FdtdBridge            # noqa: E402
from fdtd_mcp.engine import proctree              # noqa: E402


class _Recorder(object):
    def __init__(self):
        self.killed = []
        self.owned = []
        self.jobs = []

    def install(self, monkeypatch, owned_pids=(4321,)):
        self.owned = list(owned_pids)

        def owned_engines(own_pid=None, table=None):
            return [{'pid': p, 'ppid': 1, 'name': 'fdtd-solutions.exe'}
                    for p in self.owned]

        def kill_owned(own_pid=None, expect=None):
            expect = list(expect or [])
            self.killed.append(expect)
            return {'killed': [{'pid': p} for p in expect], 'failed': []}

        def assign(pids):
            self.jobs.append(list(pids))
            return {'supported': True, 'assigned': list(pids), 'failed': []}

        def inventory(own_pid=None):
            return {'own_pid': 1, 'counts': {'owned': len(self.owned),
                                             'orphan': 0, 'foreign': 0},
                    'owned': [], 'orphans': [], 'foreign': []}

        monkeypatch.setattr(proctree, 'owned_engines', owned_engines)
        monkeypatch.setattr(proctree, 'kill_owned', kill_owned)
        monkeypatch.setattr(proctree, 'assign_kill_on_close', assign)
        monkeypatch.setattr(proctree, 'inventory', inventory)


@pytest.fixture(autouse=True)
def _fake_lumapi():
    """Re-assert the fake engine: other test modules replace sys.modules['lumapi']
    with a stub whose FDTD() returns None, which would otherwise leave these
    tests with a bridge that has no project."""
    fake_engine.install()
    yield


@pytest.fixture
def rec(monkeypatch):
    r = _Recorder()
    r.install(monkeypatch)
    return r


def test_launch_tracks_and_job_binds_the_engine(rec):
    b = FdtdBridge()
    b._cmd_session_new({})
    assert b._engine_pids == [4321]
    # Job binding is what makes a force-killed bridge take its engine with it.
    assert rec.jobs == [[4321]]


def test_session_close_reaps_an_engine_that_survived_close(rec):
    b = FdtdBridge()
    b._cmd_session_new({})
    result = b._cmd_session_close({})
    # [_begin_session's pre-launch reap (nothing to kill yet), close's reap]
    assert rec.killed == [[], [4321]]
    assert result['engine_reap']['killed'] == [{'pid': 4321}]


def test_reopen_reaps_before_launching_the_next_engine(rec):
    b = FdtdBridge()
    b._cmd_session_new({})
    b._cmd_session_new({})
    assert rec.killed == [[], [4321]]      # second open reaps the first engine
    assert b._engine_pids == [4321]


def test_shutdown_closes_session_and_reaps(rec):
    b = FdtdBridge()
    b._cmd_session_new({})
    report = b.shutdown()
    assert report['closed'] is True
    assert report['reaped']['killed'] == [{'pid': 4321}]
    assert b._fsp is None
    assert b._engine_pids == []


def test_shutdown_survives_a_broken_close(rec):
    """Shutdown runs from bridge.main()'s finally block: it must never raise."""
    b = FdtdBridge()
    b._cmd_session_new({})

    class _Boom(object):
        def close(self):
            raise RuntimeError('engine already gone')

    b._fsp = _Boom()
    report = b.shutdown()
    assert report['closed'] is True
    assert report['errors'] and 'close' in report['errors'][0]
    assert rec.killed[-1] == [4321]


def test_engine_processes_reports_inventory(monkeypatch):
    monkeypatch.setattr(proctree, 'inventory', lambda own_pid=None: {
        'own_pid': 1, 'counts': {'owned': 1, 'orphan': 2, 'foreign': 0},
        'owned': [], 'orphans': [], 'foreign': []})
    b = FdtdBridge()
    assert b.engine_processes()['counts']['orphan'] == 2


def test_engine_status_exposes_process_inventory(rec, monkeypatch):
    b = FdtdBridge()
    b._cmd_session_new({})
    st = b._cmd_engine_status({})
    assert st['session_engine_pids'] == [4321]
    assert st['engine_processes']['counts']['owned'] == 1
