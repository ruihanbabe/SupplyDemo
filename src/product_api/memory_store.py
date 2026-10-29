"""Compatibility import; implementation lives in supplyagent.service.memory.store."""

import importlib
import sys

_module = importlib.import_module('supplyagent.service.memory.store')

sys.modules[__name__] = _module
