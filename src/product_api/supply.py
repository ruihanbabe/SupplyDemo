"""Compatibility import; implementation lives in supplyagent.service.domains.supply."""

import importlib
import sys

_module = importlib.import_module('supplyagent.service.domains.supply')

sys.modules[__name__] = _module
