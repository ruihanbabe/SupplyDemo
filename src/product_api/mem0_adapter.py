"""Compatibility import; implementation lives in supplyagent.service.memory.mem0_adapter."""

import importlib
import sys

_module = importlib.import_module('supplyagent.service.memory.mem0_adapter')

sys.modules[__name__] = _module
