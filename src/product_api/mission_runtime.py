"""Compatibility import; implementation lives in supplyagent.service.workflows.mission_runtime."""

import importlib
import sys

_module = importlib.import_module('supplyagent.service.workflows.mission_runtime')

sys.modules[__name__] = _module
