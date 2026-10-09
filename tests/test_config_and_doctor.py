"""Cahier Hub — configuration, control-plane discovery and doctor contracts.

The plugin is meant to run on someone else's machine, where the author's paths do
not exist. These tests therefore build a throwaway ``HERMES_HOME`` and assert the
plugin still finds a control plane, still reads settings, and still says out loud
when a deployment is broken.

    $HOME/.hermes/hermes-agent/venv/bin/python3 -m pytest \
        ~/.hermes/plugins/cahier-hub/tests/test_config_and_doctor.py -q
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

HERE = Path(__file__).resolve().parent
PLUGIN = HERE.parent
API_FILE = PLUGIN / "dashboard" / "plugin_api.py"
PREFIX = "/api/plugins/cahier-hub"

# Every env var the settings layer reads: cleared per test so a developer's own
# shell cannot change the result.
ENV_VARS = (
    "CAHIER_HUB_BRIDGE_PORT",
    "CAHIER_HUB_CACHE_TTL",
    "CAHIER_HUB_BASE_URL",
    "CAHIER_HUB_GROUPS",
    "CAHIER_HUB_CTL",
    "CAHIER_HUB_HOSTING_MODE",
    "CAHIER_HUB_NOTIFY_CHANNEL",
    "CAHIER_HUB_USERS",
)


def load_api():
    """A fresh copy of the backend module — its caches are module-level."""
    name = "cahier_hub_api_cfg"
    spec = importlib.util.spec_from_file_location(name, API_FILE)
    module = importlib.util.module_from_spec(spec)
    sys.modules.pop(name, None)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def api(world):
    return load_api()


@pytest.fixture
def world(tmp_path, monkeypatch):
    """An empty HERMES_HOME: the generic-environment case, nothing preinstalled."""
    home = tmp_path / "hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    for var in ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    return home


def write_config(home: Path, body: str) -> None:
    (home / "config.yaml").write_text(body, encoding="utf-8")


def write_chat(home: Path, data: dict) -> None:
    (home / "cahier-hub.json").write_text(json.dumps(data), encoding="utf-8")


# ----------------------------------------------------------------- precedence


def test_defaults_when_nothing_is_configured(api, world):
    cfg = api.settings()
    assert cfg["bridge_port"] == 8766
    assert cfg["hosting_mode"] == "lan"
    assert api._settings_layers["bridge_port"] == "default"


def test_config_yaml_beats_the_chat_answers(api, world):
    write_chat(world, {"bridge_port": 9001})
    write_config(world, """
plugins:
  entries:
    cahier-hub:
      settings:
        bridge_port: 9002
""")
    cfg = api.settings()
    assert cfg["bridge_port"] == 9002
    assert api._settings_layers["bridge_port"] == "config.yaml"


def test_env_beats_everything(api, world, monkeypatch):
    write_chat(world, {"bridge_port": 9001})
    write_config(world, """
plugins:
  entries:
    cahier-hub:
      settings:
        bridge_port: 9002
""")
    monkeypatch.setenv("CAHIER_HUB_BRIDGE_PORT", "9003")
    assert api.settings()["bridge_port"] == 9003
    assert api._settings_layers["bridge_port"] == "env:CAHIER_HUB_BRIDGE_PORT"


def test_only_our_settings_block_is_read(api, world):
    """Another plugin's settings must not bleed in through the scan."""
    write_config(world, """
plugins:
  entries:
    some-other-plugin:
      settings:
        bridge_port: 1234
    cahier-hub:
      settings:
        bridge_port: 4321
""")
    assert api.settings()["bridge_port"] == 4321


def test_scalars_survive_the_yaml_scan(api, world):
    write_config(world, """
plugins:
  entries:
    cahier-hub:
      settings:
        pin_enabled: true
        cache_ttl: 2.5
        notify_channel: "telegram"
        users: [{"name": "Alex"}]
""")
    cfg = api.settings()
    assert cfg["pin_enabled"] is True
    assert cfg["cache_ttl"] == 2.5
    assert cfg["notify_channel"] == "telegram"
    assert cfg["users"] == [{"name": "Alex"}]


def test_env_only_list_setting_is_parsed(api, world, monkeypatch):
    monkeypatch.setenv("CAHIER_HUB_USERS", '[{"name": "Sam"}]')
    assert api.settings()["users"] == [{"name": "Sam"}]


def test_legacy_env_names_still_work(api, world, monkeypatch):
    monkeypatch.setenv("CAHIER_HUB_GROUPS", "/tmp/filings.json")
    assert str(api._groups_path()) == "/tmp/filings.json"


# -------------------------------------------------------------- control plane


def test_bundled_control_plane_covers_a_fresh_install(api, world):
    """No $HERMES_HOME/scripts copy: the bundled one must load, not degrade."""
    assert not (world / "scripts" / "cahier_ctl.py").exists()
    module = api.ctl()
    assert module is not None, api._ctl_error
    assert api._ctl_source == "bundled"
    assert callable(getattr(module, "iteration", None))
    assert callable(getattr(module, "spin", None))


def test_a_local_control_plane_wins_over_the_bundled_one(api, world):
    scripts = world / "scripts"
    scripts.mkdir()
    (scripts / "cahier_ctl.py").write_text(
        "REGISTRY = '/nowhere/registry.json'\nGROUPS = '/nowhere/groups.json'\n"
        "def iteration(scope='all'):\n    return {'rows': [], 'scope': scope}\n",
        encoding="utf-8")
    module = api.ctl()
    assert module is not None
    assert api._ctl_source == "hermes-home"
    assert module.iteration()["scope"] == "all"


def test_configured_path_beats_the_search(api, world):
    elsewhere = world / "elsewhere"
    elsewhere.mkdir()
    target = elsewhere / "ctl.py"
    target.write_text("def iteration(scope='all'):\n    return {'rows': []}\n", encoding="utf-8")
    write_chat(world, {"control_plane": str(target)})
    assert api.ctl() is not None
    assert api._ctl_source == "configured"
    assert Path(api._ctl_path()) == target


def test_hermes_home_moves_the_state_paths(api, world):
    """A control plane hardcoding ~/.hermes must be re-pointed at HERMES_HOME."""
    module = api.ctl()
    assert module is not None
    assert str(module.STATE) == str(world / "state")
    assert str(module.REGISTRY) == str(world / "state" / "cahier-registry.json")


def test_share_and_projects_dirs_are_configurable(api, world):
    write_chat(world, {"share_dir": "/tmp/served-here", "projects_root": "/tmp/projects-here"})
    module = api.ctl()
    assert str(module.SERVE) == "/tmp/served-here"
    assert str(module.PROJECTS) == "/tmp/projects-here"


# --------------------------------------------------------------------- doctor


def test_doctor_fails_when_loopback_is_handed_to_other_people(api, world):
    """The deployment bug that looks fine locally: answerers get 127.0.0.1."""
    write_chat(world, {"base_url": "http://127.0.0.1:8766/", "hosting_mode": "lan"})
    report = api.doctor_report()
    base = next(c for c in report["checks"] if c["name"] == "base url")
    assert base["status"] == "fail"
    assert report["ok"] is False
    assert report["fails"] >= 1


def test_doctor_accepts_loopback_for_a_local_only_install(api, world):
    write_chat(world, {"base_url": "http://127.0.0.1:8766/", "hosting_mode": "local"})
    report = api.doctor_report()
    assert next(c for c in report["checks"] if c["name"] == "base url")["status"] == "ok"


def test_doctor_accepts_a_lan_address(api, world):
    write_chat(world, {"base_url": "http://192.168.1.20:8766/", "hosting_mode": "lan"})
    report = api.doctor_report()
    assert next(c for c in report["checks"] if c["name"] == "base url")["status"] == "ok"


def test_doctor_reports_the_onboarding_questions(api, world):
    write_chat(world, {"notify_channel": "telegram", "hosting_mode": "public",
                       "users": [{"name": "Alex"}]})
    report = api.doctor_report()
    assert next(c for c in report["checks"] if c["name"] == "onboarding")["status"] == "ok"


def test_doctor_says_which_layer_every_setting_came_from(api, world):
    write_chat(world, {"notify_channel": "telegram"})
    report = api.doctor_report()
    assert report["settings"]["layers"]["notify_channel"] == "chat"
    assert report["settings"]["layers"]["bridge_port"] == "default"


def test_doctor_never_writes(api, world):
    before = sorted(p.relative_to(world) for p in world.rglob("*"))
    api.doctor_report()
    after = sorted(p.relative_to(world) for p in world.rglob("*"))
    assert before == after


def test_doctor_route_is_registered(api, world):
    app = FastAPI()
    app.include_router(api.router, prefix=PREFIX)
    body = TestClient(app).get(f"{PREFIX}/doctor").json()
    assert "checks" in body and "settings" in body
    assert body["version"] == api.VERSION


def test_health_names_the_control_plane_source(api, world):
    app = FastAPI()
    app.include_router(api.router, prefix=PREFIX)
    body = TestClient(app).get(f"{PREFIX}/health").json()
    assert body["control_plane"]["loaded"] is True
    assert body["control_plane"]["source"] == "bundled"
    assert body["settings"]["layers"]["bridge_port"] == "default"
