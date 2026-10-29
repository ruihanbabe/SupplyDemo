"""Compatibility import; implementation lives in supplyagent.service.api.rbac."""

import importlib
import sys

_module = importlib.import_module('supplyagent.service.api.rbac')

sys.modules[__name__] = _module
