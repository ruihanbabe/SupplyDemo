"""Compatibility import; implementation lives in supplyagent.sdk.tools.registry."""

import importlib
import sys

_module = importlib.import_module('supplyagent.sdk.tools.registry')

sys.modules[__name__] = _module
