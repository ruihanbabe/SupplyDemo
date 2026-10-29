"""Compatibility import; implementation lives in supplyagent.service.persistence.context_store."""

import importlib
import sys

_module = importlib.import_module('supplyagent.service.persistence.context_store')

sys.modules[__name__] = _module
