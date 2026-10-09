"""Cahier Hub contract tests.

Run with the repo venv (fastapi + httpx live there):

    $HOME/.hermes/hermes-agent/venv/bin/python3 -m pytest \
        ~/.hermes/plugins/cahier-hub/tests/test_cahier_hub.py -q

These assert CONTRACTS, not snapshots: the panel's payload must agree with the
control plane, must be deterministic, and must not write anything. A new cahier
appearing or a state flipping must never break a test here.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
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
    # Active means "on the bridge right now" — a lifecycle, not a phase.
    assert all(r["lifecycle"] == "serving" for r in body["rows"]), \
        f"active scope leaked a row that is not serving: {[r['slug'] for r in body['rows'] if r['lifecycle'] != 'serving']}"
    assert body["states"] == ["serving"]


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
    for scope in ("active", "paused", "all", "finished"):
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


# -------------------------------------------------------------------- filing


SOURCES = {"override", "declared", "session", "derived", "none"}


def test_every_row_carries_a_filed_profile_and_project(client, ctl):
    """Grouping needs both labels on every row — including the ones nobody has
    answered for, which say 'none' instead of inventing a bucket."""
    body = client.get(f"{PREFIX}/list", params={"scope": "all"}).json()
    assert body["rows"], "expected cahiers on this machine"
    for row in body["rows"]:
        assert isinstance(row.get("profile"), str)
        assert isinstance(row.get("project"), str)
        assert row.get("profile_source") in SOURCES, row
        assert row.get("project_source") in SOURCES, row
    vocab = body["vocab"]
    assert "default" in vocab["profiles"]
    assert isinstance(vocab["projects"], list) and vocab["projects"]


def test_filing_write_lands_in_one_file_and_survives_a_relist(client, ctl, monkeypatch, tmp_path):
    """The panel's ✎ is the only write path, it goes to the groups file alone, and
    the very next GET must show it — no cache standing between the human and the
    answer."""
    target = tmp_path / "cahier-groups.json"
    monkeypatch.setenv("CAHIER_HUB_GROUPS", str(target))
    body = client.get(f"{PREFIX}/list", params={"scope": "all"}).json()
    slug = body["rows"][0]["slug"]

    resp = client.post(f"{PREFIX}/filing",
                       json={"slug": slug, "profile": "dobbs", "project": "Demo"})
    assert resp.status_code == 200, resp.text
    assert target.is_file(), "the override must land in the file the resolver reads"
    on_disk = json.loads(target.read_text())
    assert on_disk["cahiers"][slug] == {"profile": "dobbs", "project": "Demo"}
    assert on_disk["updated_by"] == "panel"

    rows = {r["slug"]: r for r in client.get(f"{PREFIX}/list", params={"scope": "all"}).json()["rows"]}
    assert rows[slug]["profile"] == "dobbs"
    assert rows[slug]["project"] == "Demo"
    assert rows[slug]["profile_source"] == "override"
    assert rows[slug]["project_source"] == "override"

    # An empty string FORGETS the override and hands the question back to the resolver.
    assert client.post(f"{PREFIX}/filing",
                       json={"slug": slug, "profile": "", "project": ""}).status_code == 200
    assert json.loads(target.read_text())["cahiers"] == {}
    rows = {r["slug"]: r for r in client.get(f"{PREFIX}/list", params={"scope": "all"}).json()["rows"]}
    assert rows[slug]["profile_source"] != "override"


def test_filing_refuses_unknown_slugs_and_bad_input(client, monkeypatch, tmp_path):
    monkeypatch.setenv("CAHIER_HUB_GROUPS", str(tmp_path / "g.json"))
    assert client.post(f"{PREFIX}/filing", json={}).status_code == 400
    assert client.post(f"{PREFIX}/filing", json={"slug": "no-such-cahier"}).status_code == 404
    assert client.post(f"{PREFIX}/filing",
                       json={"slug": "../../etc/passwd", "profile": "x"}).status_code == 404
    body = client.get(f"{PREFIX}/list", params={"scope": "all"}).json()
    slug = body["rows"][0]["slug"]
    assert client.post(f"{PREFIX}/filing",
                       json={"slug": slug, "profile": "x" * 200}).status_code == 400
    assert not (tmp_path / "g.json").exists(), "a rejected write must leave nothing behind"


def test_reading_never_touches_the_groups_file(client, ctl, monkeypatch, tmp_path):
    """The filing file is written by a human click and by nothing else."""
    target = tmp_path / "g.json"
    target.write_text('{"version": 1, "cahiers": {"kept": {"profile": "default"}}}')
    monkeypatch.setenv("CAHIER_HUB_GROUPS", str(target))
    before = target.read_text()
    for scope in ("active", "paused", "all", "finished"):
        client.get(f"{PREFIX}/list", params={"scope": scope})
    client.get(f"{PREFIX}/health")
    assert target.read_text() == before


def test_override_beats_every_automatic_answer(ctl, monkeypatch, tmp_path):
    """A derived or session answer is a default, never a verdict: the override wins.

    Hermetic on purpose: `derived` is a keyword match against the projects THIS
    machine has, so the test brings its own vocabulary instead of borrowing the
    live registry — otherwise it only passes on the machine that built it.
    """
    projects, share = tmp_path / "Projects", tmp_path / "share"
    (projects / "Ops").mkdir(parents=True)
    share.mkdir()
    (share / "fleet-status.json").write_text(
        json.dumps({"projects": [{"name": "Ops"}, {"name": "Atlas"}]}))
    monkeypatch.setattr(ctl, "PROJECTS", str(projects))
    monkeypatch.setattr(ctl, "SERVE", str(share))
    monkeypatch.setenv("CAHIER_HUB_GROUPS", str(tmp_path / "g.json"))
    ctl._VOCAB_CACHE.clear()

    slug = "ops-restructure"
    auto = ctl.filing(slug, meta={"declared_profile": "", "declared_project": ""})
    assert (auto["project"], auto["project_source"]) == ("Ops", "derived")
    assert ctl.set_filing(slug, project="Client Work", actor="test")["ok"]
    now = ctl.filing(slug, meta={})
    assert (now["project"], now["project_source"]) == ("Client Work", "override")


def test_declared_profile_wins_over_the_build_session(ctl):
    """A page that stamps its own profile is believed over the session lookup."""
    both = ctl.filing("declared-slug", meta={"declared_profile": "cody",
                                             "origin_session": "20260820_114128_a3c89b74"})
    assert (both["profile"], both["profile_source"]) == ("cody", "declared")
    session = ctl.filing("session-slug", meta={"origin_session": "20260820_114128_a3c89b74"})
    assert (session["profile"], session["profile_source"]) == ("default", "session")

# ----------------------------------------------------------------- start / stop
# Rule (2026-10-03): a cahier is SAVED in its own project folder, under a
# `Cahiers/` subfolder, and the panel's Start/Stop serves or parks it from there
# in its ORIGINAL settings. Proven on a scratch world, never on the live one.


def _two_way_page(slug, project=""):
    """The minimum the control plane reads: a SLUG, a /save marker, the project
    the builder stamps in, and a real (non-template) title."""
    return (f'<!doctype html><html><head><title>{slug} answers</title></head><body>\n'
            f'<script>\nconst SLUG = "{slug}";\nconst CAHIER_PROJECT = "{project}";\n'
            f'const CAHIER_PROFILE = "default";\nconst PUSH_ENDPOINT = "save";\n'
            f'const USERS = {{"1111": "Alex"}};\n/* the page pushes to /save */\n'
            f'</script>\n</body></html>\n').encode()


@pytest.fixture
def scratch_world(ctl, monkeypatch, tmp_path):
    """A whole cahier world under tmp_path: share, projects, registry, answers."""
    share, projects, retired = tmp_path / "share", tmp_path / "Projects", tmp_path / "retired"
    state = tmp_path / "state"
    for d in (share, projects, retired, state):
        d.mkdir()
    monkeypatch.setattr(ctl, "SERVE", str(share))
    monkeypatch.setattr(ctl, "PROJECTS", str(projects))
    monkeypatch.setattr(ctl, "RETIRED", str(retired))
    monkeypatch.setattr(ctl, "STATE", str(state))
    monkeypatch.setattr(ctl, "REGISTRY", str(state / "cahier-registry.json"))
    monkeypatch.setattr(ctl, "GROUPS", str(state / "cahier-groups.json"))
    monkeypatch.setattr(ctl, "DEADLINE", str(state / "cahier-deadline.json"))
    monkeypatch.setattr(ctl, "ANSWERS_DB", str(state / "cahier-answers.db"))
    monkeypatch.setattr(ctl, "CONTROL_LOG", str(state / "cahier-control.log"))
    monkeypatch.setenv("CAHIER_HUB_GROUPS", str(state / "cahier-groups.json"))
    # never touch the real systemd timer from a test
    monkeypatch.setenv("CAHIER_CTL_NO_ARM", "1")
    ctl._VOCAB_CACHE.clear()
    ctl._GROUPS_CACHE.clear()
    return {"share": share, "projects": projects, "retired": retired, "state": state}


def test_cahier_home_is_the_project_folder_or_the_projects_root(ctl, scratch_world):
    projects = scratch_world["projects"]
    (projects / "Demo").mkdir()
    assert ctl.project_dir("Demo") == str(projects / "Demo")
    assert ctl.project_dir("demo") == str(projects / "Demo")  # labels are typed by hand
    assert ctl.project_dir("no-such-project") == ""
    assert ctl.cahier_home("Demo") == str(projects / "Demo" / "Cahiers")
    assert ctl.cahier_home("") == str(projects / "Cahiers")
    assert ctl.cahier_home("no-such-project") == str(projects / "Cahiers")
    # reads never create: the panel's GETs must not touch the disk
    assert not (projects / "Cahiers").exists()
    assert ctl.cahier_home("", create=True) == str(projects / "Cahiers")
    assert (projects / "Cahiers").is_dir()


def test_spin_down_files_the_page_into_its_project_folder_and_up_restores_it(ctl, scratch_world):
    share, projects = scratch_world["share"], scratch_world["projects"]
    (projects / "Demo").mkdir()
    page = share / "demo-stock-check.html"
    page.write_bytes(_two_way_page("demo-stock-check", "Demo"))

    row = next(r for r in ctl.build_rows() if r["slug"] == "demo-stock-check")
    assert row["served"] is True and row["action"] == "down" and row["controllable"] is True
    assert row["lifecycle"] == "serving" and row["actions"] == ["pause", "close"], row
    assert row["home"] == str(projects / "Demo" / "Cahiers"), row["home"]
    assert row["home_path"] == str(projects / "Demo" / "Cahiers" / "demo-stock-check.html")

    down = ctl.spin_down("demo-stock-check", apply=True, actor="test")
    assert down["ok"] is True, down
    home_file = projects / "Demo" / "Cahiers" / "demo-stock-check.html"
    assert home_file.is_file(), down
    assert not page.exists() and os.listdir(share) == []
    assert down["home"] == str(projects / "Demo" / "Cahiers")

    ctl.sync_registry(ctl.build_rows(), note="test")
    assert ctl.saved_settings("demo-stock-check")["file"] == "demo-stock-check.html"

    # parked, and the control plane now offers the way back
    row = next(r for r in ctl.build_rows() if r["slug"] == "demo-stock-check")
    assert row["state"] == "pending" and row["served"] is False and row["action"] == "up"
    assert row["lifecycle"] == "closed" and row["actions"] == ["up"], row

    up = ctl.spin_up("demo-stock-check", apply=True, actor="test")
    assert up["ok"] is True, up
    assert page.is_file() and not home_file.exists()
    assert up["settings"]["page"].startswith("restored"), up["settings"]


def test_spin_down_creates_the_cahiers_folder_and_falls_back_to_the_projects_root(ctl, scratch_world):
    """No project folder yet -> ~/Projects/Cahiers, created by the save itself."""
    share, projects = scratch_world["share"], scratch_world["projects"]
    (share / "orphan-check.html").write_bytes(_two_way_page("orphan-check", ""))

    row = next(r for r in ctl.build_rows() if r["slug"] == "orphan-check")
    assert row["project"] == "" and row["home"] == str(projects / "Cahiers")

    out = ctl.spin_down("orphan-check", apply=True, actor="test")
    assert out["ok"] is True, out
    assert (projects / "Cahiers" / "orphan-check.html").is_file(), out


def test_a_cahiers_folder_declares_its_pages_whatever_they_are_called(ctl, scratch_world):
    """Inside `Cahiers/`, any .html is a cahier; elsewhere the name has to say so."""
    projects = scratch_world["projects"]
    (projects / "Demo" / "Cahiers").mkdir(parents=True)
    (projects / "Demo" / "Cahiers" / "stock.html").write_bytes(_two_way_page("stock", "Demo"))
    (projects / "Demo" / "notes.html").write_bytes(_two_way_page("notes", "Demo"))

    found = ctl.scan_pending()
    assert "stock" in found, "a page in a Cahiers folder is a cahier by location"
    assert "notes" not in found, "a page loose in the project tree still needs a telling name"


def test_the_action_route_spins_once_and_refuses_everything_else(client, api, monkeypatch):
    """The route's whole job: validate first, move second. A refusal moves nothing."""
    class FakeCtl:
        NEVER = {"fleet-status"}

        def __init__(self):
            self.calls = []

        def iteration(self, scope="all"):
            rows = [
                {"slug": "live-one", "state": "live", "lifecycle": "serving", "controllable": True,
                 "action": "down", "actions": ["pause", "close"],
                 "served": True, "file": "live-one.html", "home": "/tmp/x"},
                {"slug": "parked-one", "state": "pending", "lifecycle": "closed", "controllable": True,
                 "action": "up", "actions": ["up"],
                 "served": False, "file": "parked-one.html", "home": "/tmp/x"},
                {"slug": "paused-one", "state": "pending", "lifecycle": "paused", "controllable": True,
                 "action": "up", "actions": ["up", "close"],
                 "served": False, "file": "paused-one.html", "home": "/tmp/x"},
                {"slug": "fleet-status", "state": "pending", "lifecycle": "closed", "controllable": True,
                 "action": "up", "actions": ["up"],
                 "served": False, "file": "fleet-status.html", "home": "/tmp/x"},
                {"slug": "viewer-only", "state": "pending", "lifecycle": "closed", "controllable": False,
                 "action": "none", "actions": [],
                 "served": False, "file": "viewer-only.html", "home": "/tmp/x"},
            ]
            return {"generated_at": "now", "scope": scope, "states": ["live", "pending"],
                    "shown": len(rows), "rows": rows,
                    "counts": {"live": 1, "pending": 3, "total": 4},
                    "vocab": {"profiles": ["default"], "projects": []},
                    "registry": {"path": "x", "updated": "", "entries": 4},
                    "integrity": {"warnings": [], "duplicates": [], "unindexed_inbox": [],
                                  "served_without_slug": [], "unregistered": [],
                                  "stale_registry_states": []},
                    "degraded": False}

        def spin(self, slug, action, apply=True, actor="cli"):
            self.calls.append({"slug": slug, "action": action, "apply": apply, "actor": actor})
            return {"ok": True, "slug": slug, "state": "pending", "settings": {}, "notes": ["moved"]}

        def build_rows(self):
            return []

        def sync_registry(self, rows, note=""):
            return {"ok": True}

    fake = FakeCtl()
    monkeypatch.setattr(api, "ctl", lambda: fake)
    monkeypatch.setattr(api, "_cache", {})

    assert client.post(f"{PREFIX}/action", json={}).status_code == 400
    assert client.post(f"{PREFIX}/action", json={"slug": "live-one"}).status_code == 400
    assert client.post(f"{PREFIX}/action",
                       json={"slug": "live-one", "action": "sideways"}).status_code == 400
    assert client.post(f"{PREFIX}/action", json={"slug": "nope", "action": "up"}).status_code == 404
    assert client.post(f"{PREFIX}/action",
                       json={"slug": "fleet-status", "action": "down"}).status_code == 400
    assert client.post(f"{PREFIX}/action",
                       json={"slug": "viewer-only", "action": "up"}).status_code == 400
    assert client.post(f"{PREFIX}/action",
                       json={"slug": "parked-one", "action": "down"}).status_code == 400
    assert client.post(f"{PREFIX}/action",
                       json={"slug": "live-one", "action": "up"}).status_code == 400
    # the three verbs, refused in the directions their row does not allow:
    # paused is already off the bridge, so there is nothing left to pause or close
    assert client.post(f"{PREFIX}/action",
                       json={"slug": "parked-one", "action": "pause"}).status_code == 400
    assert client.post(f"{PREFIX}/action",
                       json={"slug": "paused-one", "action": "pause"}).status_code == 400
    assert client.post(f"{PREFIX}/action",
                       json={"slug": "paused-one", "action": "down"}).status_code == 200
    assert fake.calls == [{"slug": "paused-one", "action": "down", "apply": True, "actor": "panel"}], \
        f"close from a paused row is legal: {fake.calls}"
    fake.calls.clear()

    ok = client.post(f"{PREFIX}/action", json={"slug": "live-one", "action": "pause"})
    assert ok.status_code == 200, ok.text
    assert fake.calls == [{"slug": "live-one", "action": "pause", "apply": True, "actor": "panel"}]
    body = ok.json()
    assert body["written_by"] == "panel" and body["ok"] is True
    assert body["row"]["slug"] == "live-one"
    fake.calls.clear()
    # close from a PAUSED row is legal: the page is already off the bridge
    ok = client.post(f"{PREFIX}/action", json={"slug": "paused-one", "action": "close"})
    assert ok.status_code == 200, ok.text
    assert fake.calls == [{"slug": "paused-one", "action": "close", "apply": True, "actor": "panel"}]
    assert ok.json()["row"]["slug"] == "paused-one"
    fake.calls.clear()
    # the old name still works, and it is close
    ok = client.post(f"{PREFIX}/action", json={"slug": "live-one", "action": "down"})
    assert ok.status_code == 200, ok.text
    assert fake.calls == [{"slug": "live-one", "action": "down", "apply": True, "actor": "panel"}]


# --------------------------------------------------------------- pause / close
# Three verbs, two notes. Pause and close are the SAME file move with a different
# registry note (only the registry can tell them apart: the page is off the bridge
# either way), and close from a paused row has no file left to move at all.
# Proven on the scratch world, never on a live page.


def test_pause_moves_the_page_and_keeps_the_row_active(ctl, scratch_world):
    share, projects = scratch_world["share"], scratch_world["projects"]
    (projects / "Demo").mkdir()
    page = share / "demo-poll.html"
    page.write_bytes(_two_way_page("demo-poll", "Demo"))
    ctl.sync_registry(ctl.build_rows(), note="test")
    assert next(r for r in ctl.build_rows() if r["slug"] == "demo-poll")["lifecycle"] == "serving"

    out = ctl.spin("demo-poll", "pause", apply=True, actor="test")
    assert out["ok"] is True and out["lifecycle"] == "paused", out
    assert not page.exists() and (projects / "Demo" / "Cahiers" / "demo-poll.html").is_file()

    row = next(r for r in ctl.build_rows() if r["slug"] == "demo-poll")
    assert row["lifecycle"] == "paused" and row["actions"] == ["up", "close"], row
    assert row["served"] is False and row["controllable"] is True
    # the pills follow the note
    assert [r["slug"] for r in ctl.iteration("paused")["rows"]] == ["demo-poll"]
    assert ctl.iteration("active")["rows"] == []
    assert ctl.load_registry()["cahiers"]["demo-poll"]["lifecycle"] == "paused"
    # and pausing twice is refused, not repeated
    assert ctl.spin("demo-poll", "pause", apply=True, actor="test")["ok"] is False


def test_close_from_paused_only_rewrites_the_note(ctl, scratch_world):
    share, projects = scratch_world["share"], scratch_world["projects"]
    (projects / "Demo").mkdir()
    (share / "demo-poll.html").write_bytes(_two_way_page("demo-poll", "Demo"))
    ctl.sync_registry(ctl.build_rows(), note="test")
    ctl.spin("demo-poll", "pause", apply=True, actor="test")
    moved = projects / "Demo" / "Cahiers" / "demo-poll.html"
    before = moved.read_bytes()

    out = ctl.spin("demo-poll", "close", apply=True, actor="test")
    assert out["ok"] is True and out["lifecycle"] == "closed", out
    assert moved.read_bytes() == before, "closing a paused cahier must not touch the file"
    row = next(r for r in ctl.build_rows() if r["slug"] == "demo-poll")
    assert row["lifecycle"] == "closed" and row["actions"] == ["up"], row
    assert [r["slug"] for r in ctl.iteration("finished")["rows"]] == ["demo-poll"]
    assert ctl.iteration("paused")["rows"] == []
    assert ctl.spin("demo-poll", "close", apply=True, actor="test")["ok"] is False
    assert ctl.spin("demo-poll", "pause", apply=True, actor="test")["ok"] is False


def test_spin_up_clears_the_note_so_the_row_is_plainly_serving_again(ctl, scratch_world):
    share, projects = scratch_world["share"], scratch_world["projects"]
    (projects / "Demo").mkdir()
    page = share / "demo-poll.html"
    page.write_bytes(_two_way_page("demo-poll", "Demo"))
    ctl.sync_registry(ctl.build_rows(), note="test")
    ctl.spin("demo-poll", "pause", apply=True, actor="test")

    out = ctl.spin("demo-poll", "up", apply=True, actor="test")
    assert out["ok"] is True and page.is_file(), out
    entry = ctl.load_registry()["cahiers"]["demo-poll"]
    assert "lifecycle" not in entry, entry
    assert next(r for r in ctl.build_rows() if r["slug"] == "demo-poll")["lifecycle"] == "serving"


def test_down_is_still_the_old_name_for_close(ctl, scratch_world):
    share, projects = scratch_world["share"], scratch_world["projects"]
    (projects / "Demo").mkdir()
    (share / "demo-poll.html").write_bytes(_two_way_page("demo-poll", "Demo"))
    out = ctl.spin("demo-poll", "down", apply=True, actor="test")
    assert out["ok"] is True and out["lifecycle"] == "closed", out
    assert ctl.spin("demo-poll", "sideways", apply=True, actor="test")["ok"] is False


def test_the_scope_pills_follow_the_lifecycle(client, ctl):
    """active = on the bridge, paused = parked on purpose, finished = done / never."""
    body = {s: client.get(f"{PREFIX}/list", params={"scope": s}).json()
            for s in ("active", "paused", "finished", "all")}
    assert body["active"]["states"] == ["serving"]
    assert body["paused"]["states"] == ["paused"]
    assert all(r["lifecycle"] == "serving" for r in body["active"]["rows"])
    assert all(r["lifecycle"] == "paused" for r in body["paused"]["rows"])
    active = {r["slug"] for r in body["active"]["rows"]}
    paused = {r["slug"] for r in body["paused"]["rows"]}
    everything = {r["slug"] for r in body["all"]["rows"]}
    assert not (active & paused), "a row cannot be serving and paused at once"
    assert active <= everything and paused <= everything
    for row in body["all"]["rows"]:
        key = ctl.scope_key(row)
        assert key in ctl.ALL_STATES, f"{row['slug']} falls outside every scope: {key!r}"
        assert bool(row["actions"]) or not row["controllable"], row["slug"]
