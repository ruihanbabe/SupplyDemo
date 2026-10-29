"""Compatibility import; implementation lives in supplyagent.service.persistence.tenant."""

import importlib
import sys

_module = importlib.import_module('supplyagent.service.persistence.tenant')

sys.modules[__name__] = _module
