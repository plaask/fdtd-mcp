# -*- coding: utf-8 -*-
"""Runs fdtd_mcp.bridge.main() with the scripted fake_engine lumapi injected,
so functional tests need no Lumerical installation. Consumed by
tests/test_functional.py."""
import os
import sys
from pathlib import Path

_PKG = str(Path(__file__).resolve().parents[1])
os.environ['LUMERICAL_HOME'] = _PKG

_TESTS = str(Path(__file__).resolve().parent)
sys.path.insert(0, _TESTS)
sys.path.insert(0, _PKG)

import fake_engine  # noqa: E402

fake_engine.install()

from fdtd_mcp.bridge import main  # noqa: E402

main()
