"""Compatibility import; implementation lives in supplyagent.service.actions.channels."""

import importlib
import sys

_module = importlib.import_module('supplyagent.service.actions.channels')

sys.modules[__name__] = _module
