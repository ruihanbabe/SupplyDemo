"""Compatibility import; implementation lives in supplyagent.sdk.context.compiler."""

import importlib
import sys

_module = importlib.import_module('supplyagent.sdk.context.compiler')

sys.modules[__name__] = _module
