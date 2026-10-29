"""Compatibility import; implementation lives in supplyagent.service.memory.runtime."""

import importlib
import sys

_module = importlib.import_module('supplyagent.service.memory.runtime')

sys.modules[__name__] = _module
