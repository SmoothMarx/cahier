"""Cahier — backend for the desktop sidebar page.

Every number the panel shows comes from ``cahier_ctl.iteration()``: the same
read-only call the CLI's ``list`` subcommand uses, so the page and the terminal
can never disagree about what a cahier IS.

GETs never write — not to the share directory, not to the registry, not to the
answers DB. Two POSTs exist, and both are a human's click:

* ``POST /filing`` — the ✎ on a row, saying which profile/project a cahier
  belongs to (``cahier_ctl.set_filing``: validated, atomic, logged).
* ``POST /action`` — the row's Start/Stop, spinning the cahier up (published
  again from its project folder, in its ORIGINAL settings) or down (off the
  bridge and back into <project>/Cahiers). It goes through ``cahier_ctl.spin``.

When ``cahier_ctl.py`` cannot be imported it degrades to the registry file alone
and says so in ``integrity.warnings`` instead of pretending the list is complete.

Configuration is layered, most specific first, so the same build works on a
stranger's machine and on the one it was written on:

1. ``CAHIER_<KEY>`` environment variables (ops; ``CAHIER_GROUPS``,
   ``CAHIER_BASE_URL``, ``CAHIER_CTL`` keep their historical names),
2. ``plugins.entries.cahier.settings.<key>`` in ``$HERMES_HOME/config.yaml``
   — the Desktop Plugins settings form, driven by ``config_schema``,
3. ``$HERMES_HOME/cahier.json`` — the answers the agent recorded while
   onboarding in a session chat,
4. the built-in defaults below.

``GET /doctor`` names the layer every value came from, so "why is it using that
port" is a lookup, not an investigation.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query, Request

VERSION = "0.5.0"
PROBE_TTL = 10.0

# Every knob the panel reads. `config_schema` in plugin.yaml describes the same
# keys; keep the two in step — a key here that is missing there is invisible to
# the Desktop form, and one there that is missing here does nothing.
DEFAULTS: dict[str, Any] = {
    "bridge_port": 8766,
    "cache_ttl": 5.0,
    "base_url": "",          # the URL answerers actually open; "" = derive from the request host
    "control_plane": "",     # explicit path to cahier_ctl.py; "" = search
    "groups_file": "",       # where filings live; "" = the control plane's own default
    "share_dir": "",         # the folder the bridge serves; "" = ~/cahier-share
    "projects_root": "",     # where cahiers are filed; "" = ~/Projects
    "hosting_mode": "lan",   # local | lan | public — decided by the onboarding questions
    "timezone": "",          # "" = the host's timezone
    "notify_channel": "none",  # none | telegram | email | webhook
    "notify_target": "",
    "notify_on": "new_answer",  # new_answer | deadline | silent
    "quiet_hours": "",          # "22:00-07:00"; "" = no quiet hours
    "pin_enabled": False,
    "users": [],             # [{"name": "...", "pin": "..."}] — names only; PINs travel in the page
}

# Names each setting accepts, most-preferred first. The plugin was called
# `cahier-hub` until 2026-10-09; the old prefix stays honoured so a deployment
# mid-rename keeps working. `groups_file`/`control_plane` need an explicit entry
# because their derived form (`CAHIER_GROUPS_FILE`) is not the documented name.
# The derived `CAHIER_<KEY>` names are appended by the resolver, so listing them
# here is for the old generation only. Do not drop these without a deprecation cycle.
_ENV_ALIASES = {
    "bridge_port": ("CAHIER_HUB_BRIDGE_PORT",),
    "cache_ttl": ("CAHIER_HUB_CACHE_TTL",),
    "groups_file": ("CAHIER_GROUPS", "CAHIER_HUB_GROUPS"),
    "control_plane": ("CAHIER_CTL", "CAHIER_HUB_CTL"),
}

router = APIRouter()

_cache: dict[str, tuple[float, Any]] = {}
_ctl: Any = None
_ctl_error = ""
_ctl_source = ""
_settings_cache: dict | None = None
_settings_key_cache: tuple | None = None
_settings_layers: dict[str, str] = {}


# --------------------------------------------------------------- control plane


def _home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))


# ------------------------------------------------------------------- settings


def _config_settings() -> dict:
    """`plugins.entries.cahier.settings` from $HERMES_HOME/config.yaml.

    Read with a tiny hand-rolled scan rather than a YAML dependency: the panel
    must not gain an import that the desktop host may not ship. Only the
    indentation of this one block matters, and a miss just means "no settings".
    """
    path = _home() / "config.yaml"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    out: dict[str, Any] = {}
    in_plugins = in_entries = in_ours = in_settings = False
    for raw in lines:
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        line = raw.strip()
        if indent == 0:
            in_plugins = line.startswith("plugins:")
            in_entries = in_ours = in_settings = False
            continue
        if not in_plugins:
            continue
        if indent == 2:
            in_entries = line.startswith("entries:")
            in_ours = in_settings = False
            continue
        if not in_entries:
            continue
        if indent == 4:
            in_ours = line.startswith("cahier:")
            in_settings = False
            continue
        if not in_ours:
            continue
        if indent == 6:
            in_settings = line.startswith("settings:")
            continue
        if in_settings and indent >= 8 and ":" in line:
            key, _, value = line.partition(":")
            out[key.strip()] = _scalar(value.strip())
    return out


def _scalar(text: str) -> Any:
    """Best-effort scalar for a config line: quotes, bools, numbers, lists."""
    t = text.strip()
    if t.startswith(("[", "{", "'", '"')) or t in ("true", "false", "null", ""):
        try:
            return json.loads(t.replace("'", '"')) if t else ""
        except Exception:
            return t.strip("'\"")
    try:
        return int(t)
    except ValueError:
        pass
    try:
        return float(t)
    except ValueError:
        pass
    return t


def _chat_settings() -> dict:
    """`$HERMES_HOME/cahier.json` — what the onboarding questions wrote."""
    try:
        data = json.loads((_home() / "cahier.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _settings_key() -> tuple:
    """Everything the merged settings depend on: paths, file mtimes, env values.

    Caching on a timer instead would let a fresh `config.yaml` (or a test's
    monkeypatched env) sit invisible for seconds — the panel would show a stale
    answer, which is exactly the class of bug this layer exists to avoid.
    """
    def mt(path: Path) -> int:
        try:
            return path.stat().st_mtime_ns
        except OSError:
            return 0

    env = tuple(
        (name, os.environ.get(name, ""))
        for key in sorted(DEFAULTS)
        for name in _ENV_ALIASES.get(key, ()) + (f"CAHIER_{key.upper()}",)
    )
    return (str(_home()), mt(_home() / "config.yaml"), mt(_home() / "cahier.json"), env)


def settings() -> dict:
    """Merged settings + the layer each key came from."""
    global _settings_cache, _settings_key_cache, _settings_layers
    key = _settings_key()
    if _settings_cache is not None and _settings_key_cache == key:
        return _settings_cache
    layers: dict[str, str] = {}
    merged = dict(DEFAULTS)
    for k, value in _chat_settings().items():
        if k in DEFAULTS:
            merged[k] = value
            layers[k] = "chat"            # the session-chat onboarding answers
    for k, value in _config_settings().items():
        if k in DEFAULTS:
            merged[k] = value
            layers[k] = "config.yaml"     # the Desktop Plugins settings form
    for k in DEFAULTS:
        names = _ENV_ALIASES.get(k, ()) + (f"CAHIER_{k.upper()}",)
        for name in names:
            raw = os.environ.get(name)
            if raw not in (None, ""):
                merged[k] = _scalar(raw) if isinstance(DEFAULTS[k], (int, float, bool, list)) else raw
                layers[k] = f"env:{name}"
                break
    for k in DEFAULTS:
        layers.setdefault(k, "default")
    _settings_layers = layers
    _settings_cache = merged
    _settings_key_cache = key
    return merged


def bridge_port() -> int:
    try:
        return int(settings()["bridge_port"])
    except (TypeError, ValueError):
        return int(DEFAULTS["bridge_port"])


def cache_ttl() -> float:
    try:
        return float(settings()["cache_ttl"])
    except (TypeError, ValueError):
        return float(DEFAULTS["cache_ttl"])


def _bundled_ctl() -> Path:
    return Path(__file__).resolve().parent.parent / "lib" / "cahier_ctl.py"


def _ctl_candidates() -> list[tuple[str, Path]]:
    """Where a control plane may live, best first.

    The machine's own copy wins: it is the one the CLI, the fleet page and any
    cron job are already using, and the panel must agree with them. The bundled
    copy is what makes a fresh install work with nothing else set up.
    """
    out: list[tuple[str, Path]] = []
    explicit = str(settings().get("control_plane") or "").strip()
    if explicit:
        out.append(("configured", Path(os.path.expanduser(explicit))))
    out.append(("hermes-home", _home() / "scripts" / "cahier_ctl.py"))
    out.append(("bundled", _bundled_ctl()))
    return out


def _ctl_path() -> Path:
    """The control plane in use (first existing candidate), else the first."""
    candidates = _ctl_candidates()
    for _, path in candidates:
        if path.is_file():
            return path
    return candidates[0][1]


def _apply_home(module: Any) -> list[str]:
    """Re-point the control plane at $HERMES_HOME when it is not ~/.hermes.

    ``cahier_ctl`` hardcodes ``~/.hermes``; every path it derives hangs off that.
    A runner with HERMES_HOME elsewhere would otherwise read one state directory
    and write another, so mirror its own layout. Settings win over the mirror.
    """
    notes: list[str] = []
    home = _home()
    if home == Path.home() / ".hermes":
        return notes
    layout = {
        "H": home,
        "STATE": home / "state",
        "REGISTRY": home / "state" / "cahier-registry.json",
        "DEADLINE": home / "state" / "cahier-deadline.json",
        "ANSWERS_DB": home / "state" / "cahier-answers.db",
        "CONTROL_LOG": home / "state" / "cahier-control.log",
        "RETIRED": home / "state" / "cahier-retired",
        "GROUPS": home / "state" / "cahier-groups.json",
        "PINS": home / "state" / "cahier-template-pins.json",
        "SESSIONS_DB": home / "state.db",
        "TEMPLATES": home / "skills" / "productivity" / "cahier" / "templates",
    }
    cfg = settings()
    if str(cfg.get("share_dir") or "").strip():
        layout["SERVE"] = Path(os.path.expanduser(str(cfg["share_dir"])))
    if str(cfg.get("projects_root") or "").strip():
        layout["PROJECTS"] = Path(os.path.expanduser(str(cfg["projects_root"])))
    if str(cfg.get("groups_file") or "").strip():
        layout["GROUPS"] = Path(os.path.expanduser(str(cfg["groups_file"])))
    for name, value in layout.items():
        if hasattr(module, name):
            setattr(module, name, str(value))
            notes.append(name)
    return notes


def ctl() -> Any:
    """Import cahier_ctl once. None (plus the reason) when it is unusable."""
    global _ctl, _ctl_error, _ctl_source
    if _ctl is not None or _ctl_error:
        return _ctl
    source, path = "", _ctl_path()
    for name, candidate in _ctl_candidates():
        if candidate.is_file():
            source, path = name, candidate
            _ctl_source = source
            break
    if not path.is_file():
        tried = ", ".join(str(p) for _, p in _ctl_candidates())
        _ctl_error = f"cahier_ctl.py not found (looked in: {tried})"
        return None
    try:
        spec = importlib.util.spec_from_file_location("cahier_ctl", path)
        module = importlib.util.module_from_spec(spec)
        sys.modules.setdefault("cahier_ctl", module)
        spec.loader.exec_module(module)
        module._cahier_overrides = _apply_home(module)
        _ctl = module
    except Exception as exc:  # a broken control plane degrades, never 500s
        _ctl_error = f"{type(exc).__name__}: {exc}"
        _ctl = None
    return _ctl


def _ctl_digest(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:12]
    except OSError:
        return "absent"


def _registry_path() -> Path:
    module = ctl()
    stored = getattr(module, "REGISTRY", None) if module else None
    return Path(stored) if stored else _home() / "state" / "cahier-registry.json"


def _groups_path() -> Path:
    """Where the human's filings live. Same precedence as the control plane, so the
    panel can never write a file the resolver does not read."""
    configured = str(settings().get("groups_file") or "").strip()
    if configured:
        return Path(os.path.expanduser(configured))
    module = ctl()
    stored = getattr(module, "GROUPS", None) if module else None
    return Path(stored) if stored else _home() / "state" / "cahier-groups.json"


def _degraded_payload(scope: str) -> dict:
    """No control plane: the registry file is all we have, and we say so."""
    path = _registry_path()
    warnings = [f"cahier_ctl.py unavailable ({_ctl_error or 'not loaded'}) — "
                "showing the registry file only"]
    try:
        reg = json.loads(path.read_text(encoding="utf-8"))
        book = reg.get("cahiers") or {}
        warnings.append("served files were not scanned: the panel may be showing a stale list")
    except Exception as exc:
        book, reg = {}, {}
        warnings.append(f"registry unreadable at {path}: {exc}")

    rows = []
    for slug, ent in sorted(book.items()):
        pin = ent.get("pin") if isinstance(ent.get("pin"), dict) else {}
        served_path = ent.get("path") or ""
        rows.append({
            "slug": slug,
            "title": ent.get("title") or slug,
            "state": ent.get("state") or "unknown",
            "why": ent.get("why") or "",
            "served": bool(ent.get("served")),
            "two_way": bool(ent.get("two_way")),
            "armed": bool(ent.get("armed")),
            "deadline": ent.get("deadline") or "",
            "saves": ent.get("saves") or 0,
            "people": ent.get("people") or 0,
            "last_save": ent.get("last_save") or "",
            "file": Path(served_path).name if served_path else None,
            "file_age": None,
            "path": served_path,
            "home": ent.get("home") or "",
            "home_path": "",
            "settings": ent.get("settings") if isinstance(ent.get("settings"), dict) else {},
            "controllable": bool(ent.get("controllable")),
            "action": ent.get("action") or "",
            # No control plane to derive from: the registry note is all we have, and
            # the buttons it implies are the two the row's own direction allows.
            "lifecycle": ent.get("lifecycle") or "",
            "actions": (["pause", "close"] if ent.get("action") == "down"
                        else ["up"] if ent.get("action") == "up" else []),
            "protected": bool(pin.get("protected")),
            "pin_users": pin.get("users", 0),
            "page_pin": bool(pin.get("page_pin")),
            "first_seen": ent.get("first_seen") or "",
            "last_change": ent.get("last_change") or "",
            "profile": ent.get("profile") or "",
            "project": ent.get("project") or "",
            "profile_source": ent.get("profile_source") or "",
            "project_source": ent.get("project_source") or "",
            "in_registry": True,
        })

    try:
        module = ctl()
        states = list(module.resolve_scope(scope)) if module else ["live", "pending"]
    except Exception:
        states = ["live", "pending"]
    picked = [r for r in rows if r["state"] in states]
    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "scope": scope,
        "states": states,
        "shown": len(picked),
        "rows": picked,
        "vocab": {"profiles": [], "projects": []},
        "counts": {s: sum(1 for r in rows if r["state"] == s) for s in
                   ("live", "pending", "finished", "stopped", "unknown")} | {"total": len(rows)},
        "registry": {"path": str(path), "updated": reg.get("updated", ""), "entries": len(book)},
        "integrity": {"warnings": warnings, "duplicates": [], "unindexed_inbox": [],
                      "served_without_slug": [], "unregistered": [], "stale_registry_states": []},
        "degraded": True,
    }


def payload(scope: str) -> dict:
    """Cached iteration for a scope. The cache is what keeps a 30s panel cheap."""
    key = f"list:{scope}"
    hit = _cache.get(key)
    now = time.time()
    if hit and now - hit[0] < cache_ttl():
        return hit[1]
    module = ctl()
    if module is None:
        value = _degraded_payload(scope)
    else:
        try:
            value = module.iteration(scope=scope)
            value["degraded"] = False
        except Exception as exc:
            value = _degraded_payload(scope)
            value["integrity"]["warnings"].insert(0, f"iteration() failed: {type(exc).__name__}: {exc}")
    _cache[key] = (now, value)
    return value


# --------------------------------------------------------------------- bridge


def _bridge_base(request: Request) -> str:
    override = str(settings().get("base_url") or "").strip()
    if override:
        return override.rstrip("/") + "/"
    host = request.url.hostname or "127.0.0.1"
    return f"http://{host}:{bridge_port()}/"


def _bridge_up() -> bool:
    """Is the static bridge answering? Probed on loopback, always.

    The URL we hand out uses the request's host (so the app's machine can reach
    it); the probe must use a host the backend itself can resolve.
    """
    probe = f"http://127.0.0.1:{bridge_port()}/"
    key = "probe:loopback"
    hit = _cache.get(key)
    now = time.time()
    if hit and now - hit[0] < PROBE_TTL:
        return bool(hit[1])
    ok = False
    try:
        with urllib.request.urlopen(probe, timeout=1.5) as resp:
            ok = 200 <= resp.status < 400
    except (urllib.error.URLError, OSError, ValueError):
        ok = False
    _cache[key] = (now, ok)
    return ok


def _link_rows(rows: list, base: str) -> list:
    """Attach the served URL. The bridge serves GET without a token (the token
    gates the /save inbox only), so a plain link is the whole access story."""
    out = []
    for row in rows:
        item = dict(row)
        file_name = item.get("file")
        item["url"] = f"{base}{file_name}" if file_name and item.get("served") else ""
        out.append(item)
    return out


# --------------------------------------------------------------------- routes


@router.get("/health")
def health(request: Request) -> dict:
    module = ctl()
    reg = payload("all")
    base = _bridge_base(request)
    return {
        "ok": True,
        "version": VERSION,
        "degraded": reg.get("degraded", False),
        "control_plane": {
            "path": str(_ctl_path()),
            "source": _ctl_source or "none",
            "loaded": module is not None,
            "error": _ctl_error,
        },
        "registry": reg["registry"],
        "bridge": {"base": base, "up": _bridge_up()},
        "settings": {"values": settings(), "layers": dict(_settings_layers)},
        "counts": reg["counts"],
        "generated_at": reg["generated_at"],
    }


# ---------------------------------------------------------------------- doctor


def doctor_report(base: str | None = None) -> dict:
    """Is this install actually wired up? A checklist, not a guess.

    Every entry is `ok | warn | fail` plus a one-line detail and, where useful,
    the fix. Nothing here writes: the doctor is safe to run at any time, and the
    same function backs both `GET /doctor` and `scripts/doctor.py`.
    """
    checks: list[dict] = []

    def add(name: str, status: str, detail: str, fix: str = "") -> None:
        item = {"name": name, "status": status, "detail": detail}
        if fix:
            item["fix"] = fix
        checks.append(item)

    cfg = settings()
    module = ctl()
    path = _ctl_path()

    # 1. control plane
    if module is None:
        add("control plane", "fail", _ctl_error or "not loaded",
            "put the control plane in $HERMES_HOME/scripts/, point `control_plane` at it, "
            "or ship the bundled copy")
    else:
        src_note = {"configured": "configured path", "hermes-home": "$HERMES_HOME/scripts",
                    "bundled": "bundled copy (no local control plane found)"}.get(_ctl_source, _ctl_source)
        add("control plane", "ok" if _ctl_source != "bundled" else "warn",
            f"{path} [{src_note}]", "" if _ctl_source != "bundled" else
            "the bundled copy is a starting point, not a source of truth — keep the real one in "
            "$HERMES_HOME/scripts/ when you run the CLI too")

    # 2. drift between a local copy and the bundled one
    local = _home() / "scripts" / "cahier_ctl.py"
    bundled = _bundled_ctl()
    if local.is_file() and bundled.is_file():
        same = _ctl_digest(local) == _ctl_digest(bundled)
        add("bundled copy", "ok" if same else "warn",
            "identical to the local control plane" if same else
            f"differs from {local} ({_ctl_digest(bundled)} vs {_ctl_digest(local)})",
            "" if same else "expected after a local edit; the local copy is the one in use")

    # 3. registry
    reg_path = _registry_path()
    if reg_path.is_file():
        try:
            book = json.loads(reg_path.read_text(encoding="utf-8")).get("cahiers") or {}
            add("registry", "ok", f"{len(book)} cahier(s) at {reg_path}")
        except Exception as exc:
            add("registry", "fail", f"unreadable at {reg_path}: {exc}")
    else:
        add("registry", "warn", f"no registry yet at {reg_path}",
            "normal on a fresh install — it appears the first time a cahier is published")

    # 4. filings file
    groups = _groups_path()
    add("filings", "ok" if groups.is_file() else "warn",
        f"{groups}" + ("" if groups.is_file() else " (not written yet)"))

    # 5. bridge
    up = _bridge_up()
    port = bridge_port()
    add("bridge", "ok" if up else "warn",
        f"{'answering' if up else 'not answering'} on 127.0.0.1:{port}",
        "" if up else "start the bridge service if you want served links and /save to work")

    # 6. the URL answerers actually open
    configured_url = str(cfg.get("base_url") or "").strip()
    url = (base or "").strip() or (configured_url.rstrip("/") + "/" if configured_url else "")
    host = url.split("//")[-1].split("/")[0].split(":")[0] if url else ""
    loopback = host in ("127.0.0.1", "localhost", "::1")
    mode = str(cfg.get("hosting_mode") or "lan")
    if not url:
        add("base url", "warn", "not set",
            "set it to the address the answerers will use, e.g. http://192.168.1.20:8766/")
    elif loopback and mode != "local":
        add("base url", "fail", f"{url} is loopback-only while hosting mode is '{mode}'",
            "answerers opening that link from their own device will get nothing — "
            "use the machine's LAN address")
    else:
        add("base url", "ok", f"{url} (hosting mode: {mode})")

    # 7. the onboarding questions
    onboarding = {
        "notify_channel": (str(cfg.get("notify_channel") or "none") != "none",
                           "where answers should be announced"),
        "hosting_mode": (str(cfg.get("hosting_mode") or "lan") != "lan",
                         "where answerers open the page from"),
        "users": (bool(cfg.get("users")), "who answers"),
    }
    answered = [key for key, (done, _) in onboarding.items() if done]
    detail = (f"{len(answered)}/3 answered" if answered else "not answered — running on defaults")
    add("onboarding", "ok" if len(answered) == 3 else "warn", detail,
        "" if len(answered) == 3 else
        "ask in a session chat before the first cahier: " +
        ", ".join(what for _, (done, what) in onboarding.items() if not done))

    fails = sum(1 for c in checks if c["status"] == "fail")
    warns = sum(1 for c in checks if c["status"] == "warn")
    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "version": VERSION,
        "ok": fails == 0,
        "fails": fails,
        "warns": warns,
        "checks": checks,
        "settings": {"values": cfg, "layers": dict(_settings_layers)},
    }


@router.get("/doctor")
def doctor(request: Request) -> dict:
    return doctor_report(_bridge_base(request))


@router.get("/list")
def list_cahiers(
    request: Request,
    scope: str = Query("active",
                       description="active (= on the bridge) | paused | finished | all"),
) -> dict:
    data = payload(scope)
    base = _bridge_base(request)
    data = dict(data)
    data["rows"] = _link_rows(data["rows"], base)
    data["bridge"] = {"base": base, "up": _bridge_up()}
    return data


@router.get("/cahier")
def one_cahier(request: Request, slug: str = Query(..., min_length=1)) -> dict:
    data = payload("all")
    base = _bridge_base(request)
    for row in data["rows"]:
        if row["slug"] == slug:
            return {"generated_at": data["generated_at"], **_link_rows([row], base)[0]}
    raise HTTPException(status_code=404, detail=f"no cahier with slug {slug!r}")


@router.post("/action")
def spin_cahier(request: Request, body: dict = Body(...)) -> dict:
    """Spin ONE cahier up, or take it off the bridge — the panel's last write.

    Three verbs, one file move each way: ``up`` publishes the page again from its
    project folder in its original settings (same file name, same gate — PINs
    travel in the page — and the same deadline when it has not already passed);
    ``pause`` takes it off the bridge but keeps the row active and startable;
    ``close`` takes it off the bridge AND marks it done (``down`` is kept as an
    alias of close). A human's click is the only caller, exactly like the ✎ filing
    write, and the move itself is done by ``cahier_ctl.spin`` so the CLI, the
    fleet page and the panel stay one actor.

    Refusals stay refusals: an unknown slug, an unknown verb, a page that must
    never be spun down (``cahier_ctl.NEVER``), a row the control plane calls
    uncontrollable, or a move the row's own state does not allow — all 4xx,
    nothing moved.
    """
    module = ctl()
    if module is None:
        raise HTTPException(status_code=503,
                            detail="cahier_ctl.py is unavailable — refusing to move pages blind")
    payload_body = body or {}
    slug = str(payload_body.get("slug") or "").strip()
    action = str(payload_body.get("action") or "").strip().lower()
    if not slug:
        raise HTTPException(status_code=400, detail="slug is required")
    if action not in ("up", "pause", "close", "down"):
        raise HTTPException(status_code=400,
                            detail="action must be 'up', 'pause' or 'close'")
    rows = {r["slug"]: r for r in payload("all")["rows"]}
    row = rows.get(slug)
    if row is None:
        raise HTTPException(status_code=404, detail=f"no cahier with slug {slug!r}")
    if slug in getattr(module, "NEVER", set()) or not row.get("controllable"):
        raise HTTPException(status_code=400,
                            detail=f"'{slug}' is not controllable from the panel")
    # The control plane's own guard, said early: a 4xx with a reason beats a move
    # nobody wanted.
    if action == "up" and row.get("served"):
        raise HTTPException(status_code=400, detail=f"'{slug}' is already being served")
    if action == "pause" and not row.get("served"):
        raise HTTPException(status_code=400, detail=f"'{slug}' is not being served")
    if action in ("close", "down") and not row.get("served") \
            and (row.get("lifecycle") or "") != "paused":
        raise HTTPException(status_code=400,
                            detail=f"'{slug}' is not on the bridge — nothing to close")
    result = module.spin(slug, action, apply=True, actor="panel")
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error") or "the spin failed")
    try:
        module.sync_registry(module.build_rows(), note=f"panel {action} {slug}")
    except Exception as exc:  # noqa: BLE001 — the page already moved; say so, do not undo
        result["registry_warning"] = f"{type(exc).__name__}: {exc}"
    _cache.clear()  # the very next GET must show the new state, not a 5s-old one
    return {**result, "row": one_cahier(request, slug=slug), "written_by": "panel"}


@router.post("/filing")
def set_filing(request: Request, body: dict = Body(...)) -> dict:
    """The ONE write this backend performs, and only because a human asked.

    The panel's ✎ is the only caller. The write goes through
    ``cahier_ctl.set_filing`` — validated slug, atomic rename, logged to the
    control log — so a panel edit and a hand-edit of the same file are the same
    operation, and an unknown slug is refused instead of inventing an entry.
    Sending an empty string for a field FORGETS that override.
    """
    module = ctl()
    if module is None:
        raise HTTPException(status_code=503,
                           detail="cahier_ctl.py is unavailable — refusing to write a filing blind")
    slug = str((body or {}).get("slug") or "").strip()
    if not slug:
        raise HTTPException(status_code=400, detail="slug is required")
    listed = {r["slug"] for r in payload("all")["rows"]}
    if slug not in listed:
        raise HTTPException(status_code=404, detail=f"no cahier with slug {slug!r}")
    payload_body = body or {}
    for field in ("profile", "project"):
        value = payload_body.get(field)
        if value is not None and len(str(value)) > 64:
            raise HTTPException(status_code=400, detail=f"{field} is longer than 64 characters")
    result = module.set_filing(
        slug,
        profile=payload_body.get("profile"),
        project=payload_body.get("project"),
        clear=bool(payload_body.get("clear")),
        actor="panel",
        path=_groups_path(),
    )
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error") or "filing rejected")
    _cache.clear()  # the very next GET must show the new grouping, not a 5s-old one
    return {**result, "row": one_cahier(request, slug=slug), "written_by": "panel"}

