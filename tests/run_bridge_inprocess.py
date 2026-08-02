# -*- coding: utf-8 -*-
"""Runs fdtd_mcp.bridge.main() with a fake lumapi module so tests need no
Lumerical installation. Consumed by tests/test_bridge_protocol.py."""
import json
import os
import sys
import types
from pathlib import Path

_PKG = str(Path(__file__).resolve().parents[1])
os.environ['LUMERICAL_HOME'] = _PKG


class _FakeProject(object):
    def __init__(self, *a, **k):
        pass

    def close(self): pass
    def setnamed(self, *a, **k): pass
    def eval(self, *a, **k): pass
    def select(self, *a, **k): pass
    def save(self, *a, **k): pass
    def feval(self, *a, **k): pass
    def run(self, *a, **k): pass
    def runsweep(self, *a, **k): pass
    def getv(self, *a, **k): return None
    def getnamed(self, *a, **k): return None


lumapi = types.ModuleType('lumapi')
lumapi.FDTD = _FakeProject
lumapi.appCall = lambda *a, **k: None
sys.modules['lumapi'] = lumapi

sys.path.insert(0, _PKG)
from fdtd_mcp.bridge import main

main()
