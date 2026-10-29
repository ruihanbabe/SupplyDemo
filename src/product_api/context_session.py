"""Compatibility import; implementation lives in supplyagent.service.context.session."""

import importlib
import sys

_module = importlib.import_module('supplyagent.service.context.session')

sys.modules[__name__] = _module
