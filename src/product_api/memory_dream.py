"""Compatibility import; implementation lives in supplyagent.service.memory.dream."""

import importlib
import sys

_module = importlib.import_module('supplyagent.service.memory.dream')

sys.modules[__name__] = _module
