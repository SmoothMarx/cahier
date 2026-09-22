# Cahier Hub

Every cahier in one place, reachable from the desktop sidebar.

A cahier is a question the agent asks a human — a served HTML page (or a chat
question) whose answers land in an inbox and a SQLite table. `Cahier Hub` is the
index: one sidebar row → `/cahiers` → every live and pending cahier with its
saves, deadline, PIN state and served link, grouped by **profile ▸ project**.
Read one **here** (framed in the workspace pane, browser one click away), copy
its link, filter by scope, or say which profile/project a cahier belongs to.

**Reads are read-only by construction.** The page renders whatever
`cahier_ctl.iteration()` returns — the same call the CLI's `list` uses — so the
page and the terminal can never disagree about what a cahier *is*. Grouping only
buckets that order; it never re-sorts or re-counts it. Zero API keys, zero model
tokens.

**One write exists, and a human makes it.** The ✎ on a row POSTs `/filing` to
record the profile and project behind a cahier; that goes through
`cahier_ctl.set_filing` (slug validated against the live list, atomic rename,
audit line in `cahier-control.log`). Nothing else on the page writes anywhere.

## What's in here

| Path | Role |
|---|---|
| `plugin.yaml` | Plugin manifest (`kind: standalone`) |
| `__init__.py` | Agent-side stub — registers no tools/hooks by design |
| `dashboard/manifest.json` | Dashboard half: label, icon, API file, hidden tab |
| `dashboard/plugin_api.py` | JSON backend (all reads + the one `POST /filing`), mounted at `/api/plugins/cahier-hub/` |
| `desktop/plugin.js` | Sidebar row + `/cahiers` page: grouping, in-window viewer, ✎ filing (runs inside the Electron app) |
| `tests/` | Backend contract suite + panel render harness |

## Endpoints

| Route | Returns |
|---|---|
| `GET /health` | Control-plane load status, bridge port, counts |
| `GET /list?scope=active\|all\|finished` | Rows + counts + integrity warnings + the folder vocabulary (what the page fetches) |
| `GET /cahier?slug=<slug>` | One cahier, plus its latest saves |
| `POST /filing` | Records `{slug, profile, project}` (empty string = forget). 404 on an unknown slug, 400 on bad input, 503 when the control plane is missing |

Every route sits behind the dashboard's own auth (the same gate
`mnemosyne-panel` lives behind), so a bare `curl` from the host gets `401`; the
app's `ctx.rest` carries the session token.

`active` = live + pending. `finished` = retired. Unknown scopes fall back to
`all` rather than erroring — a stale bookmark should never show a broken page.

## Where a cahier's profile/project comes from

Grouping needs two labels per cahier and refuses to invent them. Each answer
carries its provenance (`profile_source` / `project_source`), resolved strongest
first — the same ladder lives in `cahier_ctl.filing()`:

| Source | Meaning |
|---|---|
| `override` | a human said so: the panel's ✎ or `cahier_ctl.py filing --slug … --profile … --project …` |
| `declared` | stamped into the page by `cahier.py build` (`CAHIER_PROFILE` / `CAHIER_PROJECT`) |
| `session` | the session that built it → `profile_name` in `~/.hermes/state.db` |
| `derived` | the project's own name appears in the slug (`mmapp-restructure` → MMAPP). Slug only — titles are prose |
| `none` | nobody knows; the panel shows an empty chip |

Overrides live in one hand-editable file, `~/.hermes/state/cahier-groups.json`
(`CAHIER_HUB_GROUPS` re-points it, which is how the tests stay hermetic):

```json
{"version": 1, "updated_by": "panel", "cahiers": {"rolodex-merges": {"profile": "default", "project": "rolodex"}}}
```

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
   The filing tests write to a `tmp_path` override file: the write lands there and
   nowhere else, an unknown slug / traversal / oversized label is refused with
   nothing left behind, a GET never touches the file, and an override always beats
   every automatic answer.
2. **Panel harness** (Node, SDK stubbed, real backend payload) — `register()`
   wires one sidebar row and one route, the page renders every cahier the backend
   sent exactly once, grouping buckets those rows by profile ▸ project (and Flat
   drops the headers) without losing any, "Open" frames the served URL in-window,
   "Browser" hands the same URL to the OS, "Copy link" writes it to the clipboard,
   and the ✎ POSTs `/filing` — the only non-read call the page makes.

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
