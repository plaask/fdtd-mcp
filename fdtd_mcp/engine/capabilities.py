# -*- coding: utf-8 -*-
"""Verified engine capabilities — the runtime half of the cheatsheet.

`fdtd_mcp/cheatsheet/lumapi_ref.json` is the human/LLM-facing record of what was
verified against a real engine; this module is the machine-readable subset the
bridge needs at runtime, plus a code fingerprint so a caller can tell whether
the bridge it is talking to is running the code on disk.

`tests/test_capability_guard.py` asserts the two stay in sync, so a fact only
has to be corrected in one place to be corrected everywhere.

Runs on Lumerical's bundled Python 3.6.8 (no f-strings, no type hints).
"""
from __future__ import print_function
import hashlib
import os

from .constants import LSF_PROP_TYPES

# ---------------------------------------------------------------------------
# Commands that do NOT exist on v202 (verified live via appCall and eval).
# Referencing one from bridge code fails test_capability_guard.
# ---------------------------------------------------------------------------
DEAD_COMMANDS = frozenset([
    'addvar', 'addvarfdtd', 'setvar', 'getvar',
    'findresult', 'hasresult', 'listresults',
    'adddftmonitor', 'addfield',
])

# Verified metadata source for the facts above (kept for the status tool).
VERIFIED_AGAINST = 'Lumerical v202 (2020 R2), live engine probes 2026-09'

# Which declaration command each object type accepts for a NEW property.
# Verified live on v202:
#   * the model root counts as a group ("adduserprop requires a single
#     structure or analysis group object to be selected"),
#   * the model root and analysis groups are edited by the "Edit analysis
#     group" dialog, whose Analysis > Variables table lists ANALYSIS
#     properties — so ::model uses addanalysisprop, which is what makes a
#     model parameter show up there,
#   * structure groups use adduserprop,
#   * the two namespaces SHARE names: declaring "gap" as an analysis property
#     while a user property "gap" exists creates "gap2" (verified),
#   * everything else has no property-declaration API at all.
USER_PROP_FUNCTION = 'adduserprop'
ANALYSIS_PROP_FUNCTION = 'addanalysisprop'
ANALYSIS_RESULT_FUNCTION = 'addanalysisresult'


def user_prop_function(obj_name, obj_type):
    """Return the LSF function that declares a new property on this object.

    The model root and analysis groups use addanalysisprop (their Analysis tab
    lists analysis properties); structure groups use adduserprop. Returns None
    when the object type cannot carry new properties.
    """
    if obj_type == 'ObjectModelRoot' or obj_name == '::model' \
            or 'Analysis' in obj_type:
        return ANALYSIS_PROP_FUNCTION
    if 'Structure' in obj_type:
        return USER_PROP_FUNCTION
    return None


def type_code(value, explicit=None):
    """Resolve the INTEGER property-type code for adduserprop/addanalysisprop.

    An explicit code (int) or type name (e.g. 'length') wins, otherwise the code
    is inferred from the Python value. Verified live on v202: only the integer
    form is accepted — the string form ('Length') raises
    "argument list for adduserprop command is incorrect".
    """
    if isinstance(explicit, bool):
        explicit = None            # bool is an int subclass; never a code
    if isinstance(explicit, int):
        return explicit if 0 <= explicit <= 5 else 0
    if isinstance(explicit, str) and explicit.strip():
        key = explicit.strip().lower()
        if key in LSF_PROP_TYPES:
            return LSF_PROP_TYPES[key]
        try:
            code = int(key)
            if 0 <= code <= 5:
                return code
        except ValueError:
            pass
    if isinstance(value, str):
        return LSF_PROP_TYPES['text']
    return LSF_PROP_TYPES['number']


# ---------------------------------------------------------------------------
# Code fingerprint
# ---------------------------------------------------------------------------

_FINGERPRINT_NAMES = ('bridge.py', 'dispatch.json')


def _fingerprint_paths(root=None):
    """The file set that identifies a bridge code generation."""
    pkg = root or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    paths = [os.path.join(pkg, n) for n in _FINGERPRINT_NAMES]
    for sub in ('engine', 'handlers'):
        d = os.path.join(pkg, sub)
        if os.path.isdir(d):
            for name in sorted(os.listdir(d)):
                if name.endswith('.py'):
                    paths.append(os.path.join(d, name))
    return [p for p in paths if os.path.isfile(p)]


def _fingerprint(paths):
    h = hashlib.sha1()
    for path in paths:
        h.update(os.path.basename(path).encode('utf-8'))
        try:
            with open(path, 'rb') as f:
                h.update(f.read())
        except Exception:
            h.update(b'?')
    return h.hexdigest()[:12]


# Fingerprint of the code THIS process actually imported. Computed once, at
# import time — recomputing it later would just re-read the files and could
# never reveal that the process is running stale code.
LOADED_CODE_ID = _fingerprint(_fingerprint_paths())


def code_id():
    """Fingerprint of the bridge code this process loaded (import-time)."""
    return LOADED_CODE_ID


def code_id_of(root):
    """Fingerprint of the same file set *as it is on disk right now*.

    engine_status compares the two: a difference means this process is running
    older code than the checkout (restart the bridge for changes to apply).
    """
    return _fingerprint(_fingerprint_paths(root))


def engine_facts():
    """Environment facts reported by session_* and engine_status.

    `bridge_code_id` is the fingerprint of the bridge code actually imported by
    the running bridge process: if it differs from the on-disk hash, the bridge
    predates your edit and needs a restart for bridge-side changes to apply.
    """
    home = os.environ.get('LUMERICAL_HOME', '') or ''
    return {
        'lumerical_home': home,
        'install_version': os.path.basename(home.rstrip('/\\')) or None,
        'bridge_code_id': code_id(),
        'verified_against': VERIFIED_AGAINST,
    }
