# -*- coding: utf-8 -*-
"""Session lifecycle, project I/O and solving.

Split out of bridge.py; runs on Lumerical's bundled Python 3.6.8, so keep the
syntax 3.6-compatible (no f-strings, no type hints).
"""
from __future__ import print_function
import os

from ..engine import proctree
from ..engine.base import BridgeBase
from ..engine.capabilities import engine_facts
from ..engine.constants import ERR_MSG as _ERR_MSG
from ..engine.errors import BridgeError

_FDTD_CONFIG_KEYS = ['dimension', 'x span', 'y span', 'z span',
                     'simulation time', 'mesh accuracy']

_SUMMARY_KEYS = ['dimension', 'x span', 'y span', 'z span', 'simulation time',
                 'mesh accuracy', 'x min bc', 'x max bc', 'y min bc', 'y max bc',
                 'z min bc', 'z max bc']


class SessionHandlers(BridgeBase):
    """Session lifecycle, project I/O and solving."""

    # ------------------------------------------------------------------
    # Engine-process hygiene
    #
    # `lumapi.FDTD()` spawns fdtd-solutions.exe as a CHILD of this process and
    # Windows does not reap children when a parent dies. Anything that ends the
    # bridge without an explicit close (MCP server recycle -> EOF on stdin,
    # Stop-Process on the bridge, a crash) therefore leaves a hidden engine
    # holding a license seat. Every exit path below is covered:
    #
    #   * launch  -> remember the pid + bind it to a kill-on-close job object
    #   * close   -> kill any engine of ours that survived close()
    #   * reopen  -> reap before starting the next engine
    #   * exit    -> shutdown() runs from bridge.main()'s finally block
    # ------------------------------------------------------------------

    def _track_engine(self):
        """Remember (and job-bind) the engine process just launched."""
        self._engine_pids = [item['pid'] for item in proctree.owned_engines()]
        self._engine_job = proctree.assign_kill_on_close(self._engine_pids)
        return self._engine_pids

    def _reap_engines(self):
        """Kill engine processes of ours that outlived their session."""
        pids = list(getattr(self, '_engine_pids', []) or [])
        self._engine_pids = []
        try:
            return proctree.kill_owned(expect=pids)
        except Exception as e:                      # never fail a close on this
            return {'killed': [], 'failed': [{'pid': None,
                                              'message': str(e)[:_ERR_MSG]}]}

    def engine_processes(self):
        """Engine-process inventory for engine_status."""
        try:
            return proctree.inventory()
        except Exception as e:
            return {'error': str(e)[:_ERR_MSG]}

    def shutdown(self):
        """Close the session and guarantee no engine of ours survives.

        Runs from bridge.main()'s finally block, including the EOF path taken
        when the MCP server dies — that path used to exit(0) with the engine
        still running, which is how orphans accumulated across server restarts.
        """
        errors = []
        if self._fsp:
            try:
                self._fsp.close()
            except Exception as e:
                errors.append('close: ' + str(e)[:_ERR_MSG])
            self._fsp = None
        reaped = self._reap_engines()
        return {'closed': True, 'errors': errors, 'reaped': reaped}

    def _open_engine(self, path=None):
        """Launch a hidden engine instance.

        `lumapi` is imported here (not at module import time) so this module —
        and the tests that import it — load without a Lumerical installation.
        """
        import lumapi
        if path:
            return lumapi.FDTD(path, hide=True)
        return lumapi.FDTD(hide=True)

    def _begin_session(self, path):
        """Close any previous project and install the new engine handle."""
        if self._fsp:
            try:
                self._fsp.close()
            except Exception:
                pass
            self._fsp = None
        # A wedged or already-dead engine can survive close(): reap it before a
        # new one is launched, or it stays invisible and licensed forever.
        self._reap_engines()
        try:
            engine = self._open_engine(path)
        except Exception as e:
            # Never leave a closed/stale handle behind: a dangling self._fsp
            # makes every later call fail with a confusing engine error.
            self._path = None
            self._autosave_path = None
            raise BridgeError('could not open %s: %s'
                              % (path or 'a new project', str(e)[:_ERR_MSG]))
        self._fsp = engine
        self._track_engine()
        self._path = path
        self._autosave_path = None
        self._session_generation = getattr(self, '_session_generation', 0) + 1

    def _cmd_session_open(self, p):
        path = p['path']
        self._begin_session(path)
        summary = {}
        for key in _SUMMARY_KEYS:
            try:
                summary[key] = self._fsp.getnamed('FDTD', key)
            except Exception:
                summary[key] = None
        return {'status': 'ok', 'path': path, 'summary': summary,
                'session_generation': self._session_generation,
                'engine': engine_facts()}

    def _cmd_session_close(self, p):
        if self._fsp:
            self._fsp.close()
        self._fsp = None
        self._path = None
        self._autosave_path = None
        reaped = self._reap_engines()
        result = {'status': 'closed'}
        if reaped.get('killed') or reaped.get('failed'):
            result['engine_reap'] = reaped
        return result

    def _cmd_session_save(self, p):
        self._require_project()
        path = p.get('path') or self._path
        if not path:
            raise BridgeError(
                'Project has no current file path. Pass path= to session_save.')
        try:
            self._fsp.save(path)
        except Exception as e:
            raise BridgeError('save failed: %s' % str(e)[:_ERR_MSG])
        self._path = path
        self._autosave_path = None
        return {'status': 'ok', 'path': path}

    def _cmd_session_new(self, p):
        """Create a blank FDTD project (no .fsp file needed).

        Optional FDTD region config: dimension, x/y/z span, simulation time,
        mesh accuracy. Applied keys are echoed back; rejected ones are reported
        instead of being swallowed.
        """
        self._begin_session(None)
        self._call_lsf('addfdtd')
        cfg = {}
        failed = {}
        for key in _FDTD_CONFIG_KEYS:
            value = p.get(key, p.get(key.replace(' ', '_')))
            if value is None:
                continue
            try:
                self._fsp.setnamed('FDTD', key, value)
                cfg[key] = value
            except Exception as e:
                failed[key] = str(e)[:_ERR_MSG]
        result = {'status': 'ok' if not failed else 'partial', 'config': cfg,
                  'session_generation': self._session_generation,
                  'engine': engine_facts()}
        if failed:
            result['config_failed'] = failed
        return result

    def _ensure_project_saved(self):
        """Make sure the project has a file path before run().

        Lumerical shows a modal 'Save As' dialog when running an unnamed project;
        with a hidden instance that dialog is invisible and wedges the solver.
        An unsaved project is therefore written to a throwaway temp path — kept
        in _autosave_path, deliberately NOT in _path, so a later
        session_save() with no path still reports "no current file path" instead
        of silently overwriting the temp file.
        """
        if self._path:
            return self._path
        if not self._autosave_path:
            self._autosave_path = os.path.join(
                self._tmp_dir, '_mcp_auto_%d.fsp' % os.getpid())
            self._fsp.save(self._autosave_path)
        return self._autosave_path

    def _run_result(self, saved):
        result = {'status': 'completed', 'saved_to': saved,
                  'session_generation': self._session_generation}
        if not self._path:
            result['note'] = ('Project was unsaved, so it was written to a '
                              'temporary path before solving. Use session_save '
                              'to keep it.')
        return result

    def _cmd_run(self, p):
        self._require_project()
        saved = self._ensure_project_saved()
        self._fsp.run()
        return self._run_result(saved)
