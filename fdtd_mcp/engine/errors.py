# -*- coding: utf-8 -*-
"""The bridge's single error channel.

Handlers raise BridgeError for expected, user-facing failures; `handle()` turns
it into the one `{'error': {code, message}}` shape the server understands.
Anything else is the second, traceback-derived channel (unexpected engine
errors).

Runs on Lumerical's bundled Python 3.6.8 (no f-strings, no type hints).
"""
from __future__ import print_function


class BridgeError(RuntimeError):
    """Expected failure: reported verbatim to the MCP client."""
    pass
