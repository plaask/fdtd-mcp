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


# Editable properties of each sweep kind on v202, from ?setsweep(...).
# Verified live on v202: the parameter sweep lists
#   name / type / number of points / resave files after analysis
# and does NOT list "solver" (setsweep(...,"solver",...) returns 0.0).
_SWEEP_DEFAULT_PROPERTIES = {
    0: ['name', 'type', 'number of points', 'resave files after analysis'],
    1: ['name', 'algorithm', 'maximum generations', 'reset random generator',
        'type', 'generation size', 'tolerance', 'first generation script',
        'next generation script', 'use figure of merit script',
        'figure of merit script'],
    2: ['name', 'number of trials', 'variation', 'seed', 'enable seed',
        'individual trial', 'enable individual trial',
        'enable spatial correlations'],
    3: ['name', 'excite all ports', 'calculate group delay', 'invert sign',
        'map from', 'active', 'port', 'mode', 'map vector', 'auto symmetry',
        'export setup'],
}


def _split_props(raw):
    """Turn the newline-separated property list from setsweep/getsweep into a list."""
    return [p.strip() for p in str(raw or '').split('\n') if p.strip()]


def _sweep_type_from_props(props):
    """Infer the sweep kind from the editable property names on v202."""
    lower = set(p.lower() for p in (props or []))
    if 'algorithm' in lower:
        return 1
    if 'number of trials' in lower:
        return 2
    if 'excite all ports' in lower:
        return 3
    return 0


def _default_props_for_type(sweep_type):
    return list(_SWEEP_DEFAULT_PROPERTIES.get(sweep_type,
                                              _SWEEP_DEFAULT_PROPERTIES[0]))


def _parameter_names(props, sweep_type):
    defaults = set(p.lower() for p in _default_props_for_type(sweep_type))
    return [p for p in (props or []) if p.lower() not in defaults]


class SweepHandlers(BridgeBase):
    """Parameter sweep lifecycle and results."""

    def _sweep_properties(self, name):
        """Return the editable property names of a sweep.

        getsweep() is the documented reader; setsweep(name) is the same list on
        v202 and is kept as a fallback for builds where getsweep is absent.
        Raises when neither can read the sweep (missing sweep / engine error).
        """
        last = None
        for func in ('getsweep', 'setsweep'):
            try:
                props = _split_props(self._call_lsf(func, name))
                if props:
                    return props
            except Exception as e:
                last = e
        if last is not None:
            raise last
        raise BridgeError('sweep "%s" not found or has no readable properties'
                          % name)

    def _port_group_present(self):
        """True when FDTD has a ports group (i.e. at least one port was added)."""
        for path in ('FDTD::ports', '::model::FDTD::ports'):
            try:
                self._call_lsf('getnamed', path, 'type')
                return True
            except Exception:
                pass
        return False

    def _cmd_sweep_get(self, p):
        """Get sweep configuration.

        Returns the editable property names (from getsweep/setsweep), the
        inferred sweep kind, parameter names, and whether results exist.
        """
        self._require_project()
        name = p['name']
        info = {'name': name, 'exists': False, 'has_results': False}
        try:
            props = self._sweep_properties(name)
        except Exception as e:
            err = str(e)
            info['error'] = err[:_ERR_MSG]
            if 'no results' in err.lower():
                # A defined-but-unrun sweep is still a sweep.
                info['exists'] = True
                info['note'] = 'Sweep defined but not yet run'
            return info
        info['exists'] = True
        info['properties'] = props
        sweep_type = _sweep_type_from_props(props)
        info['sweep_type'] = sweep_type
        info['parameter_names'] = _parameter_names(props, sweep_type)

        try:
            result = self._call_lsf('getsweepresult', name)
            info['has_results'] = True
            info['result_sample'] = str(result)[:_ERR_MSG] if result else 'empty'
        except Exception as e:
            err = str(e)
            if 'no results' in err.lower():
                info['has_results'] = False
                info['note'] = 'Sweep defined but not yet run'
            else:
                info['has_results'] = False
                info['result_error'] = err[:_ERR_MSG]
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
        try:
            sweep_type = int(p.get('type', 0))
        except (TypeError, ValueError):
            raise BridgeError('type must be an integer 0..3 '
                              '(0=parameter, 1=optimization, 2=Monte Carlo, '
                              '3=S-parameter).')
        if sweep_type not in (0, 1, 2, 3):
            raise BridgeError('type must be one of 0 (parameter), 1 (optimization), '
                              '2 (Monte Carlo), 3 (S-parameter); got %r.'
                              % (p.get('type'),))
        name = p.get('name', 'sweep')
        parameters = p.get('parameters') or []
        results = p.get('results') or []
        if not isinstance(parameters, list):
            raise BridgeError('parameters must be a list of dicts')
        if not isinstance(results, list):
            raise BridgeError('results must be a list of dicts')

        # addsweep returns the created sweep path (e.g. '::sweep', '::sweep1' —
        # the auto-name increments when sweeps already exist). Sweeps are NOT
        # model-tree objects: rename via the sweep API, not get/set on the
        # selection (addsweep does not auto-select).
        created_raw = self._call_lsf('addsweep', sweep_type)
        created_name = str(created_raw).split('::')[-1] if created_raw else 'sweep'
        warnings = []
        if not created_raw:
            warnings.append('addsweep did not return a name; assuming "%s". '
                            'With more than one sweep the rename may target the '
                            'wrong one — call sweep_get to verify.' % created_name)
        if name:
            try:
                self._call_lsf('setsweep', created_name, 'name', name)
            except Exception as e:
                warnings.append('rename to "%s" failed: %s'
                                % (name, str(e)[:_ERR_MSG]))

        param_errors = []
        param_warnings = []
        result_errors = []
        point_values = set()
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
                point_values.add(param['points'])
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

        if len(point_values) > 1:
            param_warnings.append(
                'different "points" values were given (%s); "number of points" '
                'is a single sweep-level property, so the last one won.'
                % ', '.join(str(v) for v in sorted(point_values)))

        if sweep_type == 3 and not self._port_group_present():
            warnings.append(
                'S-parameter sweep was created without a port group. Add ports '
                'with model_add(type="port") before sweep_run, or the sweep '
                'cannot run.')

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
        if warnings:
            result['warnings'] = warnings
        return result

    def _cmd_sweep_set(self, p):
        """Set sweep properties.

        Verified live on v202: setsweep does NOT raise for an unknown property —
        it returns 0.0. Ignoring that return value turned every typo into a
        silent success, so every property is checked against the sweep's real
        editable list and against the returned status.
        """
        self._require_project()
        name = p['name']
        properties = p.get('properties') or {}
        if not isinstance(properties, dict):
            raise BridgeError('properties must be a dict of property=value pairs')

        try:
            editable = self._sweep_properties(name)
        except Exception as e:
            raise BridgeError('sweep "%s" not found or unreadable: %s'
                              % (name, str(e)[:_ERR_MSG]))
        if not editable:
            raise BridgeError('sweep "%s" has no readable properties.' % name)

        lower = dict((prop.lower(), prop) for prop in editable)
        failed = {}
        applied = []
        unverified = []
        target = name

        for prop, val in properties.items():
            key = lower.get(str(prop).lower())
            if key is None:
                failed[prop] = (
                    'Lumerical does not expose "%s" on this sweep. Editable '
                    'properties: %s' % (prop, ', '.join(editable)))
                continue
            try:
                r = self._call_lsf('setsweep', target, key, val)
            except Exception as e:
                failed[prop] = str(e)[:_ERR_MSG]
                continue

            # v202 returns 1.0 on success and 0.0 on failure (no exception).
            # None means the engine gave no status; keep it visible rather than
            # calling it a success.
            if r is None:
                ok = True
                unverified.append(prop)
            elif isinstance(r, bool):
                ok = r
            elif isinstance(r, (int, float)):
                ok = float(r) != 0.0
            else:
                ok = str(r).strip().lower() not in ('', '0', '0.0', 'false')

            if not ok:
                failed[prop] = (
                    'Lumerical rejected "%s" (setsweep returned %r). Editable '
                    'properties: %s' % (key, r, ', '.join(editable)))
                continue
            applied.append(prop)
            if key.lower() == 'name':
                target = val
                lower = dict((x.lower(), x) for x in editable)
                lower['name'] = 'name'

        any_failure = bool(failed)
        status = 'ok' if not any_failure else ('partial' if applied else 'error')
        result = {'status': status, 'name': target,
                  'properties_set': len(applied),
                  'applied': applied,
                  'editable_properties': editable}
        if unverified:
            result['unverified'] = unverified
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
        started = time.time()
        try:
            self._fsp.runsweep(p['name'])
        except Exception as e:
            raise BridgeError('sweep_run failed: %s' % str(e)[:_ERR_MSG])
        # Best-effort: queued/remote sweeps may not have written a log yet.
        return self._run_result(saved, started)

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
