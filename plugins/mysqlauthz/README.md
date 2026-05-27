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

This directory is **the in-progress hzforge fork** of the upstream plugin. The
target is a dual-target Py2.7 + Py3.11 wheel that also fixes a set of latent
bugs surfaced during a code audit.

| Concern | Plan |
|---|---|
| Python 2/3 compatibility | One source, env-marker deps (PyMySQL, configparser backport on Py2) |
| `import MySQLdb` (Py2-only driver) | Swap to `import pymysql` (works on both Py2 and Py3) |
| `import ConfigParser` (Py2 module name) | `from configparser import RawConfigParser` (Py3 stdlib or PyPI backport on Py2) |
| `<>` not-equal operator (Py2-only) | `!=` (8 sites) |
| SQL string concatenation (injection class) | Parameterized queries throughout (~17 sites) |
| Class-level `db`/`dbcursor` (thread-unsafe singleton) | Per-instance, `with closing(self.db.cursor()) as cur:` |
| `disconnect()` never closes the connection | Actually `.close()` it |
| `__init__` log of `int + str` (`self.project_id` typo) | Coerce with `str()` |
| `get_permission_groups` query references undefined `proj` alias | Add `jos_trac_project AS proj` to FROM |
| Unreachable `elif` branches behind `if True:` | Delete or gate explicitly |

The current commit is a **verbatim copy** of the deployed
`hubzero-trac-mysqlauthz-2.2.5-1.el8` RPM contents — byte-identical to
`/usr/lib/python2.7/site-packages/hubzeroplugin/{__init__,api}.py`. Subsequent
commits land each of the items above so the diff cleanly shows the change.

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
