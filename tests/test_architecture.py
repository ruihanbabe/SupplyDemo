"""F53 A: module ownership, real compatibility and a frozen transition boundary."""

import ast
import importlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MAP = json.loads((ROOT / 'config/architecture/module-map.json').read_text())


def test_package_ownership_and_transitional_imports():
    """Stage A moves modules, not internals; existing reverse edges remain explicit."""
    observed = set()
    paths = set()
    for path in (ROOT / 'src/supplyagent').rglob('*.py'):
        source = '.'.join(path.relative_to(ROOT / 'src').with_suffix('').parts)
        if path.name != '__init__.py' and source != 'supplyagent.paths':
            paths.add(source)
        for node in ast.walk(ast.parse(path.read_text())):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ''] if isinstance(node, ast.ImportFrom) else []
            for name in names:
                assert name != 'product_api' and not name.startswith('product_api.'), (path, node.lineno)
                if source.startswith('supplyagent.sdk.') and name.startswith('supplyagent.service.'):
                    observed.add((source, name))
    assert set(MAP['legacyModules'].values()) == paths
    assert observed == {tuple(edge) for edge in MAP['transitionalImports']}


@pytest.mark.parametrize('old,new', MAP['legacyModules'].items())
def test_legacy_imports_preserve_module_identity(old, new):
    legacy = importlib.import_module('product_api.' + old)
    canonical = importlib.import_module(new)
    assert legacy is canonical
    assert Path(canonical.__file__).is_relative_to(ROOT / 'src/supplyagent')


@pytest.mark.parametrize('name', ['worker_main', 'memory_worker_main', 'evaluation', 'evaluation_result'])
def test_legacy_and_canonical_cli_help(name):
    environment = {**os.environ, 'PYTHONPATH': str(ROOT / 'src')}
    for module in ('product_api.' + name, MAP['legacyModules'][name]):
        result = subprocess.run([sys.executable, '-m', module, '--help'], cwd=ROOT,
                                env=environment, capture_output=True, text=True, timeout=15, check=False)
        assert result.returncode == 0, result.stderr
        assert 'usage:' in result.stdout.lower()
