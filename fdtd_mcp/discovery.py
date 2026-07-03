"""Auto-detect Lumerical FDTD installation path."""

import os
import sys
import glob


def find_lumerical() -> str:
    """Return the Lumerical installation root (e.g. C:/Program Files/Lumerical/v241).

    Resolution order:
      1. LUMERICAL_HOME environment variable
      2. Search common install directories
      3. Pick newest version if multiple found

    Raises FileNotFoundError if no installation is found.
    """
    # 1. Environment variable override
    env = os.environ.get('LUMERICAL_HOME')
    if env and _is_valid(env):
        return os.path.normpath(env)

    # 2. Search common locations
    candidates = []

    if sys.platform == 'win32':
        search_roots = [
            'C:/Program Files/Lumerical',
        ]
    else:
        search_roots = [
            '/opt/lumerical',
            os.path.expanduser('~/lumerical'),
        ]

    for root in search_roots:
        for pattern in [os.path.join(root, 'v*')]:
            for path in sorted(glob.glob(pattern), reverse=True):
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


def find_lumerical_python(lumerical_home: str) -> str:
    """Return the Python executable path for the given Lumerical installation."""
    if sys.platform == 'win32':
        python_dir = os.path.join(lumerical_home, 'python-3.6.8-embed-amd64')
        python_exe = os.path.join(python_dir, 'python.exe')
    else:
        # Linux: use system python or bundled python3
        python_exe = os.path.join(lumerical_home, 'bin', 'python3')
        if not os.path.exists(python_exe):
            python_exe = 'python3'  # fallback to system
    if not os.path.exists(python_exe) and sys.platform == 'win32':
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
