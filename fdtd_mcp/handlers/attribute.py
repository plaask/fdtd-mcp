# -*- coding: utf-8 -*-
"""Grid attributes (the GUI "Attributes" group).

Runs on Lumerical's bundled Python 3.6.8, so keep the syntax 3.6-compatible
(no f-strings, no type hints).

Verified live on v202: addgridattribute takes one of
  "lc orientation", "permittivity rotation", "matrix transform",
  "np density", "temperature"
and creates a root-level object named "LC attribute", "Rotation attribute",
"Matrix attribute", "np density", "temperature" respectively.
"""
from __future__ import print_function

from ..engine.base import BridgeBase
from ..engine.constants import ERR_MSG as _ERR_MSG
from ..engine.errors import BridgeError


class AttributeHandlers(BridgeBase):
    """Grid attribute creation."""

    # canonical kind -> (auto name, human label)
    KINDS = {
        'lc orientation': ('LC attribute', 'LC orientation'),
        'permittivity rotation': ('Rotation attribute', 'permittivity rotation'),
        'matrix transform': ('Matrix attribute', 'matrix transform'),
        'np density': ('np density', 'np density'),
        'temperature': ('temperature', 'temperature'),
    }

    ALIASES = {
        'lc': 'lc orientation',
        'liquid crystal': 'lc orientation',
        'liquid crystal orientation': 'lc orientation',
        'rotation': 'permittivity rotation',
        'permittivity': 'permittivity rotation',
        'matrix': 'matrix transform',
        'np': 'np density',
        'density': 'np density',
        'temp': 'temperature',
    }

    def _root_names(self):
        """Names of the root-level objects (grid attributes live there)."""
        self._fsp.eval('groupscope("::model"); selectall();')
        raw = self._call_lsf('getid')
        names = [n.strip() for n in str(raw or '').split('\n') if n.strip()]
        # getid returns full paths ("::model::LC attribute"); the tool reports
        # the short name, which is what select/set uses at root scope.
        return [n.rsplit('::', 1)[-1] for n in names]

    def _cmd_attribute_add(self, p):
        """Add one grid attribute; return its real name and object type."""
        self._require_project()
        raw_kind = str(p.get('kind', '')).strip().lower()
        kind = self.ALIASES.get(raw_kind, raw_kind)
        if kind not in self.KINDS:
            raise BridgeError(
                'Unknown grid attribute kind %r. Supported: %s'
                % (p.get('kind'), ', '.join(sorted(self.KINDS))))

        before = self._root_names()
        try:
            self._call_lsf('addgridattribute', kind)
        except Exception as e:
            raise BridgeError('addgridattribute(%s) failed: %s'
                              % (kind, str(e)[:_ERR_MSG]))

        after = self._root_names()
        created = [n for n in after if n not in before]
        name = created[0] if created else self.KINDS[kind][0]

        # The add* command normally selects the new object, but do not rely on
        # it: rename/verify through an explicit selection.
        try:
            self._select_by_name(name)
        except Exception:
            pass

        wanted = p.get('name')
        if wanted:
            try:
                self._call_lsf('set', 'name', wanted)
                name = wanted
            except Exception as e:
                raise BridgeError(
                    'grid attribute was created as "%s" but renaming to "%s" '
                    'failed: %s' % (name, wanted, str(e)[:_ERR_MSG]))

        obj_type = None
        try:
            obj_type = str(self._call_lsf('getnamed', name, 'type'))
        except Exception:
            pass
        return {'status': 'ok', 'name': name, 'type': obj_type,
                'kind': kind}
