"""Compatibility import; implementation lives in supplyagent.sdk.engine.tool_bridge."""

import importlib
import sys

_module = importlib.import_module('supplyagent.sdk.engine.tool_bridge')

sys.modules[__name__] = _module
