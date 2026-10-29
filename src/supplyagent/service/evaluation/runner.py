"""F38 E0: execute pinned contract cases; never claim model semantic quality."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from supplyagent.paths import PROJECT_ROOT

ROOT = PROJECT_ROOT
MANIFEST = ROOT / "config/product/eval-cases.json"


def load_cases(path=MANIFEST):
    manifest = json.loads(Path(path).read_text())
    if manifest["schemaVersion"] != 1 or not manifest["cases"]:
        raise ValueError("invalid evaluation manifest")
    ids = set()
    for case in manifest["cases"]:
        node = case["node"]
        file = node.split("::")[0]
        if (
            case["id"] in ids
            or case["mode"] not in {"offline", "integration"}
            or not case["oracle"].strip()
            or not file.startswith("tests/test_product_api_")
            or ".." in Path(file).parts
            or not (ROOT / file).is_file()
            or "::test_" not in node
            or any(part in file for part in ("_live", "supabase"))
        ):
            raise ValueError("invalid evaluation case")
        ids.add(case["id"])
    return manifest


def verdict(xml, returncode):
    """Skip, collection failure and zero assertions are never a passing case."""
    try:
        cases = list(ET.fromstring(xml).iter("testcase"))
    except ET.ParseError:
        return {"status": "failed", "reason": "report_invalid", "executed": 0}
    failed = sum(c.find("failure") is not None or c.find("error") is not None for c in cases)
    skipped = sum(c.find("skipped") is not None for c in cases)
    executed = len(cases) - skipped
    status = "failed" if returncode or failed or not cases else "skipped" if skipped else "passed"
    return {"status": status, "executed": executed, "skipped": skipped, "failed": failed}


def fingerprint():
    digest = hashlib.sha256()
    files = sorted(
        p for directory in ("src/product_api", "src/supplyagent", "tests", "config/product", "pi_sidecar")
        for p in (ROOT / directory).rglob("*")
        if p.is_file() and p.suffix in {".py", ".json", ".mjs", ".sql"}
        and not {"node_modules", "__pycache__"}.intersection(p.parts)
    )
    files += sorted((ROOT / "supabase/migrations").glob("*.sql"))
    files += [ROOT / "Makefile", ROOT / "pyproject.toml"]
    files += sorted(ROOT.glob("requirements*.txt"))
    for file in files:
        digest.update(str(file.relative_to(ROOT)).encode())
        digest.update(file.read_bytes())
    return digest.hexdigest()


def run(mode, output):
    manifest = load_cases()
    before = fingerprint()
    results = []
    for case in manifest["cases"]:
        if case["mode"] != mode:
            continue
        started = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="supplyagent-eval-") as directory:
            report = Path(directory) / "junit.xml"
            # Only pinned local tests, never live-provider markers or arbitrary commands.
            completed = subprocess.run(
                [sys.executable, "-m", "pytest", "-q", case["node"],
                 "-m", "not model_live and not supabase_live", f"--junitxml={report}"],
                cwd=ROOT, capture_output=True, check=False,
            )
            result = verdict(report.read_text() if report.exists() else "", completed.returncode)
        results.append({**case, **result, "elapsedSeconds": round(time.monotonic() - started, 3)})
    after = fingerprint()
    passed = bool(results) and before == after and all(r["status"] == "passed" for r in results)
    summary = {
        "schemaVersion": 1, "suiteVersion": manifest["suiteVersion"],
        "manifestHash": hashlib.sha256(MANIFEST.read_bytes()).hexdigest(),
        "sourceHash": before, "sourceUnchanged": before == after,
        "mode": mode, "status": "passed" if passed else "failed",
        "measurement": "contract_regression", "semanticQuality": "not_measured",
        "costMicros": None, "usage": "not_aggregated", "cases": results,
    }
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"status": summary["status"], "cases": len(results), "report": str(output)}))
    return 0 if passed else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("offline", "integration"), default="offline")
    parser.add_argument("--output", default=".runtime/eval/latest.json")
    args = parser.parse_args()
    return run(args.mode, args.output)


if __name__ == "__main__":
    raise SystemExit(main())
