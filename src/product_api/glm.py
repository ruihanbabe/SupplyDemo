"""Compatibility import; implementation lives in supplyagent.service.gateway.glm."""

import importlib
import sys

_module = importlib.import_module('supplyagent.service.gateway.glm')

sys.modules[__name__] = _module
