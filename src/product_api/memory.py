"""Compatibility import; implementation lives in supplyagent.service.domains.memory."""

import importlib
import sys

_module = importlib.import_module('supplyagent.service.domains.memory')

sys.modules[__name__] = _module
