"""Compatibility import; implementation lives in supplyagent.service.workflows.specialist_runner."""

import importlib
import sys

_module = importlib.import_module('supplyagent.service.workflows.specialist_runner')

sys.modules[__name__] = _module
