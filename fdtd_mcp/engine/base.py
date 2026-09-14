# -*- coding: utf-8 -*-
"""Shared bridge plumbing: object dispatch, LSF calls, value sanitising.

Split out of bridge.py; runs on Lumerical's bundled Python 3.6.8, so keep the
syntax 3.6-compatible (no f-strings, no type hints).
"""
from __future__ import print_function
import os
import re
import json

from .constants import (ERR_MSG as _ERR_MSG,
                        TRUNC as _TRUNC,
                        LSF_PROP_TYPES as _LSF_PROP_TYPES,
                        RUN_COMMAND as _RUN_COMMAND)
from .errors import BridgeError
from .lsf import lsf_quote, lsf_expr, TempVar, strip_comments_and_strings


class BridgeBase(object):
    """Mix-in base: everything every handler module may rely on."""

    def _require_project(self):
        """Reject a handler call when no FDTD project is open."""
        if not self._fsp:
            raise BridgeError('No open project')

    def _select_by_name(self, name):
        """Select an object, deterministically.

        Verified live on v202: after a group setup script runs, the engine's
        current scope/selection can be left inside that group, and `select()` on
        a bare name then returns without selecting anything — the next
        `get("type")` fails with the confusing 'in get, no items are currently
        selected'. Resetting the group scope first (as _traverse already does)
        and verifying the selection removes that class of failure.
        """
        if name != '::model':
            try:
                self._fsp.eval('groupscope("::model");')
            except Exception:
                pass
        try:
            self._fsp.select(name)
            self._call_lsf('get', 'type')   # confirm something really is selected
            return
        except Exception:
            if name.startswith('::'):
                raise
        self._fsp.select('::model::' + name)

    def _resolve_name(self, name):
        """Resolve a possibly-short object name to its full scoped path.

        Tries bare name first, then '::model::name' as fallback.
        Returns the resolved scoped name if found, or the original name.
        """
        if '::' in name:
            return name  # already scoped
        # Try bare name
        try:
            self._call_lsf('getnamed', name, 'type')
            return name
        except Exception:
            pass
        # Try ::model:: prefix
        scoped = '::model::' + name
        try:
            self._call_lsf('getnamed', scoped, 'type')
            return scoped
        except Exception:
            pass
        return name  # fallback: return original, let caller handle error

    def _call_lsf(self, func_name, *args):
        """Unified Lumerical function call.

        Auto-converts list->ndarray so MATLAB matrix marshalling stays correct.
        `lumapi` is imported here rather than at module import time so this
        module (and its tests) load without a Lumerical installation.
        """
        from lumapi import appCall
        self._lsf_call_count = getattr(self, '_lsf_call_count', 0) + 1
        converted = []
        for a in args:
            if isinstance(a, list) and len(a) > 0:
                try:
                    import numpy as np
                    arr = np.array(a)
                    if arr.dtype.kind in ('i', 'f', 'u'):
                        a = arr
                except Exception:
                    pass
            converted.append(a)
        return appCall(self._fsp, func_name, converted)

    def _lsf_str(self, value):
        """Quote a value for interpolation into an LSF snippet."""
        return lsf_quote(value)

    def _temp_var(self, hint='br'):
        """Context manager returning a collision-free LSF variable name."""
        return TempVar(self._fsp, hint)

    def _sanitize(self, value, cap=_TRUNC):
        """Sanitize values for JSON serialization.

        Preserves ndarray shape for small arrays (nested lists), represents
        complex values as [re, im] pairs (instead of '(1.5+0.001j)' strings),
        and reports shape/length/truncation for large arrays.
        """
        if value is None:
            return None
        if isinstance(value, (bool, int)):
            return value
        if isinstance(value, float):
            return None if value != value else value
        if isinstance(value, str):
            return value
        if isinstance(value, (list, tuple)):
            total = len(value)
            truncated = total > cap
            items = [self._sanitize(v, cap) for v in (value[:cap] if truncated else value)]
            if truncated:
                return {'data': items, 'length': total, 'truncated': True}
            return items
        if isinstance(value, dict):
            return {str(k): self._sanitize(v, cap) for k, v in value.items()}
        try:
            import numpy as np
            if isinstance(value, np.ndarray):
                if value.size <= cap:
                    data = value.tolist()
                    if value.dtype.kind == 'c':
                        data = self._complex_to_lists(data)
                    return {'data': data, 'shape': list(value.shape),
                            'length': value.size}
                flat = value.flatten()[:cap]
                if flat.dtype.kind == 'c':
                    data = [[float(v.real), _clean_imag(v.imag)]
                            for v in flat.tolist()]
                else:
                    data = flat.tolist()
                return {'data': data, 'shape': list(value.shape),
                        'length': value.size, 'truncated': True}
        except ImportError:
            pass
        try:
            return float(value)
        except (TypeError, ValueError):
            pass
        if isinstance(value, complex):
            return [value.real, _clean_imag(value.imag)]
        s = str(value)
        return s[:10000] if len(s) > 10000 else s

    def _complex_to_lists(self, data):
        """Recursively convert Python complex numbers to [re, im] pairs."""
        if isinstance(data, complex):
            return [data.real, _clean_imag(data.imag)]
        if isinstance(data, list):
            return [self._complex_to_lists(v) for v in data]
        return data


def _clean_imag(imag):
    """Report negligible imaginary parts as exactly 0.0.

    Lumerical stores matrices as complex doubles and leaves the imaginary part
    uninitialised for real-valued data, so reading e.g. "sampled 3d data" back
    yields values like 6.2e-307 — memory noise that looks like corruption. No
    FDTD quantity of interest lives near 1e-290, so anything below that (and any
    NaN) is reported as 0.0.
    """
    imag = float(imag)
    if imag != imag or abs(imag) < 1e-290:
        return 0.0
    return imag
