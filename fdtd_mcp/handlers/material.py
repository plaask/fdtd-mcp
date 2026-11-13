# -*- coding: utf-8 -*-
"""Material database CRUD.

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


class MaterialHandlers(BridgeBase):
    """Material database CRUD."""

    def _cmd_material_add(self, p):
        """Create a new material from a model type template.

        Args:
            type: material model type, e.g. 'Sampled 3D data', 'Dielectric', 'Drude'
        Returns the auto-generated material name.
        """
        self._require_project()
        mat_type = p.get('type', 'Sampled 3D data')
        name = self._call_lsf('addmaterial', mat_type)
        return {'status': 'ok', 'name': str(name), 'type': mat_type}

    @staticmethod
    def _is_complex_pair(cell):
        """A JSON-safe complex number: [real, imag] (both plain numbers)."""
        return (isinstance(cell, (list, tuple)) and len(cell) == 2
                and all(isinstance(v, (int, float)) and not isinstance(v, bool)
                        for v in cell))

    def _normalise_sampled_data(self, value):
        """Turn the JSON form of sampled data into (rows, has_complex, width).

        Accepted cell forms: number, "re+imi" string, or [re, im]. The
        official format is frequency (Hz) in column 1 and COMPLEX PERMITTIVITY
        in the remaining columns (2 = isotropic, 4 = diagonal anisotropic).
        """
        if isinstance(value, dict):
            value = value.get('data', value)
            if isinstance(value, dict):
                value = value.get('data')
        if not isinstance(value, (list, tuple)) or not value:
            raise BridgeError('sampled 3d data must be a non-empty list of rows')
        rows = []
        width = None
        has_complex = False
        for row in value:
            if not isinstance(row, (list, tuple)):
                raise BridgeError('each sampled-data row must be a list')
            cells = []
            for cell in row:
                if self._is_complex_pair(cell):
                    cells.append(complex(float(cell[0]), float(cell[1])))
                    has_complex = True
                elif isinstance(cell, bool):
                    raise BridgeError('boolean is not valid sampled data')
                elif isinstance(cell, (int, float)):
                    cells.append(cell)
                elif isinstance(cell, str):
                    try:
                        cells.append(complex(cell.strip().replace('i', 'j')))
                    except ValueError:
                        raise BridgeError(
                            'could not parse sampled-data cell %r as a number or '
                            'complex literal' % (cell,))
                    has_complex = True
                else:
                    raise BridgeError(
                        'unsupported sampled-data cell %r (use a number, '
                        '"re+imi", or [re, im])' % (cell,))
            if width is None:
                width = len(cells)
            elif len(cells) != width:
                raise BridgeError('sampled-data rows have different lengths')
            rows.append(cells)
        return rows, has_complex, width

    def _cmd_material_set(self, p):
        """Set a material property.

        Args:
            name: material name
            property: property name, e.g. 'Refractive Index', 'sampled 3d data'
            value: property value (number, string, or sampled-data matrix)
        """
        self._require_project()
        name = p['name']
        prop = p['property']
        value = p['value']
        sampled = str(prop).strip().lower() == 'sampled 3d data'

        if not sampled:
            self._call_lsf('setmaterial', name, prop, value)
            return {'status': 'ok', 'name': name, 'property': prop}

        rows, has_complex, width = self._normalise_sampled_data(value)
        if width not in (2, 4):
            raise BridgeError(
                'sampled 3d data takes 2 columns [f, eps] (isotropic) or 4 '
                'columns [f, eps_xx, eps_yy, eps_zz] (diagonal anisotropic); '
                'got %d columns. The first column is frequency in Hz and the '
                'remaining columns are complex permittivity (eps = n^2).'
                % width)

        import numpy as np
        arr = np.array(rows, dtype=complex if has_complex else float)
        self._call_lsf('setmaterial', name, prop, arr)

        result = {'status': 'ok', 'name': name, 'property': prop,
                  'rows': len(rows), 'columns': width,
                  'complex': bool(has_complex)}
        # Read back: a silent no-op is the failure mode this tool exists to
        # avoid. A shape mismatch is reported instead of claiming success.
        try:
            got = self._call_lsf('getmaterial', name, prop)
            shape = list(getattr(got, 'shape', []) or [])
            if shape and shape != [len(rows), width]:
                result['status'] = 'partial'
                result['readback_shape'] = shape
                result['warning'] = ('material data was written but read back '
                                     'with shape %s, expected [%d, %d]'
                                     % (shape, len(rows), width))
        except Exception as e:
            result['readback_error'] = str(e)[:_ERR_MSG]
        return result

    def _cmd_material_index(self, p):
        """Sample a material's complex refractive index at given frequencies.

        Anisotropic "sampled 3d data" is [f, eps_xx, eps_yy, eps_zz]; getindex
        only reports the xx component, so this tool interpolates the requested
        diagonal component and returns n, k and epsilon.
        """
        self._require_project()
        import numpy as np
        name = p['name']
        comp = str(p.get('component') or 'xx').strip().lower()
        if comp not in ('xx', 'yy', 'zz'):
            raise BridgeError('component must be xx, yy or zz')
        wavelengths = p.get('wavelengths')
        frequencies = p.get('frequencies')
        if wavelengths and frequencies:
            raise BridgeError('pass wavelengths= or frequencies=, not both')

        try:
            raw = self._call_lsf('getmaterial', name, 'sampled 3d data')
            source = 'sampled 3d data'
        except Exception:
            try:
                n0 = self._call_lsf('getmaterial', name, 'Refractive Index')
            except Exception as e:
                raise BridgeError(
                    'material "%s" has neither sampled 3d data nor a constant '
                    'Refractive Index: %s' % (name, str(e)[:_ERR_MSG]))
            source = 'Refractive Index'
            n0 = complex(n0)
            eps0 = n0 * n0
            if wavelengths:
                freqs = [299792458.0 / float(w) for w in wavelengths]
            elif frequencies:
                freqs = [float(x) for x in frequencies]
            else:
                freqs = []
            if not freqs:
                return {'name': name, 'source': source, 'component': comp,
                        'constant': True,
                        'n': n0.real, 'k': n0.imag,
                        'epsilon_real': eps0.real,
                        'epsilon_imag': eps0.imag}
            return {
                'name': name, 'source': source, 'component': comp,
                'frequencies': freqs,
                'wavelengths': [299792458.0 / f if f else None for f in freqs],
                'n': [n0.real for _ in freqs],
                'k': [n0.imag for _ in freqs],
                'epsilon_real': [eps0.real for _ in freqs],
                'epsilon_imag': [eps0.imag for _ in freqs],
            }

        try:
            arr = np.asarray(raw, dtype=complex)
        except (TypeError, ValueError):
            raise BridgeError('material "%s" sampled data is not numeric' % name)
        if arr.ndim != 2 or arr.shape[1] not in (2, 4):
            raise BridgeError(
                'material "%s" sampled data has shape %s; expected Nx2 or Nx4'
                % (name, list(arr.shape)))
        f = np.asarray(arr[:, 0].real, dtype=float)
        if arr.shape[1] == 2:
            eps = np.asarray(arr[:, 1], dtype=complex)
        else:
            eps = np.asarray(arr[:, 1 + ('xx', 'yy', 'zz').index(comp)],
                             dtype=complex)
        order = np.argsort(f)
        f, eps = f[order], eps[order]

        if wavelengths:
            target = np.asarray([299792458.0 / float(w) for w in wavelengths],
                                dtype=float)
        elif frequencies:
            target = np.asarray([float(x) for x in frequencies], dtype=float)
        else:
            target = f
        eps_re = np.interp(target, f, eps.real, left=np.nan, right=np.nan)
        eps_im = np.interp(target, f, eps.imag, left=np.nan, right=np.nan)
        n = np.sqrt(eps_re + 1j * eps_im)

        def _nums(values):
            out = []
            for v in values:
                v = float(v)
                out.append(None if v != v else v)
            return out

        return {
            'name': name, 'source': source, 'component': comp,
            'frequencies': [float(x) for x in target],
            'wavelengths': [299792458.0 / float(x) if x else None
                            for x in target],
            'n': _nums(n.real),
            'k': _nums(n.imag),
            'epsilon_real': _nums(eps_re),
            'epsilon_imag': _nums(eps_im),
            'data_rows': int(len(f)),
            'data_columns': int(arr.shape[1]),
            'note': ('4-column data is diagonal anisotropic [f, eps_xx, eps_yy, '
                     'eps_zz]; values outside the tabulated range are null.'
                     if arr.shape[1] == 4 else
                     '2-column data is isotropic [f, eps].'),
        }

    def _cmd_material_get(self, p):
        """Read material properties.

        Args:
            name: material name
            property: optional property name; if omitted, lists all property names
        """
        self._require_project()
        name = p['name']
        prop = p.get('property', None)
        if prop:
            result = self._call_lsf('getmaterial', name, prop)
        else:
            result = self._call_lsf('getmaterial', name)
        return self._sanitize(result)

    def _cmd_material_delete(self, p):
        """Delete a material by name."""
        self._require_project()
        self._call_lsf('deletematerial', p['name'])
        return {'status': 'ok', 'deleted': p['name']}

    def _cmd_material_exists(self, p):
        """Check if a material exists."""
        self._require_project()
        exists = self._call_lsf('materialexists', p['name'])
        return {'name': p['name'], 'exists': bool(exists)}
