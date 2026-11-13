# -*- coding: utf-8 -*-
"""Object-tree CRUD, properties and scripts.

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
from ..engine.capabilities import (ANALYSIS_RESULT_FUNCTION, IMPORT_KINDS,
                                   OBJECT_TYPES, user_prop_function)
from ..engine.errors import BridgeError
from ..engine.lsf import lsf_quote


class ModelHandlers(BridgeBase):
    """Object-tree CRUD, properties and scripts."""

    _SCENE_PROPS = ['type','x','y','z','x span','y span','z span','z min','z max',
                     'material','index','enabled','wavelength start','wavelength stop',
                     'monitor type','frequency points','wavelength center','wavelength span',
                     'dx','dy','dz','theta','phi','injection axis','direction',
                     'polarization angle','dipole type','amplitude',
                     'output Ex','output Ey','output Ez','output Hx','output Hy','output Hz',
                     'output power','x min bc','x max bc','y min bc','y max bc',
                     'z min bc','z max bc','pml layers','simulation time','dt',
                     'auto shutoff min','mesh accuracy',
                     # Geometry written by primitives whose size is not x/y/z span
                     # (verified live on v202: Ring uses radii, Pyramid uses
                     # bottom/top spans, Waveguide uses base width/height/angle,
                     # Polygon uses vertices).
                     'radius','inner radius','outer radius',
                     'base width','base height','base angle',
                     'x span bottom','x span top','y span bottom','y span top',
                     'vertices','rotation 1','rotation 2','rotation 3',
                     # Override toggles gate other properties (e.g. "mesh order"
                     # is inactive until the material-database override is on).
                     'mesh order','override mesh order from material database',
                     'use relative coordinates']

    _LIGHT_PROPS = ['type', 'enabled', 'material', 'x', 'y', 'z']

    def _traverse(self, scope, prop_list, enabled_only, result_list, seen):
        """Recurse into scope, appending discovered objects to result_list.

        Always resets to ::model before navigating, and ALWAYS restores the root
        scope afterwards: descending into groups used to leave the engine scoped
        inside the last group visited, so a later `addrect`/`addobject` created
        its object inside that group instead of at the model root (verified live
        with library_insert, and the same class of bug as the old execute_file).
        """
        try:
            self._traverse_scope(scope, prop_list, enabled_only, result_list, seen)
        finally:
            self._reset_to_model_root()

    def _traverse_scope(self, scope, prop_list, enabled_only, result_list, seen):
        """Worker for _traverse — recurses into itself, not through the guard."""
        # Navigate to target scope from root
        self._fsp.eval('groupscope("::model");')
        if scope != '::model':
            parts = scope.replace('::model::', '').split('::')
            for part in parts:
                if part:
                    self._fsp.eval('groupscope(%s);' % lsf_quote(part))
        self._fsp.eval('selectall();')
        ids_raw = self._call_lsf('getid')
        ids_str = str(ids_raw) if ids_raw else ''
        if not ids_str:
            return
        for obj_id in ids_str.split('\n'):
            obj_id = obj_id.strip()
            if not obj_id or obj_id in seen:
                continue
            seen.add(obj_id)
            try:
                t = self._call_lsf('getnamed', obj_id, 'type')
                obj = {'name': obj_id, 'type': str(t)}
                for prop in prop_list:
                    try:
                        obj[prop] = self._sanitize(
                            self._call_lsf('getnamed', obj_id, prop))
                    except Exception:
                        pass
                # enabled_only filter (if enabled property was read)
                if enabled_only:
                    en = obj.get('enabled')
                    if en is not None and not en:
                        # Skip disabled objects (but still recurse groups)
                        if str(t) in ('Structure Group', 'Analysis Group'):
                            self._traverse_scope(scope + '::' + obj_id, prop_list,
                                                 enabled_only, result_list, seen)
                        continue
                result_list.append(obj)
                try:
                    self._seen_objects.add(obj_id)
                except AttributeError:
                    pass
                if str(t) in ('Structure Group', 'Analysis Group'):
                    self._traverse_scope(scope + '::' + obj_id, prop_list,
                                         enabled_only, result_list, seen)
            except Exception:
                pass

    def _classify_source(self, t):
        """Return explicit source_kind label from object type string."""
        if not t:
            return None
        s = t.lower()
        if 'dipole' in s:
            return 'dipole'
        if 'tfsf' in s:
            return 'tfsf'
        if 'plane' in s:
            return 'plane'
        if 'gaussian' in s:
            return 'gaussian'
        if 'mode' in s:
            return 'mode-source'
        return None

    def _cmd_model_info(self, p):
        """One-call self-introspection: objects + variables + materials + FDTD summary.

        Designed to stop the LLM from skipping steps — everything needed for
        modeling + script writing in one dict. Scripts pulled separately via get_script.
        """
        self._require_project()
        enabled_only = bool(p.get('enabled_only', True))
        include_full = bool(p.get('include_full', False))

        prop_list = self._SCENE_PROPS if include_full else self._LIGHT_PROPS

        # 1) Objects (light unless include_full=true)
        objects = []
        seen = set()
        self._traverse('::model', prop_list, enabled_only, objects, seen)

        # 2) Model variables (values + declared type codes, when queryable)
        variables = self._enum_model_variables('::model')
        var_types = self._query_user_props('::model')

        # 3) Materials — list known materials via probing
        materials = []
        try:
            raw = self._call_lsf('getmaterial')
            if raw:
                raw_str = str(raw)
                for m in raw_str.replace('\n', ',').split(','):
                    m = m.strip()
                    if m and m != '():':
                        materials.append(m)
        except Exception:
            pass

        # 4) FDTD summary
        fdtd_summary = {}
        for k in ['dimension', 'x span', 'y span', 'z span',
                  'simulation time', 'mesh accuracy']:
            try:
                fdtd_summary[k] = self._fsp.getnamed('FDTD', k)
            except Exception:
                pass

        return {
            'objects': objects,
            'object_count': len(objects),
            'model_variables': variables,
            'model_variable_types': var_types,
            'variable_count': len(variables),
            'materials': materials,
            'fdtd_summary': fdtd_summary,
        }

    @staticmethod
    def _as_float(value):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _cmd_stack_report(self, p):
        """Geometry/PML audit for a layer stack (read-only).

        Answers the questions that matter when a simulation diverges or a layer
        silently disappears: which layers exist, their z extents, overlaps and
        gaps, layers thinner than 1 nm, layers outside the FDTD region, and
        structures that fill the transverse PML volume.
        """
        self._require_project()
        include_disabled = bool(p.get('include_disabled', False))
        props = ['type', 'enabled', 'material', 'mesh order',
                 'x span', 'y span', 'z', 'z span', 'z min', 'z max']
        objects = []
        self._traverse('::model', props, not include_disabled, objects, set())

        fdtd = {}
        for key in ('x span', 'y span', 'z span', 'z',
                    'x min bc', 'x max bc', 'y min bc', 'y max bc',
                    'z min bc', 'z max bc', 'pml layers'):
            try:
                fdtd[key] = self._sanitize(self._fsp.getnamed('FDTD', key))
            except Exception:
                pass
        fz = self._as_float(fdtd.get('z')) or 0.0
        fzs = self._as_float(fdtd.get('z span'))
        fzmin = fz - fzs / 2.0 if fzs else None
        fzmax = fz + fzs / 2.0 if fzs else None
        fxspan = self._as_float(fdtd.get('x span'))
        fyspan = self._as_float(fdtd.get('y span'))
        xbc = str(fdtd.get('x min bc', '') or '') + ' ' + str(fdtd.get('x max bc', '') or '')
        ybc = str(fdtd.get('y min bc', '') or '') + ' ' + str(fdtd.get('y max bc', '') or '')

        tol = 1e-12
        layers = []
        # Only physical layers belong in a stack report: skip solvers, groups,
        # sources/monitors/ports, mesh regions and grid attributes.
        skip_tokens = ('FDTD', 'STACK', 'RCWA', 'Structure Group',
                       'Analysis Group', 'Container Group', 'Layer Builder',
                       'Mesh', 'Monitor', 'Source', 'Port', 'Dipole', 'TFSF',
                       'Plane Wave', 'Gaussian', 'Attribute', 'Field Region')
        for obj in objects:
            obj_type = str(obj.get('type') or '')
            if any(tok in obj_type for tok in skip_tokens):
                continue
            zs = self._as_float(obj.get('z span'))
            z = self._as_float(obj.get('z'))
            z0 = self._as_float(obj.get('z min'))
            z1 = self._as_float(obj.get('z max'))
            if z0 is None or z1 is None:
                if z is None:
                    z = 0.0
                if zs is None:
                    continue
                z0, z1 = z - zs / 2.0, z + zs / 2.0
            if z1 < z0:
                z0, z1 = z1, z0
            thickness = z1 - z0
            xs = self._as_float(obj.get('x span'))
            ys = self._as_float(obj.get('y span'))
            warns = []
            if thickness < 1e-9:
                warns.append('thinner than 1 nm; verify mesh resolution')
            if fzmin is not None and (z0 < fzmin - tol or z1 > fzmax + tol):
                warns.append('extends outside the FDTD z range')
            if (xs is not None and ys is not None and fxspan and fyspan
                    and xs >= fxspan - tol and ys >= fyspan - tol
                    and ('PML' in xbc.upper() or 'PML' in ybc.upper())):
                warns.append('fills the transverse PML volume (x/y spans match '
                             'the FDTD region while x/y use PML)')
            layers.append({
                'name': obj.get('name'), 'type': obj.get('type'),
                'material': obj.get('material'), 'mesh_order': obj.get('mesh order'),
                'enabled': obj.get('enabled', True),
                'z_min': z0, 'z_max': z1, 'thickness': thickness,
                'warnings': warns,
            })
        layers.sort(key=lambda x: (x['z_min'], x['z_max']))

        overlaps = []
        gaps = []
        for prev, cur in zip(layers, layers[1:]):
            if cur['z_min'] < prev['z_max'] - tol:
                overlaps.append({'below': prev['name'], 'above': cur['name'],
                                 'overlap': prev['z_max'] - cur['z_min']})
            elif cur['z_min'] > prev['z_max'] + tol:
                gaps.append({'below': prev['name'], 'above': cur['name'],
                             'gap': cur['z_min'] - prev['z_max']})

        warnings = []
        for layer in layers:
            for w in layer['warnings']:
                warnings.append('%s: %s' % (layer['name'], w))
        return {
            'fdtd': fdtd,
            'fdtd_z_min': fzmin,
            'fdtd_z_max': fzmax,
            'layer_count': len(layers),
            'layers': layers,
            'overlaps': overlaps,
            'gaps': gaps,
            'warnings': warnings,
        }

    def _root_object_names(self):
        """Names of root-level objects (after an import/creation diff)."""
        self._fsp.eval('groupscope("::model"); selectall();')
        raw = self._call_lsf('getid')
        return [n.strip() for n in str(raw or '').split('\n') if n.strip()]

    def _cmd_import_add(self, p):
        """Import GDS/STL/surface/binary/nk/STEP data (v202-gated)."""
        self._require_project()
        kind = str(p.get('kind') or 'gds').strip().lower()
        spec = IMPORT_KINDS.get(kind)
        if spec is None:
            raise BridgeError('Unknown import kind %r. Supported: %s'
                              % (p.get('kind'),
                                 ', '.join(sorted(IMPORT_KINDS))))
        if kind == 'gds':
            return self._import_gds(p)
        if kind == 'stl':
            return self._import_stl(p)
        if kind in ('surface', 'binary', 'nk'):
            return self._import_primitive(kind, p)
        if kind == 'step':
            return self._import_step(p)
        raise BridgeError(spec.get('notes') or
                          'import kind "%s" is not available on this engine'
                          % kind)

    @staticmethod
    def _reject_import_options(p, allowed, kind):
        for key in ('cell', 'layer', 'zmin', 'zmax', 'scale_factor',
                    'vertex_radius', 'debug', 'file_units', 'x0', 'y0', 'z0',
                    'upper_surface', 'invert_xy', 'reverse_index_order'):
            if key not in allowed and p.get(key) is not None:
                raise BridgeError('%s is not valid for kind="%s"' % (key, kind))

    def _delete_quietly(self, name):
        try:
            self._fsp.eval('select(%s); delete();' % lsf_quote(name))
        except Exception:
            pass

    def _import_gds(self, p):
        self._reject_import_options(p, {'cell', 'layer', 'zmin', 'zmax'}, 'gds')
        path = p['path']
        if p.get('cell') is None or p.get('layer') is None:
            raise BridgeError('kind="gds" requires cell= and layer=')
        cell = p['cell']
        try:
            layer = int(p['layer'])
        except (TypeError, ValueError):
            raise BridgeError('layer must be an integer')
        material = p.get('material')
        zmin = p.get('zmin')
        zmax = p.get('zmax')
        if (zmin is None) != (zmax is None):
            raise BridgeError('pass both zmin and zmax, or neither')
        if zmin is not None and not material:
            raise BridgeError('the zmin/zmax form of gdsimport requires material=')

        # gdsimport creates at the current group scope; always import at root.
        try:
            self._fsp.eval('groupscope("::model");')
        except Exception:
            pass
        before = set(self._root_object_names())
        try:
            if zmin is not None:
                count = self._call_lsf('gdsimport', path, cell, layer,
                                       material, zmin, zmax)
            elif material:
                count = self._call_lsf('gdsimport', path, cell, layer, material)
            else:
                count = self._call_lsf('gdsimport', path, cell, layer)
        except Exception as e:
            raise BridgeError('gdsimport failed: %s' % str(e)[:_ERR_MSG])
        after = self._root_object_names()
        created = [n for n in after if n not in before]
        warnings = []
        if p.get('name') and created:
            try:
                self._call_lsf('setnamed', created[0], 'name', p['name'])
                created[0] = p['name']
            except Exception as e:
                warnings.append('rename failed: %s' % str(e)[:_ERR_MSG])
        try:
            count = int(count)
        except (TypeError, ValueError):
            count = len(created)
        result = {'status': 'ok' if not warnings else 'partial',
                  'kind': 'gds', 'count': count, 'created': created[:50]}
        if warnings:
            result['warnings'] = warnings
        return result

    def _import_stl(self, p):
        self._reject_import_options(
            p, {'scale_factor', 'vertex_radius', 'debug'}, 'stl')
        path = p['path']
        args = [path]
        scale = p.get('scale_factor')
        vertex = p.get('vertex_radius')
        debug = bool(p.get('debug', False))
        if scale is not None or vertex is not None or debug:
            args.append(1e-6 if scale is None else scale)
        if vertex is not None or debug:
            args.append(1e-12 if vertex is None else vertex)
        if debug:
            args.append(True)

        try:
            self._fsp.eval('groupscope("::model");')
        except Exception:
            pass
        before = set(self._root_object_names())
        try:
            self._call_lsf('stlimport', *args)
        except Exception as e:
            raise BridgeError('stlimport failed: %s' % str(e)[:_ERR_MSG])
        after = self._root_object_names()
        created = [n for n in after if n not in before]

        warnings = []
        material = p.get('material')
        for i, obj_name in enumerate(created):
            if i == 0 and p.get('name'):
                try:
                    self._call_lsf('setnamed', obj_name, 'name', p['name'])
                    obj_name = p['name']
                    created[i] = obj_name
                except Exception as e:
                    warnings.append('rename failed: %s' % str(e)[:_ERR_MSG])
            if material:
                try:
                    self._call_lsf('setnamed', obj_name, 'material', material)
                except Exception as e:
                    warnings.append('%s material: %s'
                                    % (obj_name, str(e)[:_ERR_MSG]))
        result = {'status': 'ok' if not warnings else 'partial',
                  'kind': 'stl', 'count': len(created),
                  'created': created[:50]}
        if warnings:
            result['warnings'] = warnings
        return result

    def _import_primitive(self, kind, p):
        if kind == 'surface':
            allowed = {'file_units', 'x0', 'y0', 'z0',
                       'upper_surface', 'invert_xy'}
        else:
            allowed = {'file_units', 'x0', 'y0', 'z0',
                       'reverse_index_order'}
        self._reject_import_options(p, allowed, kind)
        path = p['path']
        units = str(p.get('file_units') or 'm')
        x0 = float(p.get('x0') or 0.0)
        y0 = float(p.get('y0') or 0.0)
        z0 = float(p.get('z0') or 0.0)
        upper = int(p.get('upper_surface', 1) or 0)
        invert = int(p.get('invert_xy', 0) or 0)
        reverse = int(p.get('reverse_index_order', 0) or 0)

        try:
            self._fsp.eval('groupscope("::model");')
        except Exception:
            pass
        try:
            self._call_lsf('addimport')
        except Exception as e:
            raise BridgeError('addimport failed: %s' % str(e)[:_ERR_MSG])
        try:
            imp = str(self._call_lsf('get', 'name'))
        except Exception:
            imp = 'import'
        if p.get('name'):
            try:
                self._call_lsf('set', 'name', p['name'])
                imp = p['name']
            except Exception as e:
                raise BridgeError('created import "%s" but renaming failed: %s'
                                  % (imp, str(e)[:_ERR_MSG]))

        try:
            if kind == 'surface':
                r = self._call_lsf('importsurface', path, upper, units,
                                   x0, y0, z0, invert)
            elif kind == 'binary':
                r = self._call_lsf('importbinary', path, units,
                                   x0, y0, z0, reverse)
            else:
                r = self._call_lsf('importnk', path, units,
                                   x0, y0, z0, reverse)
        except Exception as e:
            self._delete_quietly(imp)
            raise BridgeError('%s failed: %s' % (kind, str(e)[:_ERR_MSG]))
        try:
            failed = r is not None and float(r) == 0.0
        except (TypeError, ValueError):
            failed = False
        if failed:
            self._delete_quietly(imp)
            raise BridgeError('%s reported failure (returned 0)' % kind)

        warnings = []
        material = p.get('material')
        if material and kind != 'nk':
            try:
                self._call_lsf('setnamed', imp, 'material', material)
            except Exception as e:
                warnings.append('material: %s' % str(e)[:_ERR_MSG])
        result = {'status': 'ok' if not warnings else 'partial',
                  'kind': kind, 'name': imp, 'created': [imp]}
        if warnings:
            result['warnings'] = warnings
        return result

    def _import_step(self, p):
        self._reject_import_options(p, {'scale_factor'}, 'step')
        path = p['path']
        args = [path]
        scale = p.get('scale_factor')
        if scale is not None:
            args.append(scale)
        try:
            self._fsp.eval('groupscope("::model");')
        except Exception:
            pass
        before = set(self._root_object_names())
        try:
            self._call_lsf('stepimport', *args)
        except Exception as e:
            msg = str(e)
            if 'not a valid function' in msg:
                raise BridgeError(
                    'stepimport is not available on this engine (v202 lacks '
                    'it); use the GUI or a newer Lumerical version.')
            raise BridgeError('stepimport failed: %s' % msg[:_ERR_MSG])
        after = self._root_object_names()
        created = [n for n in after if n not in before]
        warnings = []
        for i, obj_name in enumerate(created):
            if i == 0 and p.get('name'):
                try:
                    self._call_lsf('setnamed', obj_name, 'name', p['name'])
                    created[i] = p['name']
                except Exception as e:
                    warnings.append('rename failed: %s' % str(e)[:_ERR_MSG])
            if p.get('material'):
                try:
                    self._call_lsf('setnamed', obj_name, 'material',
                                   p['material'])
                except Exception as e:
                    warnings.append('%s material: %s'
                                    % (obj_name, str(e)[:_ERR_MSG]))
        result = {'status': 'ok' if not warnings else 'partial',
                  'kind': 'step', 'count': len(created),
                  'created': created[:50]}
        if warnings:
            result['warnings'] = warnings
        return result

    def _cmd_model_get(self, p):
        """Get full properties of ONE named object with type discriminator.
        For groups and ::model, also returns scripts and variables.
        """
        self._require_project()
        name = self._resolve_name(p['name'])
        include_all = bool(p.get('include_all_properties', False))
        out = {'name': name}
        try:
            t = self._call_lsf('getnamed', name, 'type')
            t_str = str(t)
            out['type'] = t_str
            sk = self._classify_source(t_str)
            if sk:
                out['source_kind'] = sk
        except Exception as e:
            raise BridgeError('Object not found: %s (%s)' % (name, str(e)[:_ERR_MSG]))
        for prop in self._SCENE_PROPS:
            try:
                out[prop] = self._sanitize(
                    self._call_lsf('getnamed', name, prop))
            except Exception:
                pass
        # User-declared properties (model variables, group parameters) are not in
        # _SCENE_PROPS — without this a property created by model_set would be
        # invisible to model_get.
        for uprop in self._query_user_props(name):
            try:
                out[uprop] = self._sanitize(self._call_lsf('getnamed', name, uprop))
            except Exception:
                pass
        if include_all:
            # v202's getnamed(name) with no property returns the full property
            # name list; read each one so model_get can mirror the Edit dialog.
            try:
                raw = self._call_lsf('getnamed', name)
                all_props = [x.strip() for x in str(raw or '').split('\n')
                             if x.strip()]
            except Exception:
                all_props = []
            for prop in all_props:
                if prop in out:
                    continue
                try:
                    out[prop] = self._sanitize(
                        self._call_lsf('getnamed', name, prop))
                except Exception:
                    pass
            out['all_properties_count'] = len(all_props)
        # For groups and ::model, also include scripts and variables
        t_str = out.get('type', str(t)) if 'type' in out else ''
        if name == '::model' or t_str in ('Analysis Group', 'Structure Group'):
            try:
                scripts = self._cmd_get_script({'name': name})
                out['scripts'] = scripts
            except Exception:
                pass
            if name == '::model':
                try:
                    variables = self._enum_model_variables('::model')
                    if variables:
                        out['variables'] = variables
                except Exception:
                    pass
        return out

    def _cmd_model_add(self, p):
        """Add any object type to the model tree."""
        self._require_project()
        obj_type = p['type']
        name = p.get('name', None)
        properties = p.get('properties', {})
        scope = p.get('scope', '::model')

        # Single source: engine/capabilities.py::OBJECT_TYPES. Verified live on
        # v202 that addfield/adddftmonitor do not exist; every frequency-domain
        # monitor (power / DFT / field) is an addpower object configured through
        # "monitor type" + its output switches.
        spec = OBJECT_TYPES.get(obj_type)
        if not spec:
            raise RuntimeError('Unknown object type: ' + obj_type +
                               '. Use one of: ' + ', '.join(sorted(OBJECT_TYPES)))
        func = spec['add']

        # Verified live on v202: a port is a child of the FDTD solver's
        # "ports" group, so addport fails without a solver region.
        if spec.get('prereq') == 'fdtd':
            try:
                self._call_lsf('getnamed', 'FDTD', 'type')
            except Exception:
                raise BridgeError('port requires an FDTD solver region; '
                                  'add model_add(type="fdtd") first')

        # Navigate scope (always restored, even when creation fails).
        try:
            if scope != '::model':
                self._fsp.eval('groupscope(%s);' % lsf_quote(scope))

            # Create object (Lumerical add* commands auto-select after creation,
            # no name arg).
            self._call_lsf(func)
            # Get auto-generated name from the now-selected object
            try:
                created_name = str(self._call_lsf('get', 'name'))
            except Exception:
                created_name = obj_type

            # Rename if user specified a name
            if name and created_name != name:
                self._call_lsf('set', 'name', name)
                created_name = name

            # Set initial properties. Failures are reported, never swallowed:
            # a silent no-op here made "model_add(...) -> ok" a lie.
            failures = {}
            if properties:
                type_codes = p.get('property_types') or {}
                for prop, val in properties.items():
                    ok, reason = self._set_object_prop(
                        created_name, prop, val, type_codes.get(prop))
                    if not ok:
                        failures[prop] = reason
        finally:
            # Return to root
            if scope != '::model':
                try:
                    self._fsp.eval('groupscope("::model");')
                except Exception:
                    pass

        result = {'status': 'ok', 'name': created_name, 'type': obj_type}
        if failures:
            result['properties_failed'] = sorted(failures)
            result['failures'] = failures
        return result

    def _cmd_model_delete(self, p):
        """Delete an object by name."""
        self._require_project()
        name = self._resolve_name(p['name'])
        self._fsp.eval('select(%s); delete();' % lsf_quote(name))
        return {'status': 'ok', 'deleted': name}

    def _lsf_type_code(self, value, explicit=None):
        """Resolve the INTEGER property-type code for adduserprop/addanalysisprop.

        An explicit code (int) or type name (e.g. 'length') wins, otherwise the
        code is inferred from the Python value. Verified live on v202: only the
        integer form is accepted — the string form ('Length') raises
        "argument list for adduserprop command is incorrect".
        """
        if isinstance(explicit, bool):
            explicit = None            # bool is an int subclass; never a code
        if isinstance(explicit, int):
            return explicit if 0 <= explicit <= 5 else 0
        if isinstance(explicit, str) and explicit.strip():
            key = explicit.strip().lower()
            if key in _LSF_PROP_TYPES:
                return _LSF_PROP_TYPES[key]
            try:
                code = int(key)
                if 0 <= code <= 5:
                    return code
            except ValueError:
                pass
        if isinstance(value, str):
            return _LSF_PROP_TYPES['text']
        return _LSF_PROP_TYPES['number']

    def _create_user_prop(self, obj, prop, value, type_code=None):
        """Declare a new parameter/property on ::model or a group.

        Verified live on v202: addvar/setvar/getvar are gone and setnamed cannot
        create a property ("in setnamed, the requested property was not found").
        The supported route is addanalysisprop / adduserprop, called with the
        target SELECTED and an INTEGER type code — the model root counts as a
        group for this purpose.

        Which function: the model root and analysis groups go through
        addanalysisprop, because their "Edit analysis group" dialog lists the
        ANALYSIS properties in its Analysis > Variables table (a user property
        created with adduserprop does NOT show up there — verified in the GUI).
        Structure groups use adduserprop.

        Returns (ok, reason); reason is None on success.
        """
        try:
            obj_type = str(self._call_lsf('getnamed', obj, 'type'))
        except Exception as e:
            return False, 'type lookup failed: ' + str(e)[:_ERR_MSG]

        func = user_prop_function(obj, obj_type)
        if func is None:
            return False, (
                'type "%s" has no parameter API: a new property can only be '
                'declared on the model root (::model), a Structure Group or an '
                'Analysis Group' % obj_type)

        code = self._lsf_type_code(value, type_code)
        try:
            self._select_by_name(obj)
            self._call_lsf(func, prop, code, value)
        except Exception as e:
            return False, '%s(%s) failed: %s' % (func, prop, str(e)[:_ERR_MSG])
        # Confirm: a silently ignored declaration must not look like success.
        # (The two namespaces share names, so an existing "gap" turns a new
        # declaration into "gap2" — reading the requested name back catches it.)
        try:
            self._call_lsf('getnamed', obj, prop)
        except Exception:
            return False, ('%s reported success but "%s" is still not readable '
                           '(a property namespace collision may have created a '
                           'suffixed clone)' % (func, prop))
        return True, None

    def _add_analysis_result(self, obj, result_name):
        """Declare an analysis result (the Results table of that dialog).

        Verified live on v202: `addanalysisresult("name")` takes ONE argument on
        the model root — the 2/3-argument forms used inside analysis scripts
        fail there.
        """
        try:
            self._select_by_name(obj)
            self._call_lsf('addanalysisresult', result_name)
        except Exception as e:
            return False, 'addanalysisresult(%s) failed: %s' % (
                result_name, str(e)[:_ERR_MSG])
        return True, None

    def _set_object_prop(self, obj, prop, value, type_code=None):
        """Set one property, creating it first when it does not exist yet.

        1) setnamed handles every pre-existing (active) property on any object.
        2) A property that is known but INACTIVE reports a targeted reason: the
           caller has to switch its "override ..." toggle on first.
        3) Otherwise the property does not exist, so declare it with
           adduserprop/addanalysisprop (model root, Structure or Analysis Group
           only) and verify.

        Returns (ok, reason); reason is None on success.
        """
        try:
            self._call_lsf('setnamed', obj, prop, value)
            return True, None
        except Exception as e:
            setnamed_err = str(e)[:_ERR_MSG]

        # Verified live on v202: writing "mesh order" on a primitive that has not
        # overridden the material-database value fails with "the requested
        # property is inactive" — the property exists, so it must NOT be reported
        # as missing (and no user property can shadow it).
        if 'inactive' in setnamed_err.lower():
            return False, (
                setnamed_err + ' | the property exists but is inactive: switch '
                'its "override ..." toggle on first, in the SAME call and BEFORE '
                'the dependent property (e.g. {"override global monitor '
                'settings": 1, "frequency points": 51} for a monitor, or '
                '{"override mesh order from material database": 1, '
                '"mesh order": 2} for a primitive)')

        ok, reason = self._create_user_prop(obj, prop, value, type_code)
        if ok:
            return True, None
        if setnamed_err and reason:
            return False, setnamed_err + ' | ' + reason
        return False, setnamed_err or reason

    def _cmd_model_set(self, p):
        """Set parameters/properties on an object.

        Batch form takes 'properties' (dict) and/or 'results' (list of analysis
        result names), single form takes 'property'/'value'. The object type is
        auto-detected (see _set_object_prop): a name that does not exist yet is
        declared on the model root and on Structure/Analysis Groups, so that
        model parameters show up in the Analysis > Variables table and results in
        the Results table of the GUI's "Edit analysis group" dialog.
        """
        self._require_project()
        if 'properties' in p or 'results' in p:
            obj = self._resolve_name(p.get('name', '::model'))
            type_codes = p.get('property_types') or {}
            set_count = 0
            failed = []
            failures = {}
            for prop, value in (p.get('properties') or {}).items():
                ok, reason = self._set_object_prop(
                    obj, prop, value, type_codes.get(prop))
                if ok:
                    set_count += 1
                else:
                    failed.append(prop)
                    failures[prop] = reason

            results_added = []
            results_failed = {}
            for result_name in (p.get('results') or []):
                ok, reason = self._add_analysis_result(obj, result_name)
                if ok:
                    results_added.append(result_name)
                else:
                    results_failed[result_name] = reason

            any_failure = bool(failed or results_failed)
            any_success = bool(set_count or results_added)
            if not any_failure:
                status = 'ok'
            elif any_success:
                status = 'partial'
            else:
                status = 'error'
            out = {'status': status, 'object': obj,
                   'properties_set': set_count}
            if p.get('results') is not None:
                out['results_added'] = results_added
            if failed:
                out['properties_failed'] = failed
                out['failures'] = failures
                out['hint'] = (
                    'Only pre-existing properties can be updated on ordinary '
                    'objects. New parameters can be declared on the model root '
                    '("::model"), a Structure Group or an Analysis Group — '
                    'model_set does that automatically (the model root and '
                    'analysis groups use addanalysisprop, which is what their '
                    'Analysis > Variables table lists). Pass '
                    'property_types={"name": "length"} to override the inferred '
                    'type code (0=number, 1=text, 2=length, 3=time, 4=frequency, '
                    '5=material). Note that property names are shared between '
                    'the user- and analysis-property namespaces: declaring a '
                    'name that already exists creates a suffixed clone '
                    '("gap" -> "gap2").')
            if results_failed:
                out['results_failed'] = sorted(results_failed)
                out['result_failures'] = results_failed
            return out
        # Support both new API (name=object, property=prop) and old API
        # (name=prop, object=target).
        if 'object' in p:
            obj = p['object']
            prop = p['name']
        else:
            obj = p.get('name', '::model')
            prop = p.get('property', p.get('name'))
        value = p['value']
        ok, reason = self._set_object_prop(
            obj, prop, value, p.get('property_type'))
        if not ok:
            raise BridgeError('could not set "%s" on %s: %s'
                              % (prop, obj, reason))
        return {'status': 'ok', 'object': obj, 'property': prop, 'value': value}

    def _cmd_get_script(self, p):
        self._require_project()
        name = p.get('name', '::model')
        result = {}
        self._select_by_name(name)

        # ::model + Analysis Group have setup/analysis scripts.
        # Structure Group has a single 'script' property.
        if name == '::model':
            props = [('setup script','setup'), ('analysis script','analysis')]
        else:
            obj_type = str(self._call_lsf('get', 'type'))
            if obj_type == 'Analysis Group':
                props = [('setup script','setup'), ('analysis script','analysis')]
            elif obj_type == 'Structure Group':
                props = [('script','script')]
            else:
                props = []

        for stype, suffix in props:
            key = stype.replace(' ','_')
            try:
                # Write to temp file from Lumerical side to avoid C-layer encoding issues.
                # write() appends to existing files — remove first to avoid stale accumulation.
                tmp_path = self._tmp_dir + '/_br_' + suffix + '.txt'
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
                self._fsp.eval(
                    'write(%s, get(%s));'
                    % (lsf_quote(tmp_path), lsf_quote(stype))
                )
                content = None
                for enc in ['utf-8', 'gbk', 'gb2312', 'gb18030']:
                    try:
                        with open(tmp_path, 'r', encoding=enc) as f:
                            content = f.read()
                        break
                    except (UnicodeDecodeError, LookupError):
                        continue
                if content is None:
                    with open(tmp_path, 'r', encoding='utf-8',
                              errors='replace') as f:
                        content = f.read()
                result[key] = content
            except Exception as e:
                result[key] = None
                result[key+'_error'] = str(e)[:_ERR_MSG]
        return result

    def _cmd_set_script(self, p):
        """Set setup/analysis script on an object. Supports multi-line content.

        ::model and Analysis Group use 'setup script' / 'analysis script'.
        Structure Group uses a single 'script' property.
        """
        self._require_project()
        name = p.get('name', '::model')
        stype = p.get('script_type') or p.get('type')  # 'setup' or 'analysis'
        content = p['content']

        self._select_by_name(name)
        if name == '::model':
            script_prop = stype + ' script'
        else:
            obj_type = str(self._call_lsf('get', 'type'))
            if obj_type == 'Analysis Group':
                script_prop = stype + ' script'
            else:
                script_prop = 'script'

        self._call_lsf('set', script_prop, content)
        return {'status': 'ok', 'object': name, 'script_type': script_prop}

    def _cmd_model_script(self, p):
        """Unified script access. Action: 'get' (default) or 'set'.

        For 'get': returns scripts for the named object.
        For 'set': requires 'type' ('setup'/'analysis') and 'content'.
        """
        action = p.get('action', 'get')
        if action == 'get':
            return self._cmd_get_script(p)
        elif action == 'set':
            return self._cmd_set_script(p)
        else:
            raise RuntimeError('Unknown action: "' + action + '". Use "get" or "set".')

    def _query_user_props(self, scope):
        """Return {name: type_code} declared on a group / the model root.

        v202 exposes queryuserprop() (no args — acts on the current selection)
        for the model root and queryanalysisprop(obj) for analysis groups; both
        return {'name': [...], 'type': [...]}. Anything else raises, which is
        reported as an empty mapping so callers can fall back.
        """
        try:
            self._select_by_name(scope)
        except Exception:
            return {}
        for call in (('queryuserprop',), ('queryanalysisprop', scope)):
            try:
                raw = self._call_lsf(*call)
            except Exception:
                continue
            if not isinstance(raw, dict) or raw.get('name') is None:
                continue
            names = raw.get('name')
            types = raw.get('type')
            if not isinstance(names, (list, tuple)):
                names = [names]
            if types is None:
                types = [None] * len(names)
            elif not isinstance(types, (list, tuple)):
                types = [types]
            out = {}
            for i, n in enumerate(names):
                if n:
                    out[str(n)] = (str(types[i]).strip()
                                   if i < len(types) and types[i] is not None
                                   else None)
            if out:
                return out
        return {}

    def _scan_script_vars(self, scope):
        """Fallback enumeration: regex the scripts for property declarations."""
        names = []
        try:
            scr = self._cmd_get_script({'name': scope})
            text = ((scr.get('setup_script') or '') + '\n'
                    + (scr.get('analysis_script') or '') + '\n'
                    + (scr.get('script') or ''))
            names = re.findall(r'add(?:userprop|analysisprop)\(\s*"(\w+)"', text)
            names += re.findall(r'addvar\(\s*"(\w+)"', text)
        except Exception:
            pass
        return list(set(names))

    def _enum_model_variables(self, scope='::model'):
        """Enumerate the model root's variables as {name: value}.

        Primary source is queryuserprop() on the selected model root (verified on
        v202: it lists variables created in the GUI *and* via adduserprop, which
        the old script-regex scan could not see). The script scan stays as a
        fallback for objects/versions where the query is unavailable.
        """
        names = list(self._query_user_props(scope).keys())
        if not names:
            names = self._scan_script_vars(scope)

        # Resolve values via getnamed (getv as fallback)
        out = {}
        for vn in names:
            if not vn:
                continue
            val = None
            try:
                val = self._call_lsf('getnamed', scope, vn)
                val = self._sanitize(val)
            except Exception:
                try:
                    val = self._fsp.getv(vn)
                    val = self._sanitize(val)
                except Exception:
                    pass
            if val is not None:
                out[vn] = val
        return out
