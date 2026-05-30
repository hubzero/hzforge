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

Dual-target Py2.7 + Py3.6.  The upstream macro sources were already
syntactically Py3-clean (`%`-format only, no `<>`/`iteritems`/`print`
statement, no `ConfigParser`/`MySQLdb`), so the port is much smaller than
`plugins/mysqlauthz/`'s — just `from __future__ import …` future imports
for symmetry and dropping the dead `from trac.web.href import Href` (the
`Href` was imported but never referenced).

## Security

**`0.1.1`** (2026-05-30) fixes a **stored XSS** carried over from the
upstream macros (and present in `0.1.0`): both macros interpolated
wiki-author-controlled input straight into HTML via raw `%`-formatting and
returned a plain string, which Trac inserts as markup **without escaping**.
Anyone with `WIKI_MODIFY` could plant a payload that runs in the browser of
anyone (including admins) viewing the page — e.g.
`[[link(/x <img src=x onerror=alert(document.cookie)>)]]`, or a `"`-breakout
in the path/image-name. `0.1.1` HTML-escapes every interpolated value
before returning and guards empty/`None` args (previously an
`IndexError`/`AttributeError` traceback on `[[image]]`). The version label
also drops the `+hzforge.1` local segment to match the sibling plugins'
plain semver.

Iteration log:

| Iter | Concern |
|---|---|
| `hzforge.0` | Verbatim copy of upstream `hubzero-forge/source/{image.py.in,link.py.in}`.  The `.in` suffix is vestigial — these were templates back when they used `@PROJECT@`-style substitution, but the current upstream resolves URLs via `self.env.abs_href()` and needs no template processing. |
| `hzforge.1` | Dual-target Py2.7 + Py3.6 — `from __future__ import …` future imports, drop the unused `Href` import.  Add 7 pytest cases covering both macros' basic / already-slashed / different-env / multi-word paths.  Trac is stubbed; no real Trac install required. |
| `0.1.1` | Stored-XSS fix — HTML-escape all interpolated values; guard empty/`None` args.  Add 7 XSS/edge-case tests (14 total). |

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
make            # = make test = make test-py3  -> 7 tests, ~0.1s on Py3.6
make test-py2   # same suite on Py2.7
make test-all   # both
```

The `Makefile` just shells out to `python3 -m pytest` / `python2 -m pytest`
(no venv, no tox -- see [mysqlauthz/README.md](../mysqlauthz/README.md#running-the-tests)
for the why-no-tox explanation).  One-time setup per host:

```sh
python3 -m pip install --user 'pytest>=7,<7.1'
python2 -m pip install --user 'pytest<5'
```

Tests use `pythonpath = ["src"]` and `testpaths = ["tests"]` in
`pyproject.toml` to find the plugin source and discover the test files.

## Provenance

- **Upstream:** `git@gitlab.hubzero.org:hubzero-packaging-repositories/hubzero-forge.git`
  — files `source/image.py.in` and `source/link.py.in`.
- **License:** MIT (matches the upstream copyright headers).
