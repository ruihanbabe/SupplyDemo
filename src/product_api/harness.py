"""Compatibility import; implementation lives in supplyagent.sdk.engine.harness."""

import importlib
import sys

_module = importlib.import_module('supplyagent.sdk.engine.harness')

sys.modules[__name__] = _module
