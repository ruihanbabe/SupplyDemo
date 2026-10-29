"""Compatibility import; implementation lives in supplyagent.service.workflows.specialists."""

import importlib
import sys

_module = importlib.import_module('supplyagent.service.workflows.specialists')

sys.modules[__name__] = _module
