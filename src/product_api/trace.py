"""Compatibility import; implementation lives in supplyagent.service.persistence.trace."""

import importlib
import sys

_module = importlib.import_module('supplyagent.service.persistence.trace')

sys.modules[__name__] = _module
