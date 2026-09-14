# -*- coding: utf-8 -*-
"""Object library insertion (structure groups / analysis groups).

Split out of handlers/model.py: `model_add` creates empty primitives from a
fixed type table, while a library item is a whole `.fsp` copied in and
re-executed by the engine (`addobject`). See engine/library.py for the verified
behaviour and the catalog layout.
"""
from __future__ import print_function

from ..engine.base import BridgeBase
from ..engine.constants import ERR_MSG as _ERR_MSG
from ..engine.errors import BridgeError
from ..engine import library


class LibraryHandlers(BridgeBase):
    """Inserting Lumerical object-library items into the open project."""

    def _scene_names(self):
        """Every object name currently in the model (cheap recursive walk)."""
        objects = []
        # `seen` must be a set — _traverse mutates it with .add()
        self._traverse('::model', ['type'], False, objects, set())
        return set(o['name'] for o in objects)

    def _cmd_library_insert(self, p):
        self._require_project()
        item_id = (p.get('name') or p.get('id') or '').strip()
        if not item_id:
            raise BridgeError(
                'library_insert needs name=<catalog id>. Call library_list first '
                'to see the available ids (e.g. "Qanalysis", "ring_resonator").')

        entry = library.find(item_id)
        if entry is None:
            hint = library.suggest(item_id)
            raise BridgeError(
                'No object-library item with id "%s"%s. Call library_list for the '
                'full list.' % (item_id,
                                ('. Did you mean: ' + ', '.join(hint)) if hint else ''))
        if not entry.get('installed'):
            raise BridgeError(
                'The catalog lists "%s" but its project file is missing (%s) — '
                'the install is incomplete.' % (entry['id'], entry.get('file')))

        try:
            before = self._scene_names()
        except Exception:
            before = set()

        # addobject acts on the CURRENT group scope. A walk (or a group script)
        # can leave the engine inside a group, and an item inserted there is
        # rolled back by the engine — verified live: inserting while the scope
        # sat inside "transmission_calc" produced no object at all. Always
        # insert at the model root, like execute does.
        self._reset_to_model_root()
        try:
            self._call_lsf('addobject', entry['id'])
        except Exception as e:
            raise BridgeError('addobject("%s") failed: %s'
                              % (entry['id'], str(e)[:_ERR_MSG]))

        try:
            diff = sorted(self._scene_names() - before)
        except Exception:
            diff = []

        # A name diff can contain phantoms (an insert the engine rolled back is
        # still visible for a moment), so every reported object must prove it
        # exists. Reporting "ok" for a no-op is the worst failure mode here.
        created, phantoms = [], []
        for name in diff:
            try:
                self._call_lsf('getnamed', name, 'type')
                created.append(name)
            except Exception:
                phantoms.append(name)

        if not created:
            raise BridgeError(
                'addobject("%s") added nothing — the engine rolled the item back '
                '(it may need a different context, or the item is not standalone). '
                'The project is unchanged.' % entry['id'])

        result = {
            'status': 'ok' if not phantoms else 'partial',
            'library_id': entry['id'],
            'library_type': entry['type'],
            'category': entry['category'],
            'created': created,
        }
        if phantoms:
            result['created_unverified'] = phantoms
            result['note'] = ('Some names disappeared right after the insert; only '
                              'the objects that still exist are listed as created.')
        return result
