"""Compatibility import; implementation lives in supplyagent.service.memory.models."""

import importlib
import sys

_module = importlib.import_module('supplyagent.service.memory.models')

sys.modules[__name__] = _module
