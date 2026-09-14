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

    def _cmd_material_set(self, p):
        """Set a material property.

        Args:
            name: material name
            property: property name, e.g. 'Refractive Index', 'nk data', 'mesh order'
            value: property value (number, string, or array for tabulated nk data)
        """
        self._require_project()
        name = p['name']
        prop = p['property']
        value = p['value']
        self._call_lsf('setmaterial', name, prop, value)
        return {'status': 'ok', 'name': name, 'property': prop}

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
