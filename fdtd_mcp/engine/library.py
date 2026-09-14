# -*- coding: utf-8 -*-
"""Lumerical object library: `Resources/fdtd-library` + its `catalog.xml`.

The GUI's "Object library" browser is nothing more than this folder: one `.fsp`
per library item plus a catalog that names them. A library item is inserted into
the current project with

    addobject("<catalog id>")

which copies the object(s) in and RE-RUNS the item's setup script — verified
live on v202:

  * `addobject("ring_resonator")` -> Structure Group "ring resonator" with 8
    child waveguides (geometry already built),
  * `addobject("Qanalysis")`      -> Analysis Group "Qanalysis" with 4 time
    monitors created by its setup script, `analysis script` intact,
  * `addobject("sphere")`         -> a plain Sphere (some library items are
    primitives, not groups).

Left out on purpose: a stale install may publish catalog entries whose `.fsp`
is missing, so `catalog()` reports `installed` per item and the insert tool
refuses an item whose file is not there instead of failing inside the engine.

Python 3.6.8 compatible (the bridge imports this too): stdlib only.
"""
from __future__ import print_function

import os
import xml.etree.ElementTree as ET

LIBRARY_SUBDIR = os.path.join('Resources', 'fdtd-library')
_CACHE = {}


def library_dir(home=None):
    """Path of the object library, or None when the install has none."""
    home = home or os.environ.get('LUMERICAL_HOME', '')
    if not home:
        return None
    path = os.path.join(home, LIBRARY_SUBDIR)
    return path if os.path.isdir(path) else None


def catalog(home=None, refresh=False):
    """Every catalog entry: [{'id','type','category','file','summary',...}].

    'type' is Lumerical's own split — 'Structures' or 'Analysis' — which is what
    decides whether the insert produces a structure group or an analysis group.
    """
    home = home or os.environ.get('LUMERICAL_HOME', '')
    if not refresh and home in _CACHE:
        return _CACHE[home]
    base = library_dir(home)
    items = []
    if base:
        try:
            root = ET.parse(os.path.join(base, 'catalog.xml')).getroot()
        except Exception:
            root = None
        if root is not None:
            for node in root.iter('simObject'):
                item_id = (node.findtext('id') or '').strip()
                if not item_id:
                    continue
                filename = (node.findtext('projectFileUrl') or '').strip()
                items.append({
                    'id': item_id,
                    'type': (node.findtext('type') or '').strip(),
                    'category': (node.findtext('categoryName') or '').strip(),
                    'file': filename,
                    'summary': (node.findtext('shortDescription') or '').strip(),
                    'installed': bool(filename) and os.path.isfile(
                        os.path.join(base, filename)),
                })
    items.sort(key=lambda i: (i['category'], i['id']))
    _CACHE[home] = items
    return items


def find(item_id, home=None):
    """Catalog entry for an exact id (case-insensitive), else None."""
    low = (item_id or '').strip().lower()
    if not low:
        return None
    for item in catalog(home):
        if item['id'].lower() == low:
            return item
    return None


def suggest(item_id, home=None, limit=6):
    """Close ids for a typo message (substring first, then similarity)."""
    low = (item_id or '').strip().lower().replace(' ', '_')
    items = catalog(home)
    if not low:
        return []
    hits = [i['id'] for i in items
            if low in i['id'].lower() or i['id'].lower() in low]
    if not hits:
        try:
            import difflib
            hits = difflib.get_close_matches(
                low, [i['id'] for i in items], n=limit, cutoff=0.6)
        except Exception:
            hits = []
    return hits[:limit]


def counts(items):
    """{'Structures': n, 'Analysis': m} for a catalog list."""
    out = {}
    for item in items:
        key = item.get('type') or 'unknown'
        out[key] = out.get(key, 0) + 1
    return out
