"""Compatibility import; implementation lives in supplyagent.sdk.tools.runtime."""

import importlib
import sys

_module = importlib.import_module('supplyagent.sdk.tools.runtime')

sys.modules[__name__] = _module
