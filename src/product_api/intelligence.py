"""Compatibility import; implementation lives in supplyagent.service.workflows.intelligence."""

import importlib
import sys

_module = importlib.import_module('supplyagent.service.workflows.intelligence')

sys.modules[__name__] = _module
