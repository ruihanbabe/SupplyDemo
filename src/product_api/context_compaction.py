"""Compatibility import; implementation lives in supplyagent.sdk.context.compaction."""

import importlib
import sys

_module = importlib.import_module('supplyagent.sdk.context.compaction')

sys.modules[__name__] = _module
