"""Compatibility import; implementation lives in supplyagent.service.api.app."""

import importlib
import sys

_module = importlib.import_module('supplyagent.service.api.app')

sys.modules[__name__] = _module
