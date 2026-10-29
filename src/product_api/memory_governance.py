"""Compatibility import; implementation lives in supplyagent.service.memory.governance."""

import importlib
import sys

_module = importlib.import_module('supplyagent.service.memory.governance')

sys.modules[__name__] = _module
