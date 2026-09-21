"""Cahier Hub — agent-side stub.

The real surface is split in two halves that live in this same directory:

* ``dashboard/plugin_api.py`` — read-only JSON backend, mounted by hermes-serve
  at ``/api/plugins/cahier-hub/``. It is a thin wrapper over
  ``cahier_ctl.iteration()``, the same call the CLI's ``list`` uses.
* ``desktop/plugin.js`` — the sidebar row and the ``/cahiers`` page, copied by
  the desktop app into ``$HERMES_HOME/desktop-plugins/cahier-hub/plugin.js``.

Nothing runs in the agent process: this plugin registers no tools, no hooks and
no commands. The module exists so the plugin is a well-formed directory plugin
that ``hermes plugins enable/disable``, ``hermes plugins doctor`` and the
``plugins.enabled`` trust gate can address by name.
"""

from __future__ import annotations


def register(ctx) -> None:  # noqa: ARG001 - contract signature requires the context
    """No agent-side contributions by design (desktop + dashboard plugin)."""
    return None
