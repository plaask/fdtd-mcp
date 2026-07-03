# -*- coding: utf-8 -*-
"""Auto-detect Lumerical FDTD installation path."""

import os
import sys
import glob


def find_lumerical() -> str:
    """Return the Lumerical installation root (e.g. C:/Program Files/Lumerical/v241).

    Resolution order:
      1. LUMERICAL_HOME environment variable
      2. Search common install directories (dynamic, not hardcoded)
      3. Pick newest version if multiple found

    Raises FileNotFoundError if no installation is found.
    """
    # 1. Environment variable override
    env = os.environ.get('LUMERICAL_HOME')
    if env and _is_valid(env):
        return os.path.normpath(env)

    # 2. Search common locations — dynamically built, not hardcoded
    candidates = []
    for root in _search_roots():
        for path in sorted(glob.glob(os.path.join(root, 'v*')), reverse=True):
            if _is_valid(path):
                candidates.append(os.path.normpath(path))

    # 3. Return newest version (or first valid)
    if candidates:
        return candidates[0]

    raise FileNotFoundError(
        'Lumerical FDTD installation not found. '
        'Set LUMERICAL_HOME environment variable to the installation root, '
        'e.g. C:/Program Files/Lumerical/v202'
    )


def _search_roots():
    """Build dynamic list of directories that may contain Lumerical installs.

    Uses environment variables and common conventions — no single hardcoded path
    is relied on. If a root doesn't exist on disk it's harmlessly skipped by
    glob (returns empty list).

    Windows-only for now. Linux support can be added when needed.
    """
    roots = []
    # Standard Program Files (C:/Program Files/Lumerical etc.)
    for env_var in ['ProgramFiles', 'ProgramFiles(x86)']:
        pf = os.environ.get(env_var)
        if pf:
            roots.append(os.path.join(pf, 'Lumerical'))
    # Common alternate locations across typical drive letters
    for drive in ['C:', 'D:', 'E:']:
        for sub in ['Lumerical', 'Software', 'Program Files']:
            p = drive + '/' + sub + '/Lumerical'
            roots.append(p)
    return roots


def find_lumerical_python(lumerical_home: str) -> str:
    """Return the Python executable path for the given Lumerical installation."""
    python_dir = os.path.join(lumerical_home, 'python-3.6.8-embed-amd64')
    python_exe = os.path.join(python_dir, 'python.exe')
    if not os.path.exists(python_exe):
        # Try glob for python-* directory (version may vary)
        for d in glob.glob(os.path.join(lumerical_home, 'python-*')):
            exe = os.path.join(d, 'python.exe')
            if os.path.exists(exe):
                return exe
    return python_exe


def find_lumerical_bin(lumerical_home: str) -> str:
    """Return the bin directory path."""
    return os.path.join(lumerical_home, 'bin')


def _is_valid(root: str) -> bool:
    """Check if root is a valid Lumerical installation by verifying lumapi.py exists."""
    lumapi = os.path.join(root, 'api', 'python', 'lumapi.py')
    return os.path.isfile(lumapi)
