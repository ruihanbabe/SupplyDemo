"""Compatibility import; implementation lives in supplyagent.service.gateway.gateway."""

import importlib
import sys

_module = importlib.import_module('supplyagent.service.gateway.gateway')

sys.modules[__name__] = _module
