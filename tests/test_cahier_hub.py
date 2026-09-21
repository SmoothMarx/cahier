"""Cahier Hub contract tests.

Run with the repo venv (fastapi + httpx live there):

    /home/smoothmarx/.hermes/hermes-agent/venv/bin/python3 -m pytest \
        ~/.hermes/plugins/cahier-hub/tests/test_cahier_hub.py -q

These assert CONTRACTS, not snapshots: the panel's payload must agree with the
control plane, must be deterministic, and must not write anything. A new cahier
appearing or a state flipping must never break a test here.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

HERE = Path(__file__).resolve().parent
DASHBOARD = HERE.parent / "dashboard"
API_FILE = DASHBOARD / "plugin_api.py"
CTL_FILE = Path.home() / ".hermes" / "scripts" / "cahier_ctl.py"
PREFIX = "/api/plugins/cahier-hub"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, module)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def api():
    return _load("cahier_hub_api", API_FILE)


@pytest.fixture(scope="module")
def ctl():
    if not CTL_FILE.is_file():
        pytest.skip(f"control plane missing at {CTL_FILE}")
    return _load("cahier_ctl_test", CTL_FILE)


@pytest.fixture(scope="module")
def client(api):
    app = FastAPI()
    app.include_router(api.router, prefix=PREFIX)
    return TestClient(app)


def _digest(path: Path) -> str:
    if not path.is_file():
        return "absent"
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


# ------------------------------------------------------------------ contracts


def test_health_reports_a_working_control_plane(client, ctl):
    body = client.get(f"{PREFIX}/health").json()
    assert body["ok"] is True
    assert body["control_plane"]["loaded"] is True, body["control_plane"]["error"]
    assert body["degraded"] is False
    assert body["registry"]["entries"] == len(ctl.load_registry()["cahiers"])


def test_active_scope_is_exactly_the_control_plane_active_set(client, ctl):
    body = client.get(f"{PREFIX}/list", params={"scope": "active"}).json()
    expected = {r["slug"] for r in ctl.iteration(scope="active")["rows"]}
    got = {r["slug"] for r in body["rows"]}
    assert got == expected, f"panel drifted from control plane: {got ^ expected}"
    assert body["shown"] == len(body["rows"])
    assert all(r["state"] in ("live", "pending") for r in body["rows"])


def test_list_is_deterministic_and_has_no_duplicates(client, api):
    first = client.get(f"{PREFIX}/list", params={"scope": "all"}).json()["rows"]
    api._cache.clear()  # force a second, independent pass over the filesystem
    second = client.get(f"{PREFIX}/list", params={"scope": "all"}).json()["rows"]
    slugs = [r["slug"] for r in first]
    assert slugs == [r["slug"] for r in second], "iteration order is not stable"
    assert len(set(slugs)) == len(slugs), "duplicate rows in the panel payload"
    assert api._cache, "cache should be repopulated"


def test_no_cahier_is_dropped_relative_to_the_control_plane(client, ctl):
    body = client.get(f"{PREFIX}/list", params={"scope": "all"}).json()
    on_disk = {r["slug"] for r in ctl.build_rows()}
    assert {r["slug"] for r in body["rows"]} == on_disk


def test_every_served_row_is_openable(client):
    body = client.get(f"{PREFIX}/list", params={"scope": "all"}).json()
    served = [r for r in body["rows"] if r["served"]]
    assert served, "expected at least one served cahier on this machine"
    for row in served:
        assert row["url"].startswith("http://")
        assert row["url"].endswith(row["file"])
    assert body["bridge"]["base"].endswith(":8766/")


def test_unknown_scope_falls_back_to_all(client):
    body = client.get(f"{PREFIX}/list", params={"scope": "nonsense"}).json()
    assert body["states"] == list(client.get(f"{PREFIX}/list", params={"scope": "all"}).json()["states"])


def test_single_cahier_lookup_and_404(client):
    body = client.get(f"{PREFIX}/list", params={"scope": "active"}).json()
    slug = body["rows"][0]["slug"]
    one = client.get(f"{PREFIX}/cahier", params={"slug": slug})
    assert one.status_code == 200
    assert one.json()["slug"] == slug
    assert client.get(f"{PREFIX}/cahier", params={"slug": "no-such-cahier"}).status_code == 404


def test_panel_path_writes_nothing(client, ctl):
    """The read-only contract, proven on the artifacts it could damage."""
    watched = {
        "registry": Path(ctl.REGISTRY),
        "answers_db": Path(ctl.ANSWERS_DB),
    }
    before = {name: _digest(path) for name, path in watched.items()}
    for scope in ("active", "all", "finished"):
        client.get(f"{PREFIX}/list", params={"scope": scope})
    client.get(f"{PREFIX}/health")
    after = {name: _digest(path) for name, path in watched.items()}
    assert before == after, f"a GET mutated state: {before} -> {after}"


def test_integrity_notes_surface_in_the_panel_payload(client):
    body = client.get(f"{PREFIX}/list", params={"scope": "all"}).json()
    notes = body["integrity"]["warnings"]
    assert isinstance(notes, list)
    unindexed = body["integrity"]["unindexed_inbox"]
    if unindexed:
        assert any("never recorded" in n for n in notes), "lost saves must be visible in the panel"
