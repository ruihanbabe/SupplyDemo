"""Compatibility import; implementation lives in supplyagent.sdk.context.protocol."""

import importlib
import sys

_module = importlib.import_module('supplyagent.sdk.context.protocol')

sys.modules[__name__] = _module
