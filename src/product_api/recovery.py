"""Compatibility import; implementation lives in supplyagent.service.runtime.recovery."""

import importlib
import sys

_module = importlib.import_module('supplyagent.service.runtime.recovery')

sys.modules[__name__] = _module
