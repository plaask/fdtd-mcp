# -*- coding: utf-8 -*-
"""Fake bridge subprocess for testing BridgeClient.

Echoes params back as the result; supports a 'crash' method to simulate an
unexpected subprocess death. Used only by tests/test_bridgeclient.py.
"""
import json
import sys

sys.stdout.write(json.dumps({'ready': True}) + '\n')
sys.stdout.flush()

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        req = json.loads(line)
    except Exception:
        sys.stdout.write(json.dumps(
            {'id': None, 'error': {'code': -1, 'message': 'Invalid JSON'}}) + '\n')
        sys.stdout.flush()
        continue
    if req.get('method') == 'shutdown':
        break
    if req.get('method') == 'crash':
        sys.exit(3)
    if req.get('method') == 'sleep':
        import time
        time.sleep(float(req.get('params', {}).get('duration', 1)))
        resp = {'id': req.get('id'), 'result': {'slept': True}}
        sys.stdout.write(json.dumps(resp) + '\n')
        sys.stdout.flush()
        continue
    resp = {'id': req.get('id'), 'result': {'echo': req.get('params', {})}}
    sys.stdout.write(json.dumps(resp) + '\n')
    sys.stdout.flush()

sys.exit(0)
