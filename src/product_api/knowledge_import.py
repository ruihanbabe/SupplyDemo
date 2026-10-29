"""Compatibility import; implementation lives in supplyagent.service.domains.knowledge_import."""

import importlib
import sys

_module = importlib.import_module('supplyagent.service.domains.knowledge_import')

sys.modules[__name__] = _module
