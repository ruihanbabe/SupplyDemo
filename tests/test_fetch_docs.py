"""Board documents: kept verbatim beside the schematics, listed in MANIFEST, never refetched
by accident — offline, with the GitHub calls replaced."""
from __future__ import annotations

import hashlib
import json
import shutil

import pytest

from knowledge import fetch
from knowledge.fetch import RAW, _wanted, fetch_docs, load_registry, verify

PROJECT = {"project_id": "hackrf-one", "repo": "o/r", "path": "hardware/hackrf-one",
           "license_file": "hardware/hackrf-one/LICENSE", "docs": ["NOTES.md"]}
# Not a real upstream name: macOS paths are case-insensitive, so "README.md" would
# overwrite the archived "Readme.md" in the copied fixture.


def test_registered_documents_survive_a_full_refetch():
    assert _wanted(PROJECT, "NOTES.md")
    assert not _wanted(PROJECT, "docs/other.md")


def test_documents_are_appended_without_touching_the_schematics(tmp_path, monkeypatch):
    shutil.copytree(RAW / "hackrf-one", tmp_path / "hackrf-one")
    before = json.loads((tmp_path / "hackrf-one" / "MANIFEST.json").read_text())
    monkeypatch.setattr(fetch, "_tree", lambda repo, commit: [
        {"path": "NOTES.md", "type": "blob", "sha": "blob-1"}])
    monkeypatch.setattr(fetch, "_get_blob", lambda url, sha: b"# HackRF\n")
    project = {**PROJECT, "commit": before["commit"]}

    assert fetch_docs(project, tmp_path) == ["NOTES.md"]
    after = json.loads((tmp_path / "hackrf-one" / "MANIFEST.json").read_text())
    assert {k: v for k, v in after["files"].items() if k != "NOTES.md"} == before["files"]
    assert after["files"]["NOTES.md"]["sha256"] == hashlib.sha256(b"# HackRF\n").hexdigest()
    assert not (tmp_path / "hackrf-one" / "NOTES.md").stat().st_mode & 0o222
    assert verify("hackrf-one", tmp_path) == []


def test_a_document_missing_upstream_is_refused(tmp_path, monkeypatch):
    shutil.copytree(RAW / "hackrf-one", tmp_path / "hackrf-one")
    monkeypatch.setattr(fetch, "_tree", lambda repo, commit: [])
    commit = json.loads((tmp_path / "hackrf-one" / "MANIFEST.json").read_text())["commit"]
    with pytest.raises(RuntimeError, match="not found"):
        fetch_docs({**PROJECT, "commit": commit}, tmp_path)


@pytest.mark.parametrize("project", load_registry(), ids=lambda p: p["project_id"])
def test_every_registered_document_is_archived(project):
    manifest = json.loads((RAW / project["project_id"] / "MANIFEST.json").read_text())
    assert set(project.get("docs", ())) <= set(manifest["files"])
