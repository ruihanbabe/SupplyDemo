"""Compatibility import; implementation lives in supplyagent.service.actions.models."""

import importlib
import sys

_module = importlib.import_module('supplyagent.service.actions.models')

sys.modules[__name__] = _module
