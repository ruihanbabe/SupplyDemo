"""Compatibility import; implementation lives in supplyagent.service.runtime.runtime."""

import importlib
import sys

_module = importlib.import_module('supplyagent.service.runtime.runtime')

sys.modules[__name__] = _module
