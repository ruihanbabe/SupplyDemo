"""Compatibility import; implementation lives in supplyagent.service.evaluation.results."""

import importlib
import sys

_module = importlib.import_module('supplyagent.service.evaluation.results')

if __name__ == "__main__":
    raise SystemExit(_module.main())

sys.modules[__name__] = _module
