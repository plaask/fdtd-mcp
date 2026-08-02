# -*- coding: utf-8 -*-
"""Test server-side BridgeClient robustness against a fake bridge subprocess.

Covers: ready handshake, request/response round-trip, crash detection, and
automatic respawn via _ensure_bridge(). No Lumerical installation needed.
"""
import sys
from pathlib import Path

import fdtd_mcp.server as srv

_FAKE = str(Path(__file__).resolve().parent / 'fake_bridge.py')


def _point_bridge_at_fake():
    srv.LUMERICAL_PYTHON = sys.executable
    srv.BRIDGE_SCRIPT = _FAKE


def test_roundtrip_and_shutdown():
    _point_bridge_at_fake()
    b = srv.BridgeClient()
    b.start()
    try:
        assert not b.is_dead()
        r = b.call('anything', {'a': 1, 'b': 'x'})
        assert r == {'echo': {'a': 1, 'b': 'x'}}
    finally:
        b.stop()
    assert b._proc is None


def test_crash_detected_and_reported():
    _point_bridge_at_fake()
    b = srv.BridgeClient()
    b.start()
    try:
        # Kill the subprocess out from under the client.
        b._proc.kill()
        b._proc.wait()
        try:
            b.call('anything', {})
            raise AssertionError('expected RuntimeError after bridge death')
        except RuntimeError as e:
            assert 'Bridge' in str(e)
        assert b.is_dead()
    finally:
        b.stop()


def test_ensure_bridge_respawns_after_crash():
    _point_bridge_at_fake()
    srv._bridge_started = False
    srv._ensure_bridge()
    try:
        r = srv._bridge.call('anything', {'probe': 1})
        assert r == {'echo': {'probe': 1}}
        # Simulate crash, then next ensure() must respawn a live bridge.
        srv._bridge._proc.kill()
        srv._bridge._proc.wait()
        srv._ensure_bridge()
        r = srv._bridge.call('anything', {'probe': 2})
        assert r == {'echo': {'probe': 2}}
    finally:
        srv._bridge.stop()
