"""Compatibility import; implementation lives in supplyagent.sdk.tools.scheduler."""

import importlib
import sys

_module = importlib.import_module('supplyagent.sdk.tools.scheduler')

sys.modules[__name__] = _module
