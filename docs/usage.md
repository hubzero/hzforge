# Usage

Run as **root** (`sudo`). Services are **positional** (space- or comma-separated).
A bare `hzforge` prints help.

See [Requirements](requirements.md) for host, Python, package, and network
prerequisites before installing.

```
sudo python3 hzforge.py install                          # all services
sudo python3 hzforge.py install trac                     # one service
sudo python3 hzforge.py install svn git gitExternal trac
sudo python3 hzforge.py install trac --trac-handler mod_python
sudo python3 hzforge.py uninstall git                    # stop serving git (data kept)
sudo python3 hzforge.py uninstall trac --purge           # also remove packages/artifacts
sudo python3 hzforge.py doctor                           # diagnose all configured
sudo python3 hzforge.py doctor git                       # diagnose one service
sudo python3 hzforge.py repair                           # fix drift
sudo python3 hzforge.py repair trac
```

Preview any command without changing anything:

```
sudo python3 hzforge.py install --dry-run
```

## Commands

### install `[services]`
Install packages, create `/opt/<svc>/tools` dirs (conventional perms), join
`hzsvn`/`hzgit` groups if present, and write the per-service drop-in(s). No
services = all four. Consolidates a legacy standalone `trac.conf` into the trac
drop-in.

### uninstall `<services> [--purge]`
Remove a service's drop-in (and, for trac, unload its interpreter module). **Never
deletes repository data** under `/opt/<svc>/tools`. `--purge` additionally removes
the installed packages, the wandisco repo file, and the WSGI shim — but still never
the repo data.

### doctor `[services]`
Read-only diagnosis; exits non-zero if anything is **FAIL**. Service-specific checks
are scoped to the requested services; global checks (`apachectl configtest`,
running-vs-on-disk interpreter state, a stray legacy `trac.conf`) always run.

### repair `[services]`
Diagnose, then re-assert the requested (configured) services — fixing missing
shim/dirs, file permissions, and module state — then validate and reload/restart.
`repair git` is isolated to git; it won't touch trac.

## Options (install)

| Option | Default | Notes |
|---|---|---|
| `--trac-handler {mod_wsgi,mod_python}` | `mod_wsgi` | Exactly one interpreter is loaded. |
| `--svn-source {wandisco,appstream}` | `wandisco` | `subversion`+`mod_dav_svn` source; `subversion-python` always from hubzero. |
| `--trac-spec` | `Trac>=1.0,<1.1` | pip spec matching env DB schema (no upgrade). |
| `--modwsgi-spec` | `mod_wsgi==4.9.4` | last Python-2-capable mod_wsgi. |
| `--ldap-url` / `--ldap-binddn` / `--ldap-bindpw` | auto-detect | for the Trac `/login` auth block; read from the existing `svn.conf` if not given. |
| `--force-pip` | off | reinstall Trac even if importable. |

Common to all commands: `--hub <name>` (auto-detected from `sites.d`),
`--dry-run`, `--no-restart`.

## Exit codes

- `doctor` exits **0** when there are no FAIL findings, **1** otherwise — handy in
  CI or monitoring.
- Other commands exit non-zero on a hard error (e.g. `configtest` failure, after
  which the running server is left untouched).

## Safety

- `apachectl configtest` runs before any reload/restart; on failure hzforge aborts
  without touching the running server.
- `--dry-run` previews every action; `--no-restart` stages changes without applying.
- `uninstall` preserves repository data; `--purge` still never removes
  `/opt/<svc>/tools`.
