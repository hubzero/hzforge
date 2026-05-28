# hubzero-trac-macros

Two Trac wiki macros used by HUBzero forge tools:

- `[[image <name>]]` — inserts an image from the env's `Images` wiki attachment
  area (resolved against `self.env.abs_href()` at request time).
- `[[link <path> <text>]]` — inserts an internal project link (e.g.,
  `[[link(/report Ticket System)]]` for the per-env ticket report).

Both macros are **env-agnostic** — they construct URLs from
`self.env.abs_href()` instead of having the env name baked in — so a single
install in the Python site-packages serves every Trac env on the host via
the `[trac.plugins]` entry-point discovery. No per-env file required.

## Status

This commit is a **verbatim copy** of the upstream `image.py.in` and
`link.py.in` from
`gitlab.hubzero.org:hubzero-packaging-repositories/hubzero-forge`
(`source/`). The `.in` suffix is vestigial — these were templates back when
they used `@PROJECT@`-style substitution, but the current upstream resolves
URLs via `self.env.abs_href()` and needs no template processing.

Subsequent commits will dual-target Py2.7 + Py3.11 in the same shape as
`plugins/mysqlauthz/`. The upstream macros are already syntactically
Py3-clean (no `<>`, no `iteritems`, no Py2 `print` statement, no `unicode`
literal pattern), so the port is minimal.

## Replaces

The legacy per-env macros `/opt/trac/tools/<env>/plugins/image.py` and
`<env>/plugins/link.py` that hubzero-forge's `installtool*` workflow
historically dropped into individual envs at tool-create time. Those copies
are stale — they hardcode the URL prefix
(`/tools/<env>/attachment/wiki/Images/...`) rather than deriving it from the
request — and become redundant once this plugin is installed system-wide.
hzforge's planned `_upgrade_trac_env()` cleanup will surface and remove
them.

## Install

```sh
pip2 install /path/to/hzforge/plugins/macros   # Py2 hubs (Trac 1.0.x)
pip3 install /path/to/hzforge/plugins/macros   # Py3 hubs (Trac 1.6+) -- post-port
```

After install, every Trac env on the host picks up both macros via the
`[trac.plugins]` entry points — no `trac.ini` changes required.

## Provenance

- **Upstream:** `git@gitlab.hubzero.org:hubzero-packaging-repositories/hubzero-forge.git`
  — files `source/image.py.in` and `source/link.py.in`.
- **License:** MIT (matches the upstream copyright headers).
