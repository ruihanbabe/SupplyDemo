"""Compatibility import; implementation lives in supplyagent.service.runtime.worker_main."""

import importlib
import sys

_module = importlib.import_module('supplyagent.service.runtime.worker_main')

if __name__ == "__main__":
    raise SystemExit(_module.main())

sys.modules[__name__] = _module
