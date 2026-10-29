"""Compatibility import; implementation lives in supplyagent.service.domains.business_nodes."""

import importlib
import sys

_module = importlib.import_module('supplyagent.service.domains.business_nodes')

sys.modules[__name__] = _module
