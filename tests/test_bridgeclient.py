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
    srv.BRIDGE_ARGS = [sys.executable, _FAKE]


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


def test_restart_notice_is_reported_once():
    """A restarted engine means lost in-memory state — say so, once."""
    _point_bridge_at_fake()
    b = srv.BridgeClient()
    b.start()
    try:
        assert b.take_restart_notice() is None           # first start: no notice
        b._proc.kill()
        b._proc.wait()
        b.start()                                        # simulated respawn
        notice = b.take_restart_notice()
        assert notice and 'restarted' in notice
        assert b.take_restart_notice() is None           # one-shot
    finally:
        b.stop()


def test_unsolicited_output_does_not_desync_responses():
    """Id-less chatter must not be mistaken for the next call's answer."""
    _point_bridge_at_fake()
    b = srv.BridgeClient()
    b.start()
    try:
        # 'noise' writes a JSON line without an id and a non-JSON line before
        # its real response; both must be skipped.
        r = b.call('noise', {'x': 1})
        assert r == {'echo': {'x': 1}}
        # ... and the following call must still line up.
        r2 = b.call('anything', {'y': 2})
        assert r2 == {'echo': {'y': 2}}
        collected = b.unsolicited()
        assert any('engine chatter' in line for line in collected)
        assert any('not json' in line for line in collected)
    finally:
        b.stop()
