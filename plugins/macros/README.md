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

Dual-target Py2.7 + Py3.11.  The upstream macro sources were already
syntactically Py3-clean (`%`-format only, no `<>`/`iteritems`/`print`
statement, no `ConfigParser`/`MySQLdb`), so the port is much smaller than
`plugins/mysqlauthz/`'s — just `from __future__ import …` future imports
for symmetry and dropping the dead `from trac.web.href import Href` (the
`Href` was imported but never referenced).

Iteration log:

| Iter | Concern |
|---|---|
| `hzforge.0` | Verbatim copy of upstream `hubzero-forge/source/{image.py.in,link.py.in}`.  The `.in` suffix is vestigial — these were templates back when they used `@PROJECT@`-style substitution, but the current upstream resolves URLs via `self.env.abs_href()` and needs no template processing. |
| `hzforge.1` | Dual-target Py2.7 + Py3.11 — `from __future__ import …` future imports, drop the unused `Href` import.  Add 7 pytest cases covering both macros' basic / already-slashed / different-env / multi-word paths.  Trac is stubbed; no real Trac install required. |

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

## Running the tests

```sh
cd plugins/macros
python3.11 -m pytest          # 7 tests, ~0.1s
```

Tests use `pythonpath = ["src"]` and `testpaths = ["tests"]` in
`pyproject.toml` to find the plugin source and discover the test files.

## Provenance

- **Upstream:** `git@gitlab.hubzero.org:hubzero-packaging-repositories/hubzero-forge.git`
  — files `source/image.py.in` and `source/link.py.in`.
- **License:** MIT (matches the upstream copyright headers).
