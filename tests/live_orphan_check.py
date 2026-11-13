# -*- coding: utf-8 -*-
"""LIVE check: an engine can no longer be orphaned by the bridge dying.

Run with Lumerical's bundled Python (needs a license, launches real engines):

    & "$env:LUMERICAL_HOME\python-3.6.8-embed-amd64\python.exe" tests\live_orphan_check.py

Two failure modes used to leak one fdtd-solutions.exe each; both are reproduced
here and asserted to leave nothing behind:

  phase 1  EOF on stdin      -- what an MCP server recycle does
  phase 2  taskkill without /T -- what Stop-Process on the bridge python does
  phase 3  session_close     -- the normal path (should always be clean)

Exit code 0 = every phase left no engine behind.
"""
from __future__ import print_function
import json
import os
import subprocess
import sys
import time
from pathlib import Path

PKG = str(Path(__file__).resolve().parents[1])
sys.path.insert(0, PKG)

from fdtd_mcp.discovery import find_lumerical  # noqa: E402
if not os.environ.get('LUMERICAL_HOME'):
    os.environ['LUMERICAL_HOME'] = find_lumerical()

from fdtd_mcp.engine import proctree  # noqa: E402

LAUNCH_TIMEOUT = 180
GONE_TIMEOUT = 30


def start_bridge():
    env = dict(os.environ)
    env['PYTHONPATH'] = PKG
    proc = subprocess.Popen(
        [sys.executable, '-m', 'fdtd_mcp.bridge'],
        cwd=PKG, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, universal_newlines=True)
    line = proc.stdout.readline()
    ready = json.loads(line)
    assert ready.get('ready'), line
    return proc, ready


def call(proc, rid, method, params=None):
    proc.stdin.write(json.dumps(
        {'id': rid, 'method': method, 'params': params or {}}) + '\n')
    proc.stdin.flush()
    while True:
        line = proc.stdout.readline()
        if not line:
            raise RuntimeError('bridge died while answering ' + method)
        resp = json.loads(line)
        if resp.get('id') == rid:
            return resp


def launch_session():
    """Fresh bridge + fresh engine; returns (proc, engine_pid)."""
    proc, ready = start_bridge()
    t0 = time.time()
    call(proc, 1, 'session_new', {})
    status = call(proc, 2, 'engine_status', {})['result']
    pids = status.get('session_engine_pids') or []
    print('    bridge pid=%d code=%s engine=%s (launch %.1fs)'
          % (proc.pid, ready.get('code_id'), pids, time.time() - t0))
    assert pids, 'engine_status reported no engine pid: %s' % status
    return proc, pids[0]


def wait_gone(pid, timeout=GONE_TIMEOUT):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if not proctree.is_process(pid):
            return time.time() - t0
        time.sleep(0.5)
    return None


def phase(name, kill, expect_bridge_dead=True):
    print('[%s]' % name)
    proc, engine_pid = launch_session()
    assert proctree.is_process(engine_pid), 'engine was not running'
    kill(proc)
    try:
        proc.wait(timeout=60)
    except Exception:
        pass
    if expect_bridge_dead:
        assert not proctree.is_process(proc.pid), 'bridge pid still alive'
    took = wait_gone(engine_pid)
    if took is None:
        print('    FAIL: engine pid %d survived the bridge' % engine_pid)
        return False
    print('    ok: engine %d gone after %.1fs' % (engine_pid, took))
    return True


def main():
    print('engines before: %s' % proctree.inventory()['counts'])
    ok = []

    def _eof(proc):
        proc.stdin.close()          # server went away -> EOF on stdin

    def _hard_kill(proc):
        # Deliberately NO tree kill: only the bridge python dies, exactly like a
        # manual Stop-Process (or a crash). The job object must take the engine
        # down with it. (taskkill is not used because an outer sandbox can
        # refuse the binary — TerminateProcess is the same call Stop-Process
        # makes.)
        ok, msg = proctree.kill_pid(proc.pid, tree=False)
        print('    killed bridge pid=%d -> %s (%s)' % (proc.pid, ok, msg))

    def _close(proc):
        call(proc, 3, 'session_close', {})

    ok.append(phase('phase 1: MCP server recycle (EOF on stdin)', _eof))
    ok.append(phase('phase 2: hard kill of the bridge (no /T)', _hard_kill))
    ok.append(phase('phase 3: session_close (normal path)', _close,
                    expect_bridge_dead=False))

    if not ok[-1]:
        try:
            pass
        except Exception:
            pass
    inventory = proctree.inventory()
    print('engines after: %s' % inventory['counts'])
    if inventory['orphans']:
        print('ORPHANS LEFT: %s' % inventory['orphans'])
        ok.append(False)
    print('RESULT: %s' % ('PASS' if all(ok) else 'FAIL'))
    return 0 if all(ok) else 1


if __name__ == '__main__':
    sys.exit(main())
