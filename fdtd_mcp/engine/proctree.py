# -*- coding: utf-8 -*-
"""Engine-process bookkeeping (Windows, ctypes only, Python 3.6.8 safe).

Why this module exists
----------------------
`lumapi.FDTD()` spawns `fdtd-solutions.exe` as a CHILD of the bridge process.
Windows does not kill children when a parent dies, so whenever the bridge is
terminated without killing its tree the engine keeps running: it keeps an
ANSYS license seat, ~100-240 MB of RAM, and it looks to the user like "many
FDTD instances" with no window. Two real paths do exactly that:

  * the MCP server process is recycled (patch reload / `dsh web` restart) ->
    the bridge sees EOF on stdin and exits with `sys.exit(0)` without closing
    the session (see bridge.main);
  * anyone runs Stop-Process / taskkill on the bridge python without `/T`.

This module gives the bridge (and the server) an accurate picture of engine
processes so it can (a) report them, (b) never leave one behind, and
(c) clean up orphans later without touching another client's engine.

Classification (the only safe way to decide what may be killed):

    owned   -- parent IS this process            -> ours, may be killed
    orphan  -- parent PID is gone from the table -> nobody can be using it
    foreign -- parent is alive and is not us     -> a Lumerical GUI or another
                                                    client; NEVER kill it
"""
from __future__ import print_function

import os
import subprocess
import sys
import time

TH32CS_SNAPPROCESS = 0x00000002
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_PROCESS_TERMINATE = 0x0001

# Lumerical spawns the solver engine under these names (FDTD / MODE / DEVICE).
ENGINE_EXE_HINTS = ('fdtd-solutions', 'mode-solutions', 'device-solutions')


class _PROCESSENTRY32(object):
    """Layout-compatible stand-in; built lazily so import never fails."""


def _entry_struct():
    import ctypes
    from ctypes import wintypes

    class PROCESSENTRY32(ctypes.Structure):
        _fields_ = [
            ('dwSize', wintypes.DWORD),
            ('cntUsage', wintypes.DWORD),
            ('th32ProcessID', wintypes.DWORD),
            ('th32DefaultHeapID', ctypes.POINTER(ctypes.c_ulong)),
            ('th32ModuleID', wintypes.DWORD),
            ('cntThreads', wintypes.DWORD),
            ('th32ParentProcessID', wintypes.DWORD),
            ('pcPriClassBase', ctypes.c_long),
            ('dwFlags', wintypes.DWORD),
            ('szExeFile', ctypes.c_char * 260),
        ]

    return PROCESSENTRY32


def available():
    """True when process enumeration is possible (Windows + ctypes)."""
    return os.name == 'nt'


def process_table():
    """[{'pid','ppid','name'}] for every process, or [] when unavailable."""
    if not available():
        return []
    import ctypes
    from ctypes import wintypes

    entry_t = _entry_struct()
    k32 = ctypes.WinDLL('kernel32', use_last_error=True)
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k32.Process32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(entry_t)]
    k32.Process32First.restype = wintypes.BOOL
    k32.Process32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(entry_t)]
    k32.Process32Next.restype = wintypes.BOOL
    k32.CloseHandle.argtypes = [wintypes.HANDLE]

    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    invalid = ctypes.c_void_p(-1).value
    if not snap or snap == invalid:
        return []
    rows = []
    try:
        entry = entry_t()
        entry.dwSize = ctypes.sizeof(entry_t)
        ok = k32.Process32First(snap, ctypes.byref(entry))
        while ok:
            rows.append({
                'pid': int(entry.th32ProcessID),
                'ppid': int(entry.th32ParentProcessID),
                'name': entry.szExeFile.decode('mbcs', 'replace'),
            })
            ok = k32.Process32Next(snap, ctypes.byref(entry))
    except Exception:
        return rows
    finally:
        try:
            k32.CloseHandle(snap)
        except Exception:
            pass
    return rows


def is_process(pid, table=None):
    """True when `pid` is a live process (snapshot lookup — cheap and exact)."""
    if not pid or pid <= 0:
        return False
    rows = process_table() if table is None else table
    for row in rows:
        if row['pid'] == pid:
            return True
    return False


def looks_like_engine(name):
    low = (name or '').lower()
    for hint in ENGINE_EXE_HINTS:
        if hint in low:
            return True
    return False


def classify(table=None, own_pid=None, bridge_pid=None):
    """Split engine processes into owned / bridge / orphan / foreign.

    `own_pid` is the caller (the bridge reaping itself, the server reporting);
    `bridge_pid` lets a server-side caller recognise the engines of its own
    bridge, so the live session is reported as `bridge` instead of being lumped
    in with other clients' engines as `foreign`.
    """
    rows = process_table() if table is None else table
    if own_pid is None:
        own_pid = os.getpid()
    live = set(row['pid'] for row in rows)
    by_pid = dict((row['pid'], row) for row in rows)
    out = {'owned': [], 'bridge': [], 'orphan': [], 'foreign': [], 'other': {}}
    for row in rows:
        if not looks_like_engine(row['name']):
            continue
        ppid = row['ppid']
        parent = by_pid.get(ppid)
        info = {
            'pid': row['pid'],
            'ppid': ppid,
            'name': row['name'],
            'parent': parent['name'] if parent else None,
        }
        if ppid == own_pid:
            out['owned'].append(info)
        elif bridge_pid and ppid == bridge_pid:
            out['bridge'].append(info)
        elif ppid not in live:
            out['orphan'].append(info)
        else:
            out['foreign'].append(info)
    out['other'] = {'own_pid': own_pid, 'bridge_pid': bridge_pid,
                    'total_processes': len(rows)}
    return out


def owned_engines(own_pid=None, table=None):
    """Engine processes whose parent is this process."""
    return classify(table=table, own_pid=own_pid)['owned']


def kill_owned(own_pid=None, expect=None):
    """Kill every engine process that is a child of this process.

    Called when a session is closed or the bridge is shutting down: `close()`
    normally terminates the engine, but if it did not (wedged solver, dead
    engine, no license) the survivor would hold a license seat forever.

    `expect` is the pid list captured at launch time; it is killed even when the
    parent link is no longer visible, so a re-parented engine cannot escape.
    """
    info = classify(own_pid=own_pid)
    pids = [item['pid'] for item in info['owned']]
    for pid in (expect or []):
        if pid not in pids and is_process(pid):
            pids.append(pid)
    report = {'killed': [], 'failed': [], 'counts': {
        'owned': len(info['owned']), 'bridge': len(info['bridge']),
        'orphan': len(info['orphan']), 'foreign': len(info['foreign'])}}
    for pid in pids:
        ok, msg = kill_pid(pid)
        (report['killed'] if ok else report['failed']).append(
            {'pid': pid, 'message': msg})
    return report


def inventory(own_pid=None, bridge_pid=None):
    """Compact engine-process picture for status output."""
    info = classify(own_pid=own_pid, bridge_pid=bridge_pid)
    return {
        'own_pid': info['other']['own_pid'],
        'bridge_pid': info['other'].get('bridge_pid'),
        'counts': {'owned': len(info['owned']), 'bridge': len(info['bridge']),
                   'orphan': len(info['orphan']),
                   'foreign': len(info['foreign'])},
        'owned': info['owned'],
        'bridge': info['bridge'],
        'orphans': info['orphan'],
        'foreign': info['foreign'],
    }


# ----------------------------------------------------------------------
# Job object: the only guarantee that survives a hard kill of the bridge
# ----------------------------------------------------------------------

_JOB_HANDLES = []          # keep job handles open for this process' lifetime

JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_TERMINATE = 0x0001
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def assign_kill_on_close(pids):
    """Put engine pids in a job the OS tears down when this process dies.

    Windows kills nothing when a parent dies — that is exactly how orphaned
    engines pile up. A job object with KILL_ON_JOB_CLOSE inverts it: the moment
    the last handle to the job closes (i.e. this process exits, gracefully or
    not), the OS terminates every process in it.

    Best effort by design: older Windows versions refuse to nest jobs, in which
    case this reports `supported: False` and the explicit sweep paths still
    apply. Returns {'supported', 'assigned', 'failed'}.
    """
    pids = [int(p) for p in (pids or []) if p]
    result = {'supported': available(), 'assigned': [], 'failed': []}
    if not available() or not pids:
        return result
    import ctypes
    from ctypes import wintypes

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [('ReadOperationCount', ctypes.c_ulonglong),
                    ('WriteOperationCount', ctypes.c_ulonglong),
                    ('OtherOperationCount', ctypes.c_ulonglong),
                    ('ReadTransferCount', ctypes.c_ulonglong),
                    ('WriteTransferCount', ctypes.c_ulonglong),
                    ('OtherTransferCount', ctypes.c_ulonglong)]

    class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [('PerProcessUserTimeLimit', wintypes.LARGE_INTEGER),
                    ('PerJobUserTimeLimit', wintypes.LARGE_INTEGER),
                    ('LimitFlags', wintypes.DWORD),
                    ('MinimumWorkingSetSize', ctypes.c_size_t),
                    ('MaximumWorkingSetSize', ctypes.c_size_t),
                    ('ActiveProcessLimit', wintypes.DWORD),
                    ('Affinity', ctypes.c_size_t),
                    ('PriorityClass', wintypes.DWORD),
                    ('SchedulingClass', wintypes.DWORD)]

    class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [('BasicLimitInformation', JOBOBJECT_BASIC_LIMIT_INFORMATION),
                    ('IoInfo', IO_COUNTERS),
                    ('ProcessMemoryLimit', ctypes.c_size_t),
                    ('JobMemoryLimit', ctypes.c_size_t),
                    ('PeakProcessMemoryUsed', ctypes.c_size_t),
                    ('PeakJobMemoryUsed', ctypes.c_size_t)]

    k32 = ctypes.WinDLL('kernel32', use_last_error=True)
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    k32.SetInformationJobObject.restype = wintypes.BOOL
    k32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.AssignProcessToJobObject.restype = wintypes.BOOL
    k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]

    job = k32.CreateJobObjectW(None, None)
    if not job:
        result['supported'] = False
        return result
    info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not k32.SetInformationJobObject(
            job, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(info), ctypes.sizeof(info)):
        result['supported'] = False
        k32.CloseHandle(job)
        return result
    for pid in pids:
        handle = k32.OpenProcess(
            _PROCESS_SET_QUOTA | _PROCESS_TERMINATE
            | _PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            result['failed'].append({'pid': pid, 'message': 'OpenProcess failed'})
            continue
        try:
            if k32.AssignProcessToJobObject(job, handle):
                result['assigned'].append(pid)
            else:
                result['failed'].append(
                    {'pid': pid, 'message': 'AssignProcessToJobObject failed'})
        finally:
            k32.CloseHandle(handle)
    if result['assigned']:
        _JOB_HANDLES.append(job)     # closing it would kill the engines
    else:
        k32.CloseHandle(job)
    return result


def descendants(pid, table=None):
    """All transitive child pids of `pid` (from one snapshot)."""
    rows = process_table() if table is None else table
    children = {}
    for row in rows:
        children.setdefault(row['ppid'], []).append(row['pid'])
    out = []
    seen = set([pid])
    stack = [pid]
    while stack:
        for child in children.get(stack.pop(), []):
            if child not in seen:
                seen.add(child)
                out.append(child)
                stack.append(child)
    return out


def _terminate(pid):
    """TerminateProcess — works where the taskkill *binary* is policy-blocked."""
    if not available():
        return False, 'unsupported platform'
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL('kernel32', use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    k32.TerminateProcess.restype = wintypes.BOOL
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = k32.OpenProcess(_PROCESS_TERMINATE, False, int(pid))
    if not handle:
        return False, 'OpenProcess denied (err %d)' % ctypes.get_last_error()
    try:
        if k32.TerminateProcess(handle, 1):
            return True, 'terminated'
        return False, 'TerminateProcess failed (err %d)' % ctypes.get_last_error()
    finally:
        k32.CloseHandle(handle)


def _wait_gone(pids, timeout):
    """Wait until none of `pids` exist; returns the survivors ([] when clean)."""
    deadline = time.time() + timeout
    while True:
        left = [p for p in pids if is_process(p)]
        if not left:
            return []
        if time.time() >= deadline:
            return left
        time.sleep(0.25)


def kill_pid(pid, tree=True, wait=8.0):
    """Force-kill a process and (by default) its descendants.

    Deliberately NOT built on the taskkill binary: an outer sandbox or group
    policy can refuse it ("Access denied") while TerminateProcess still works,
    and a silently failing tree kill is exactly how engines get orphaned.
    taskkill is kept as a last-resort fallback for processes that refuse
    TerminateProcess.

    A Lumerical engine needs a moment to actually exit (license release, file
    flush), so liveness is polled instead of assumed — reporting a slow exit as
    a failure would make the caller kill the same pid twice.

    Returns (ok, message); ok is True only when nothing survived.
    """
    if not pid or pid <= 0:
        return False, 'invalid pid'
    if not available():                                     # pragma: no cover
        try:
            os.kill(int(pid), 9)
            return True, 'killed'
        except Exception as e:
            return False, str(e)
    targets = [int(pid)] + (descendants(pid) if tree else [])
    errors = []
    for target in reversed(targets):        # children first
        if not is_process(target):
            continue
        ok, msg = _terminate(target)
        if not ok:
            errors.append('%d: %s' % (target, msg))
    survivors = _wait_gone(targets, wait)
    if survivors:
        try:
            subprocess.call(['taskkill', '/PID', str(survivors[0]), '/T', '/F'],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            creationflags=0x08000000)
        except Exception:
            pass
        survivors = _wait_gone(targets, wait)
    if survivors:
        return False, 'still running: %s%s' % (
            survivors, (' (' + '; '.join(errors) + ')') if errors else '')
    return True, 'killed %d process(es)' % len(targets)


def sweep_orphans(own_pid=None, include_owned=False, table=None):
    """Kill orphan engines (and optionally our own). Never touches foreign.

    Returns a report dict: {'killed': [...], 'failed': [...], 'skipped': [...]}.
    """
    info = classify(table=table, own_pid=own_pid)
    targets = list(info['orphan'])
    if include_owned:
        targets.extend(info['owned'])
    report = {'killed': [], 'failed': [], 'skipped': info['foreign'],
              'own_pid': info['other']['own_pid'],
              'counts': {'owned': len(info['owned']),
                         'bridge': len(info['bridge']),
                         'orphan': len(info['orphan']),
                         'foreign': len(info['foreign'])}}
    for item in targets:
        ok, msg = kill_pid(item['pid'])
        entry = dict(item)
        entry['message'] = msg
        (report['killed'] if ok else report['failed']).append(entry)
    # Verify rather than trust: an orphan that survived is a real failure.
    report['orphan_left'] = classify(own_pid=report['own_pid'])['orphan']
    return report


def _main(argv):
    """Self-check / manual sweep:  python -m fdtd_mcp.engine.proctree [--kill]"""
    kill = '--kill' in argv
    if kill:
        report = sweep_orphans()
        for group in ('killed', 'failed', 'skipped'):
            print('%-8s: %s' % (group, ', '.join(
                '%s%s' % (e['pid'], (' (' + e.get('message', '') + ')')
                          if e.get('message') else '')
                for e in report[group]) or '-'))
        return 0 if not report['failed'] and not report['orphan_left'] \
            else 1
    info = classify()
    print('own pid            : %d' % info['other']['own_pid'])
    print('processes          : %d' % info['other']['total_processes'])
    for kind in ('owned', 'bridge', 'orphan', 'foreign'):
        print('%s engines (%d):' % (kind, len(info[kind])))
        for item in info[kind]:
            print('    pid=%-7d ppid=%-7d parent=%-16s %s'
                  % (item['pid'], item['ppid'], item['parent'] or '<dead>',
                     item['name']))
    return 0


if __name__ == '__main__':
    sys.exit(_main(sys.argv[1:]))
