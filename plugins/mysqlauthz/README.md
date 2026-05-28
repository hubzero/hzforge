# hubzero-trac-mysqlauthz

A Trac plugin implementing `IPermissionStore` and `IPermissionGroupProvider`
against the HUBzero CMS MySQL database (`jos_users`, `jos_xgroups`,
`jos_trac_user_permission`, `jos_trac_group_permission`,
`jos_trac_project`, `jos_tool_groups`, `jos_tool`).

Per-tool Trac envs reach for HUBzero users/groups/permissions through this
plugin instead of Trac's built-in `DefaultPermissionStore`. The plugin is
discovered system-wide via its `[trac.plugins]` entry point, so a single
install in the Python site-packages serves every Trac env on the host.

## Status

This directory is the source of **`hubzero-trac-mysqlauthz` 2.4.0** — the
hzforge release that supersedes upstream 2.2.5 with a dual-target Py2.7 +
Py3.x wheel and a set of audit-driven fixes.  The version jump (2.2.5 → 2.4.0)
signals real behavior change (parameterized SQL, connection-management
rewrite, the long-broken `get_permission_groups` query), not a downstream
patch level.

| Concern | Status |
|---|---|
| Python 2/3 compatibility | **Done** (hzforge.1) — one source, env-marker deps (PyMySQL, configparser backport on Py2), `from __future__ import absolute_import, division, print_function, unicode_literals` |
| `import MySQLdb` (Py2-only driver) | **Done** (hzforge.1) — `import pymysql as MySQLdb` (PyMySQL is an API-compatible replacement; legacy `passwd=`/`db=` kwargs still work) |
| `import ConfigParser` (Py2 module name) | **Done** (hzforge.1) — `from configparser import RawConfigParser` |
| `<>` not-equal operator (Py2-only) | **Done** (hzforge.1) — 8 sites converted (`is not None` for `None`, `!=` for string compares) |
| SQL string concatenation (injection class) | **Done** (hzforge.2) — every `cursor.execute()` parameterized; PyMySQL handles quoting/type conversion driver-side |
| Class-level `db`/`dbcursor` (thread-unsafe singleton) | **Done** (hzforge.3) — replaced by per-method `with _cms_cursor(self.env)` context manager; each call opens its own connection |
| `disconnect()` never closes the connection | **Done** (hzforge.3) — `with` block actually closes the connection on exit |
| `__init__` log of `int + str` (`self.project_id` typo) | **Done** (hzforge.3) — `str(db.insert_id())` plus lazy `%s` log format |
| `get_permission_groups` query references undefined `proj` alias | **Done** (hzforge.4) — filter on `self.project_id` directly instead of adding the `proj` join (matches every other query); also dropped the unused `p.action` column and added `DISTINCT` so multi-permission group memberships don't produce duplicate `@group` entries |
| Unreachable `elif` branches behind `if True:` | **Done** (hzforge.3) — dead `INSERT/DELETE FROM jos_xgroups_members/_managers` branches dropped from grant/revoke |

Iteration log (the dev journey; each commit landed one row of the audit
table.  All five iterations ship together as 2.4.0):

| Iter | Concern |
|---|---|
| `hzforge.0` | Verbatim copy of upstream `hubzero-trac-mysqlauthz-2.2.5-1.el8` |
| `hzforge.1` | Py3 compatibility — `import` names, `<>` operator, future imports |
| `hzforge.2` | Parameterize every `cursor.execute()` — closes the SQL-injection class |
| `hzforge.3` | Connection management rewrite — drop the `HubzeroDatabaseConnection` singleton in favor of `with _cms_cursor()`; eliminates the thread-unsafe class-level state and the `disconnect()` connection leak.  Incidentally also fixes the `int + str` log concat in `__init__`, the double-construct in `PermissionGroupProvider.__init__`, and removes the unreachable `if True:`/elif dead code in `grant_permission`/`revoke_permission`. |
| `hzforge.4` | Fix `get_permission_groups` — the query referenced an undefined `proj` alias in `WHERE`/`FROM` (broken since at least 2011).  Resolved by filtering on `self.project_id` directly (matches every other query in the plugin), with `DISTINCT` added to avoid duplicate `@group` entries and the unused `p.action` column dropped. |
| `hzforge.5` | Add `tests/` — 17 pytest cases covering the context manager (close on normal+exception exit), `__init__` paths, `get_user_permissions`, `get_users_with_permissions` (parametrized over the IN-clause size), `grant`/`revoke`, `get_permission_groups`, plus regression tests locking in the parameterization (hzforge.2) and the `proj`-alias fix (hzforge.4).  Trac + the CMS DB are stubbed; no real Trac install or MySQL required. |

**All audit-table items are resolved.**  2.4.0 is the production release;
it installs cleanly on both Py2.7 (today's Trac 1.0.14 stack) and Py3.6+
(in preparation for the Trac 1.6 / Py3 migration).

## Running the tests

```sh
cd plugins/mysqlauthz
make            # = make test = make test-py3  -> 17 tests, ~0.2s on Py3.6
make test-py2   # same suite on Py2.7 (the production Trac interpreter today)
make test-all   # both
```

The `Makefile` just shells out to `python3 -m pytest` / `python2 -m pytest`
(no venv, no tox).  One-time setup per host:

```sh
sudo dnf install python3 python2                                  # Rocky 8 base
python3 -m pip install --user 'pytest>=7,<7.1' 'pymysql>=1.0,<1.1'
python2 -m pip install --user 'pytest<5' 'pymysql<1.0' 'configparser'
```

Tests use the `pythonpath = ["src"]` and `testpaths = ["tests"]` settings
in `pyproject.toml` to find the plugin source and discover the test files.

> **Why not tox?**  tox 4 + virtualenv 21+ uses interpreter-discovery code
> written in Py3.8+ syntax (the `:=` walrus operator on
> `python_discovery._py_info` line 183), so it can't drive Py3.6 (Rocky 8's
> stock `/usr/bin/python3`) or Py2.7 (the production Trac interpreter).
> The Makefile sidesteps that by invoking each system Python's own `-m pytest`.

## Provenance

- **Upstream:** `git@gitlab.hubzero.org:hubzero-packaging-repositories/hubzero-trac-mysqlauthz.git`
- **Baseline:** the deployed RPM `hubzero-trac-mysqlauthz-2.2.5-1.el8.noarch`
  (master HEAD of the GitLab repo is currently `2.2.3-1`; the deployed version
  is slightly ahead — a diff between the two is a known follow-up task).

## Install (today, verbatim Py2 source)

```sh
# Py2 host with MySQL-python already installed via the OS package
pip2 install /path/to/hzforge/plugins/mysqlauthz
```

The wheel will install to `<py2-site-packages>/hubzeroplugin/` and Trac picks
it up automatically via the `[trac.plugins]` entry point — no per-env action
required.

## Install (post-port, both interpreters)

```sh
pip2 install /path/to/hzforge/plugins/mysqlauthz   # Py2 hubs (Trac 1.0.x)
pip3 install /path/to/hzforge/plugins/mysqlauthz   # Py3 hubs (Trac 1.6+)
```

Same source, two wheels.

## Notes

- The plugin opens its **own** MySQL connection to the HUBzero CMS DB — it
  does NOT use Trac's `env.db_query`/`env.db_transaction`, which target the
  per-env Trac DB (SQLite on HUBzero hubs). Two databases, two different
  concerns; only the plugin touches MySQL.
- The plugin name (`hubzero-trac-mysqlauthz`) and the Trac entry point
  (`hubzeroplugin.api`) are preserved across the port so a Py3 wheel drops in
  over the existing 2.x RPM without changing `trac.ini`.
