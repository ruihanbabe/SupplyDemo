"""Compatibility import; implementation lives in supplyagent.service.domains.commercial."""

import importlib
import sys

_module = importlib.import_module('supplyagent.service.domains.commercial')

sys.modules[__name__] = _module
