# -*- coding: utf-8 -*-
"""Test bridge._sanitize data fidelity without a Lumerical installation."""
import os
import sys
import types
from pathlib import Path

_PKG = Path(__file__).resolve().parents[1]


def _bridge():
    os.environ['LUMERICAL_HOME'] = str(_PKG)
    lumapi = types.ModuleType('lumapi')
    lumapi.FDTD = lambda *a, **k: None
    lumapi.appCall = lambda *a, **k: None
    sys.modules['lumapi'] = lumapi
    import fdtd_mcp.bridge as bridge
    return bridge.FdtdBridge()


def test_scalars():
    s = _bridge()
    assert s._sanitize(None) is None
    assert s._sanitize(True) is True
    assert s._sanitize(3) == 3
    assert s._sanitize(1.5) == 1.5
    assert s._sanitize(float('nan')) is None  # NaN -> None
    assert s._sanitize('text') == 'text'


def test_list_truncation_reports_meta():
    s = _bridge()
    out = s._sanitize(list(range(100)), cap=10)
    assert out['truncated'] is True
    assert out['length'] == 100
    assert out['data'] == list(range(10))


def test_list_under_cap_plain():
    s = _bridge()
    assert s._sanitize([1, 2, 3]) == [1, 2, 3]


def test_dict_recursion():
    s = _bridge()
    assert s._sanitize({'a': {'b': 1}}) == {'a': {'b': 1}}


def test_ndarray_small_preserves_shape():
    s = _bridge()
    import numpy as np
    arr = np.arange(6).reshape(2, 3)
    out = s._sanitize(arr)
    assert out['shape'] == [2, 3]
    assert out['length'] == 6
    assert out['data'] == [[0, 1, 2], [3, 4, 5]]


def test_ndarray_complex_becomes_re_im():
    s = _bridge()
    import numpy as np
    arr = np.array([1.0 + 2.0j, 3.0 + 4.0j])
    out = s._sanitize(arr)
    assert out['data'] == [[1.0, 2.0], [3.0, 4.0]]


def test_ndarray_large_truncates():
    s = _bridge()
    import numpy as np
    arr = np.arange(50)
    out = s._sanitize(arr, cap=10)
    assert out['truncated'] is True
    assert out['length'] == 50
    assert len(out['data']) == 10


def test_complex_scalar_becomes_pair():
    s = _bridge()
    assert s._sanitize(2.5 + 0.1j) == [2.5, 0.1]
