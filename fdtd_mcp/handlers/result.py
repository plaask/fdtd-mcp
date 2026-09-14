# -*- coding: utf-8 -*-
"""Monitor result discovery and extraction.

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
from ..engine.lsf import lsf_quote


class ResultHandlers(BridgeBase):
    """Monitor result discovery and extraction."""

    _COMMON_RESULT_FIELDS = [
        'f', 'lambda', 'x', 'y', 'z',
        'Ex', 'Ey', 'Ez', 'Hx', 'Hy', 'Hz',
        'T', 'R', 'P', 'power', 'T_total',
        'index', 'index_x', 'index_y', 'index_z',
        'intensity', 'E', 'H',
        'Px', 'Py', 'Pz', 'sigma', 'S', 'Sx', 'Sy', 'Sz',
        'n', 'k', 'epsilon', 'temp', 'heat',
        'T_forward', 'T_backward', 'R_forward', 'R_backward',
    ]

    def _cmd_result_list(self, p):
        """List result datasets AND probe field names in one call.

        With no monitor given this reports the FDTD object's own datasets; with
        `all=true` it first walks the object tree and reports every object that
        currently has results (haveresult), which is what "list all monitors
        with results" needs.
        """
        self._require_project()
        name = p.get('monitor') or p.get('name')

        if not name and p.get('all'):
            monitors = self._monitors_with_results()
            if monitors:
                return {'monitors': monitors, 'monitor_count': len(monitors),
                        'note': 'Pass monitor="<name>" to list its datasets.'}
            return {'monitors': [], 'monitor_count': 0,
                    'note': 'No object currently returns results — run the '
                            'simulation first. (v202 haveresult is true for '
                            'objects that merely could hold results, so it is '
                            'confirmed with getresult here.)'}

        name = name or 'FDTD'

        # Get result datasets
        try:
            r = self._call_lsf('getresult', name)
            datasets = str(r).split('\n') if r else []
        except Exception as e:
            return {'name': name, 'datasets': [], 'error': str(e)[:_ERR_MSG]}

        # Probe fields for each dataset
        datasets_with_fields = []
        for dataset in datasets:
            dataset = dataset.strip()
            if not dataset:
                continue
            try:
                with self._temp_var('brrl') as var:
                    self._fsp.eval('%s = getresult(%s,%s);'
                                   % (var, lsf_quote(name), lsf_quote(dataset)))
                    fields = []
                    for cf in self._COMMON_RESULT_FIELDS:
                        try:
                            v = self._fsp.getv(var + '.' + cf)
                            if v is not None:
                                fields.append(cf)
                        except Exception:
                            pass
                datasets_with_fields.append({'data': dataset, 'fields': fields,
                                             'field_count': len(fields)})
            except Exception as e:
                datasets_with_fields.append({'data': dataset,
                                             'error': str(e)[:_ERR_MSG]})

        return {'monitor': name, 'datasets': datasets_with_fields,
                'dataset_count': len(datasets_with_fields)}

    def _monitors_with_results(self):
        """[{monitor, type, datasets}] for objects that actually have results.

        Verified live on v202: `haveresult` returns true for any object that
        *can* hold results (sources and monitors included) even before a solve,
        so it is only a cheap pre-filter here — every candidate is confirmed
        with getresult before being reported.
        """
        objects = []
        self._traverse('::model', ['type'], True, objects, set())
        monitors = []
        for obj in objects:
            obj_name = obj.get('name')
            if not obj_name:
                continue
            try:
                if not self._call_lsf('haveresult', obj_name):
                    continue
            except Exception:
                continue
            try:
                datasets = self._call_lsf('getresult', obj_name)
            except Exception:
                continue
            entry = {'monitor': obj_name, 'type': obj.get('type')}
            if datasets:
                entry['datasets'] = [d.strip() for d in str(datasets).split('\n')
                                     if d.strip()]
            monitors.append(entry)
        return monitors

    def _cmd_result_get(self, p):
        """Fetch result data for specific fields from a monitor.

        Args:
            monitor: monitor name
            data: optional result data name (e.g. 'T', 'R')
            fields: list of field names to retrieve (REQUIRED)
            cap: max elements per field (default 2000)
        """
        self._require_project()
        monitor = p['monitor']
        data = p.get('data', '')
        fields = p.get('fields', None)
        if not fields:
            raise BridgeError('fields parameter is required. '
                              'Use result_list first to discover available fields.')
        cap = int(p.get('cap', 2000))

        # The scratch variable is per-call and always cleared (an engine-global
        # name like the old 'brd' clobbered user variables and leaked on error).
        values = {}
        truncated = {}
        with self._temp_var('brd') as var:
            try:
                if data:
                    self._fsp.eval('%s = getresult(%s,%s);'
                                   % (var, lsf_quote(monitor), lsf_quote(data)))
                else:
                    self._fsp.eval('%s = getresult(%s);'
                                   % (var, lsf_quote(monitor)))
            except Exception as e:
                raise BridgeError('could not load results from %s: %s'
                                  % (monitor, str(e)[:_ERR_MSG]))

            for field in fields:
                try:
                    sanitized = self._sanitize(self._fsp.getv(var + '.' + field), cap)
                    values[field] = sanitized
                    if isinstance(sanitized, dict) and sanitized.get('truncated'):
                        truncated[field] = sanitized.get('length')
                except Exception as e:
                    values[field] = {'error': str(e)[:_ERR_MSG]}

        result = {'monitor': monitor, 'data': data, 'values': values,
                  'fields_requested': fields, 'truncated': truncated}
        missing = [f for f in fields
                   if isinstance(values.get(f), dict) and 'error' in values[f]]
        if missing:
            result['fields_failed'] = missing
            result['status'] = 'partial'
        else:
            result['status'] = 'ok'
        return result

    def _cmd_result_save(self, p):
        self._require_project()
        monitor, data = p['monitor'], p.get('data', '')
        out = p.get('output')
        if not out:
            safe_mon = re.sub(r'[^\w.-]+', '_', monitor).strip('_') or 'monitor'
            safe_data = re.sub(r'[^\w.-]+', '_', data or 'result').strip('_') or 'result'
            out = os.path.join(self._tmp_dir,
                               'result_%s_%s.mat' % (safe_mon, safe_data))
        with self._temp_var('brd') as var:
            try:
                if data:
                    self._fsp.eval('%s = getresult(%s,%s);'
                                   % (var, lsf_quote(monitor), lsf_quote(data)))
                else:
                    self._fsp.eval('%s = getresult(%s);'
                                   % (var, lsf_quote(monitor)))
                self._fsp.eval('matlabsave(%s, %s);' % (lsf_quote(out), var))
            except Exception as e:
                raise BridgeError('result_save failed: %s' % str(e)[:_ERR_MSG])
        return {'file': out, 'status': 'ok'}

    def _cmd_result_has(self, p):
        """Check whether a monitor/element has results without throwing.

        Only 'haveresult' exists on v202 — findresult/hasresult/listresults do
        not exist there (verified live), so they are not probed. Note that on
        v202 haveresult is true for objects that *can* hold results even before
        a solve, so `exists: true` means "worth calling result_get", not
        "data is present".
        """
        self._require_project()
        name = p.get('monitor') or p.get('name')
        try:
            r = self._call_lsf('haveresult', name)
            if r is not None:
                return {'name': name, 'exists': bool(r),
                        'check_method': 'haveresult',
                        'note': 'exists=true means the object can hold results; '
                                'confirm with result_get/result_list after a run.'}
        except Exception:
            pass
        # Fallback: try getresult and see if it succeeds
        try:
            r = self._call_lsf('getresult', name)
            return {'name': name, 'exists': r is not None and str(r).strip() != '',
                    'check_method': 'getresult_attempt'}
        except Exception:
            return {'name': name, 'exists': False, 'check_method': 'exception_caught'}
