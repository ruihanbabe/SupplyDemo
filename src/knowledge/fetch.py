"""Pull each registered hardware project, pinned to one commit, into the raw layer.

Each file is downloaded as upstream wrote it at that commit, checked against git's blob id,
and then pruned to electronic components only (knowledge.prune). MANIFEST.json records where
each file came from and its SHA-256, so any later doubt about a parsed value can be settled
by pointing at the exact source byte range. Files are made read-only after writing, as with
the existing domdata layer.

Board documents listed under `docs` (the README) are kept verbatim beside the schematics as
evidence for tools to read on demand; they are never pruned or turned into fields.

Only public GitHub reads happen here — no credentials, no cost.

Run with: make fetch-hardware (everything) or make fetch-docs (documents only)
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
REGISTRY = ROOT / "config" / "knowledge" / "sources.yaml"
RAW = ROOT / "data" / "hardware" / "raw"
KEEP = (".kicad_sch", ".kicad_pro")


def load_registry(path: Path = REGISTRY) -> list[dict[str, Any]]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))["projects"]


def _get(url: str) -> bytes:
    # curl rather than urllib: large schematics over a flaky link need its retry logic.
    return subprocess.run(
        ["curl", "-sfL", "--retry", "6", "--retry-all-errors", "--max-time", "180", url],
        check=True, capture_output=True).stdout


def _get_json(url: str, attempts: int = 5) -> Any:
    for attempt in range(attempts):
        try:
            return json.loads(_get(url))
        except json.JSONDecodeError:
            # A dropped connection can end a body early while curl still exits 0.
            if attempt == attempts - 1:
                raise
    raise AssertionError("unreachable")


def _git_blob_sha(body: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(body) + body).hexdigest()


def _get_blob(url: str, expected_sha: str, attempts: int = 5) -> bytes:
    """Download until the bytes hash to the blob id git itself recorded."""
    for _ in range(attempts):
        body = _get(url)
        if _git_blob_sha(body) == expected_sha:
            return body
    raise RuntimeError(f"{url}: content never matched git blob {expected_sha}")


def _wanted(project: dict[str, Any], path: str) -> bool:
    prefix = project["path"].rstrip("/")
    inside = not prefix or path.startswith(prefix + "/")
    return ((inside and path.endswith(KEEP)) or path == project["license_file"]
            or path in project.get("docs", ()))


def _tree(repo: str, commit: str) -> list[dict[str, Any]]:
    tree = _get_json(f"https://api.github.com/repos/{repo}/git/trees/{commit}?recursive=1")
    if tree.get("truncated"):
        raise RuntimeError(f"{repo}: tree listing truncated; the snapshot would be incomplete")
    return tree["tree"]


def _store(target: Path, repo: str, commit: str, path: str, blob: str) -> dict[str, Any]:
    body = _get_blob(f"https://raw.githubusercontent.com/{repo}/{commit}/{path}", blob)
    local = target / path
    local.parent.mkdir(parents=True, exist_ok=True)
    if local.exists():
        os.chmod(local, stat.S_IWUSR | stat.S_IRUSR)
    local.write_bytes(body)
    os.chmod(local, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
    return {"sha256": hashlib.sha256(body).hexdigest(), "git_blob": blob, "bytes": len(body)}


def _write_manifest(target: Path, manifest: dict[str, Any]) -> None:
    manifest_path = target / "MANIFEST.json"
    if manifest_path.exists():
        os.chmod(manifest_path, stat.S_IWUSR | stat.S_IRUSR)
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
                             encoding="utf-8")
    os.chmod(manifest_path, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)


def fetch_project(project: dict[str, Any], raw: Path = RAW) -> dict[str, Any]:
    repo, commit = project["repo"], project["commit"]
    blobs = {item["path"]: item["sha"] for item in _tree(repo, commit)
             if item["type"] == "blob" and _wanted(project, item["path"])}
    paths = sorted(blobs)
    if project["license_file"] not in paths:
        raise RuntimeError(f"{repo}@{commit}: license file {project['license_file']} missing")

    target = raw / project["project_id"]
    target.mkdir(parents=True, exist_ok=True)
    files = {path: _store(target, repo, commit, path, blobs[path]) for path in paths}

    root = f"{project['path'].rstrip('/')}/{project['root_schematic']}".lstrip("/")
    if root not in files:
        raise RuntimeError(f"{repo}@{commit}: root schematic {root} not found")
    manifest = {"project_id": project["project_id"], "repo": repo, "commit": commit,
                "path": project["path"], "root_schematic": root,
                "license": project["license"], "license_file": project["license_file"],
                "fetched_at": datetime.now(UTC).isoformat(timespec="seconds"),
                "files": files}
    _write_manifest(target, manifest)
    return manifest


def fetch_docs(project: dict[str, Any], raw: Path = RAW) -> list[str]:
    """Add the registered documents to an already-fetched project; schematics are untouched."""
    docs = list(project.get("docs", ()))
    if not docs:
        return []
    repo, commit = project["repo"], project["commit"]
    blobs = {item["path"]: item["sha"] for item in _tree(repo, commit)
             if item["type"] == "blob" and item["path"] in docs}
    missing = sorted(set(docs) - set(blobs))
    if missing:
        raise RuntimeError(f"{repo}@{commit}: documents {missing} not found")
    target = raw / project["project_id"]
    manifest = json.loads((target / "MANIFEST.json").read_text(encoding="utf-8"))
    if manifest["commit"] != commit:
        raise RuntimeError(f"{project['project_id']}: MANIFEST is at {manifest['commit'][:10]}, "
                           f"registry at {commit[:10]}; run make fetch-hardware first")
    for path in docs:
        manifest["files"][path] = _store(target, repo, commit, path, blobs[path])
    manifest["files"] = dict(sorted(manifest["files"].items()))
    _write_manifest(target, manifest)
    return docs


def verify(project_id: str, raw: Path = RAW) -> list[str]:
    """Paths whose bytes no longer match the manifest. Empty means intact."""
    target = raw / project_id
    manifest = json.loads((target / "MANIFEST.json").read_text(encoding="utf-8"))
    return [path for path, meta in manifest["files"].items()
            if hashlib.sha256((target / path).read_bytes()).hexdigest() != meta["sha256"]]


def main() -> None:
    if "--docs" in sys.argv[1:]:
        for project in load_registry():
            print(f"{project['project_id']}: docs {fetch_docs(project)}")
        return
    # Imported here: prune imports this module for RAW and load_registry.
    from knowledge.prune import prune_project

    for project in load_registry():
        manifest = fetch_project(project)
        print(f"{project['project_id']}: {len(manifest['files'])} files "
              f"@ {manifest['commit'][:10]}")
        print(f"  pruned: {prune_project(project['project_id'])}")


if __name__ == "__main__":
    main()
