# -*- coding: utf-8 -*-
"""Proctree classification / sweep decisions (no real process is killed).

The interesting logic is *which* engine processes may be killed, so every case
is exercised with an injected process table: killing the wrong engine would
destroy a user's GUI session, and failing to kill an orphan leaks a license.
"""
from fdtd_mcp.engine import proctree


BRIDGE = 'python.exe'
ENGINE = 'fdtd-solutions.exe'


def _table(rows):
    return [{'pid': p, 'ppid': pp, 'name': n} for (p, pp, n) in rows]


def test_classify_splits_owned_orphan_and_foreign():
    table = _table([
        (1, 0, 'node.exe'),                # the DSH host
        (100, 1, BRIDGE),                  # our bridge python (own_pid)
        (200, 100, ENGINE),                # ours: parent is us
        (300, 999, ENGINE),                # orphan: parent 999 is gone
        (400, 1, 'fdtd-solutions.exe'),    # foreign: parent alive (a GUI)
        (500, 100, 'notepad.exe'),         # not an engine
    ])
    info = proctree.classify(table=table, own_pid=100)
    assert [e['pid'] for e in info['owned']] == [200]
    assert [e['pid'] for e in info['orphan']] == [300]
    assert [e['pid'] for e in info['foreign']] == [400]
    assert info['bridge'] == []


def test_classify_recognises_the_bridges_engine():
    """From the server's view the live engine's parent is the bridge — a live
    process — so it must be reported as `bridge`, not as another client's."""
    table = _table([
        (1, 0, 'node.exe'),
        (50, 1, 'python.exe'),             # the MCP server (own_pid)
        (100, 50, BRIDGE),                 # its bridge
        (200, 100, ENGINE),                # the live session's engine
    ])
    info = proctree.classify(table=table, own_pid=50, bridge_pid=100)
    assert [e['pid'] for e in info['bridge']] == [200]
    assert info['foreign'] == []
    assert info['orphan'] == []


def test_foreign_engines_are_never_swept(monkeypatch):
    table = _table([
        (1, 0, 'node.exe'),      # a live process, so pid 1 is not "dead"
        (100, 1, BRIDGE),
        (300, 999, ENGINE),      # orphan -> killed
        (400, 1, ENGINE),        # foreign -> kept
    ])
    killed = []
    monkeypatch.setattr(proctree, 'process_table', lambda: table)
    monkeypatch.setattr(proctree, 'kill_pid',
                        lambda pid, tree=True: (killed.append(pid), (True, 'ok'))[1])
    report = proctree.sweep_orphans(own_pid=100)
    assert killed == [300]
    assert [e['pid'] for e in report['skipped']] == [400]
    assert report['counts'] == {'owned': 0, 'bridge': 0, 'orphan': 1,
                                'foreign': 1}


def test_kill_owned_includes_remembered_pids(monkeypatch):
    """A re-parented engine recorded at launch must still be reaped."""
    table = _table([(100, 1, BRIDGE), (400, 1, ENGINE)])
    killed = []
    monkeypatch.setattr(proctree, 'process_table', lambda: table)
    monkeypatch.setattr(proctree, 'kill_pid',
                        lambda pid, tree=True: (killed.append(pid), (True, 'ok'))[1])
    report = proctree.kill_owned(own_pid=100, expect=[400, 999])
    assert killed == [400]          # 400 via `expect`, 999 does not exist
    assert [e['pid'] for e in report['killed']] == [400]


def test_kill_owned_reports_failures(monkeypatch):
    table = _table([(100, 1, BRIDGE), (200, 100, ENGINE)])
    monkeypatch.setattr(proctree, 'process_table', lambda: table)
    monkeypatch.setattr(proctree, 'kill_pid', lambda pid, tree=True: (False, 'boom'))
    report = proctree.kill_owned(own_pid=100)
    assert report['killed'] == []
    assert report['failed'][0]['pid'] == 200


def test_inventory_shape(monkeypatch):
    table = _table([(1, 0, 'node.exe'), (100, 1, BRIDGE), (500, 1, BRIDGE),
                    (200, 100, ENGINE), (600, 500, ENGINE),
                    (300, 999, ENGINE), (400, 1, ENGINE)])
    monkeypatch.setattr(proctree, 'process_table', lambda: table)
    inv = proctree.inventory(own_pid=100, bridge_pid=500)
    assert inv['own_pid'] == 100
    assert inv['bridge_pid'] == 500
    assert inv['counts'] == {'owned': 1, 'bridge': 1, 'orphan': 1, 'foreign': 1}
    assert [e['pid'] for e in inv['bridge']] == [600]
    assert [e['pid'] for e in inv['orphans']] == [300]
    assert [e['pid'] for e in inv['foreign']] == [400]


def test_missing_pid_is_not_alive(monkeypatch):
    monkeypatch.setattr(proctree, 'process_table',
                        lambda: _table([(100, 1, BRIDGE)]))
    assert proctree.is_process(100) is True
    assert proctree.is_process(999) is False
    assert proctree.is_process(0) is False


def test_job_object_is_best_effort(monkeypatch):
    """Unsupported platforms report failure instead of raising."""
    monkeypatch.setattr(proctree, 'available', lambda: False)
    assert proctree.assign_kill_on_close([1, 2]) == {
        'supported': False, 'assigned': [], 'failed': []}
