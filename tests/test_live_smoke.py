# -*- coding: utf-8 -*-
"""Opt-in real-engine smoke test.

The pytest suite runs against tests/fake_engine.py, which cannot catch "the fake
is wrong" bugs (that is exactly how the ::model parameter support stayed broken
while every test passed). This module shells out to tests/run_bridge_live.py,
which drives the real bridge against a real engine.

Enable with a licensed installation:

    set FDTD_MCP_LIVE=1
    set LUMERICAL_HOME=D:\\Software\\Lumerical\\v202
    pytest tests/test_live_smoke.py -v
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / 'tests' / 'run_bridge_live.py'
_LIVE = os.environ.get('FDTD_MCP_LIVE') == '1'

pytestmark = pytest.mark.live


@pytest.mark.skipif(not _LIVE, reason='set FDTD_MCP_LIVE=1 to run against a real engine')
def test_live_bridge_smoke():
    home = os.environ.get('LUMERICAL_HOME')
    if not home:
        pytest.skip('LUMERICAL_HOME not set')
    sys.path.insert(0, str(ROOT))
    from fdtd_mcp.discovery import find_lumerical_python
    python = find_lumerical_python(home)
    if not os.path.exists(python):
        pytest.skip('Lumerical bundled python not found at ' + python)

    proc = subprocess.run([python, str(RUNNER)], capture_output=True,
                          text=True, timeout=900, cwd=str(ROOT))
    output = (proc.stdout or '') + (proc.stderr or '')
    assert proc.returncode == 0, output
    assert 'checks passed' in output
