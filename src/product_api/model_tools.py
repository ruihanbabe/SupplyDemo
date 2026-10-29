"""Compatibility import; implementation lives in supplyagent.service.gateway.model_tools."""

import importlib
import sys

_module = importlib.import_module('supplyagent.service.gateway.model_tools')

sys.modules[__name__] = _module
