"""Compatibility import; implementation lives in supplyagent.service.domains.knowledge."""

import importlib
import sys

_module = importlib.import_module('supplyagent.service.domains.knowledge')

sys.modules[__name__] = _module
