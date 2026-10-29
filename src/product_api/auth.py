"""Compatibility import; implementation lives in supplyagent.service.api.auth."""

import importlib
import sys

_module = importlib.import_module('supplyagent.service.api.auth')

sys.modules[__name__] = _module
