# Cahier Hub

Every cahier in one place, reachable from the desktop sidebar.

A cahier is a question the agent asks a human — a served HTML page (or a chat
question) whose answers land in an inbox and a SQLite table. `Cahier Hub` is the
index: one sidebar row → `/cahiers` → every live and pending cahier with its
saves, deadline, PIN state and served link. Open one, copy its link, or filter
by scope.

**Read-only by construction.** The page renders whatever
`cahier_ctl.iteration()` returns — the same call the CLI's `list` uses — so the
page and the terminal can never disagree about what a cahier *is*. Nothing here
writes: not to `~/cahier-share/`, not to the registry, not to the answers DB.
Zero API keys, zero model tokens.

## What's in here

| Path | Role |
|---|---|
| `plugin.yaml` | Plugin manifest (`kind: standalone`) |
| `__init__.py` | Agent-side stub — registers no tools/hooks by design |
| `dashboard/manifest.json` | Dashboard half: label, icon, API file, hidden tab |
| `dashboard/plugin_api.py` | Read-only JSON backend, mounted at `/api/plugins/cahier-hub/` |
| `desktop/plugin.js` | Sidebar row + `/cahiers` page (runs inside the Electron app) |
| `tests/` | Backend contract suite + panel render harness |

## Endpoints

| Route | Returns |
|---|---|
| `GET /health` | Control-plane load status, bridge port, counts |
| `GET /list?scope=active\|all\|finished` | Rows + counts + integrity warnings (what the page fetches) |
| `GET /cahier?slug=<slug>` | One cahier, plus its latest saves |

Every route sits behind the dashboard's own auth (the same gate
`mnemosyne-panel` lives behind), so a bare `curl` from the host gets `401`; the
app's `ctx.rest` carries the session token.

`active` = live + pending. `finished` = retired. Unknown scopes fall back to
`all` rather than erroring — a stale bookmark should never show a broken page.

## Install

Backend half (already done on this machine):

```bash
hermes plugins enable cahier-hub        # adds it to plugins.enabled
hermes plugins doctor cahier-hub        # runtime discovery + import contract
# restart hermes-serve so the route mounts
```

Desktop half — the app runs its own `plugin.js` from its own home, so the file
has to land there:

```
%USERPROFILE%\.hermes\desktop-plugins\cahier-hub\plugin.js
```

then **Rescan** in the app's plugin settings. (On a local app this is what
`materializeDesktopHalf` copies for you; across the remote backend it is a
one-file drop, same as `mnemosyne-panel`.)

## Verify

```bash
./tests/run.sh
```

Runs both halves against real data, no fixtures-by-hand:

1. **Backend contract suite** (pytest, real FastAPI app + real router) — health,
   scope equality with `cahier_ctl.iteration()`, no dropped/double slugs, stable
   order across calls, a read-only proof that the hashes of
   `cahier-registry.json` and `cahier-answers.db` are unchanged by a request, and
   **cross-implementation parity**: the panel's slug set must equal
   `GET /fleet/cahiers` on the bridge exactly — a slug visible in one and not the
   other is a cahier the user either can't see or can't open. That half skips
   cleanly when `:8766` isn't listening (override the URL with
   `CAHIER_HUB_BRIDGE=`), so a stopped optional service never turns the suite red.
2. **Panel harness** (Node, SDK stubbed, real backend payload) — `register()`
   wires one sidebar row and one route, the page renders every cahier the
   backend sent exactly once, "Open" hands the served URL to the OS, "Copy link"
   writes the same URL, and two renders are byte-identical.

Both suites run against `scope=active` (what the page opens with) and
`scope=all` (empty-scope edge), so an empty or degenerate payload fails loudly.

## Failure behaviour

`cahier_ctl.py` missing or broken → the API degrades to the registry file alone
and says so in `integrity.warnings`; the page shows a warning strip instead of an
empty list. A half-read is never silently presented as a complete one.

Known warnings on this machine today (surfaced in the page, not hidden):

- `wave-leftover-2026-09-16-answers-2026-09-16-003139.json` — 104 answers in the
  inbox, never ingested into `cahier-answers.db`.
- Three served pages with no `SLUG` declaration, so no registry row:
  `ZF_photoset_audit_2026-09-02.html`, `norway-trip-cahier.html`,
  `norway-trip-cahier-offline.html`.
