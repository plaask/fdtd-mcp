# -*- coding: utf-8 -*-
"""Parameter sweep lifecycle and results.

Split out of bridge.py; runs on Lumerical's bundled Python 3.6.8, so keep the
syntax 3.6-compatible (no f-strings, no type hints).
"""
from __future__ import print_function
import os
import re
import json
import time

from ..engine.constants import (ERR_MSG as _ERR_MSG,
                               TRUNC as _TRUNC,
                               LSF_PROP_TYPES as _LSF_PROP_TYPES,
                               RUN_COMMAND as _RUN_COMMAND)
from ..engine.base import BridgeBase
from ..engine.errors import BridgeError


class SweepHandlers(BridgeBase):
    """Parameter sweep lifecycle and results."""

    def _cmd_sweep_get(self, p):
        """Get sweep configuration."""
        self._require_project()
        name = p['name']
        info = {'name': name}
        try:
            result = self._call_lsf('getsweepresult', name)
            info['exists'] = True
            info['has_results'] = True
            info['result_sample'] = str(result)[:_ERR_MSG] if result else 'empty'
        except Exception as e:
            err = str(e)
            if 'no results' in err.lower():
                info['exists'] = True
                info['has_results'] = False
                info['note'] = 'Sweep defined but not yet run'
            else:
                info['exists'] = False
                info['error'] = err[:_ERR_MSG]
        return info

    def _sweep_param_struct(self, param):
        """Build the Lumerical sweep-parameter struct from a tool param dict.

        Verified against the real engine (v202): addsweepparameter takes a
        struct with mixed-case keys (Name / parameter / type / start / stop);
        lumapi's appCall converts a Python dict to that struct. 'points' is a
        sweep-level property, not per-parameter.

        NOTE: the per-parameter 'type' field is the parameter VALUE data type
        (numeric code, or the string 'Number'), NOT the sampling type. The
        tool's 'type':'Linear'/'Logarithmic' is the sampling intent — the
        sweep samples start..stop with the sweep's number of points (Linear by
        default) — so sampling strings are dropped and numeric/'Number' values
        pass through.
        """
        s = {'Name': param.get('name')}
        for key in ('parameter', 'start', 'stop'):
            if key in param and param[key] is not None:
                s[key] = param[key]
        t = param.get('type')
        if isinstance(t, int) and not isinstance(t, bool):
            s['type'] = t
        elif isinstance(t, str) and t.strip().lower() == 'number':
            s['type'] = t.strip()
        return s

    def _sweep_result_struct(self, result):
        s = {'Name': result.get('name')}
        if result.get('result'):
            s['Result'] = result['result']
        return s

    def _verify_param_path(self, path):
        """Return None when a sweep parameter path resolves, else why it doesn't.

        Verified on v202: addsweepparameter happily accepts a path to a property
        that does not exist, so a typo only surfaces when the sweep runs. Paths
        look like '::model::substrate::z span' (object path + '::' + property);
        anything that does not split that way is reported as unverified rather
        than guessed at.
        """
        if not isinstance(path, str) or '::' not in path:
            return 'not a scoped "::model::object::property" path'
        obj_path, _, prop = path.rpartition('::')
        if not obj_path or not prop:
            return 'could not split into object path and property'
        try:
            self._call_lsf('getnamed', obj_path, prop)
            return None
        except Exception as e:
            return 'unresolved: ' + str(e)[:_ERR_MSG]

    def _cmd_sweep_add(self, p):
        """Add a new sweep with optional parameters and results."""
        self._require_project()
        sweep_type = p.get('type', 0)
        name = p.get('name', 'sweep')
        parameters = p.get('parameters', [])
        results = p.get('results', [])

        # addsweep returns the created sweep path (e.g. '::sweep', '::sweep1' —
        # the auto-name increments when sweeps already exist). Sweeps are NOT
        # model-tree objects: rename via the sweep API, not get/set on the
        # selection (addsweep does not auto-select).
        created_raw = self._call_lsf('addsweep', sweep_type)
        created_name = str(created_raw).split('::')[-1] if created_raw else 'sweep'
        if name:
            self._call_lsf('setsweep', created_name, 'name', name)

        param_errors = []
        param_warnings = []
        result_errors = []
        for param in parameters:
            if not isinstance(param, dict) or not param.get('name'):
                param_errors.append('invalid parameter dict: ' + str(param))
                continue
            pname = param['name']
            unresolved = self._verify_param_path(param.get('parameter'))
            if unresolved:
                param_warnings.append(
                    '%s -> %s (%s)' % (pname, param.get('parameter'), unresolved))
            try:
                self._call_lsf('addsweepparameter', name, self._sweep_param_struct(param))
            except Exception as e:
                param_errors.append(pname + ': add: ' + str(e)[:_ERR_MSG])
            if param.get('points'):
                try:
                    self._call_lsf('setsweep', name, 'number of points', param['points'])
                except Exception as e:
                    param_errors.append(pname + '.points: ' + str(e)[:_ERR_MSG])

        for result in results:
            if not isinstance(result, dict) or not result.get('name'):
                result_errors.append('invalid result dict: ' + str(result))
                continue
            rname = result['name']
            try:
                self._call_lsf('addsweepresult', name, self._sweep_result_struct(result))
            except Exception as e:
                result_errors.append(rname + ': add: ' + str(e)[:_ERR_MSG])

        result = {'status': 'ok' if not param_errors else 'partial',
                  'name': name, 'type': sweep_type,
                  'parameter_count': len(parameters),
                  'result_count': len(results),
                  'parameter_errors': param_errors,
                  'result_errors': result_errors}
        if param_warnings:
            # Kept separate from errors: the sweep was created, but the path did
            # not resolve now, so it will fail at run time.
            result['parameter_warnings'] = param_warnings
        return result

    def _cmd_sweep_set(self, p):
        """Set sweep properties."""
        self._require_project()
        name = p['name']
        failed = {}
        for prop, val in p.get('properties', {}).items():
            try:
                self._call_lsf('setsweep', name, prop, val)
            except Exception as e:
                failed[prop] = str(e)[:_ERR_MSG]
        result = {'status': 'ok' if not failed else 'error', 'name': name}
        if failed:
            result['properties_failed'] = sorted(failed)
            result['failures'] = failed
        return result

    def _cmd_sweep_delete(self, p):
        """Delete a sweep by name."""
        self._require_project()
        try:
            self._call_lsf('deletesweep', p['name'])
        except Exception as e:
            raise BridgeError('sweep_delete failed: %s' % str(e)[:_ERR_MSG])
        return {'status': 'ok', 'deleted': p['name']}

    def _cmd_sweep_run(self, p):
        self._require_project()
        saved = self._ensure_project_saved()
        try:
            self._fsp.runsweep(p['name'])
        except Exception as e:
            raise BridgeError('sweep_run failed: %s' % str(e)[:_ERR_MSG])
        return self._run_result(saved)

    def _cmd_sweep_result(self, p):
        self._require_project()
        try:
            if p.get('result'):
                r = self._call_lsf('getsweepresult', p['name'], p['result'])
            else:
                r = self._call_lsf('getsweepresult', p['name'])
        except Exception as e:
            raise BridgeError('sweep_result failed: %s' % str(e)[:_ERR_MSG])
        # Payload shape is unchanged (the caller asked for data, not metadata);
        # failures already surface through the error channel above.
        return self._sanitize(r)
