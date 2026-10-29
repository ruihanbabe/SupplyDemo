"""Compatibility import; implementation lives in supplyagent.sdk.context.mapping."""

import importlib
import sys

_module = importlib.import_module('supplyagent.sdk.context.mapping')

sys.modules[__name__] = _module
