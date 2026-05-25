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
sudo python3 hzforge.py uninstall git                    # stop serving git (packages/data kept)
sudo python3 hzforge.py doctor                           # diagnose all configured
sudo python3 hzforge.py doctor git                       # diagnose one service
sudo python3 hzforge.py repair                           # fix drift
sudo python3 hzforge.py repair trac
sudo python3 hzforge.py test                             # create a throwaway Trac project, verify, remove
```

Preview any command without changing anything:

```
sudo python3 hzforge.py install --dry-run
```

## Commands

### install `[services]`
Install packages, create `/opt/<svc>/tools` dirs (conventional perms), create the
`hzsvn`/`hzgit` groups, and write the per-service drop-in(s). No services = all
four. Consolidates a legacy standalone `trac.conf` into the trac drop-in. On a host
without systemd (a container or chroot) it also creates the `/run/httpd` runtime dir
that `httpd -k start` needs, since `systemd-tmpfiles` isn't there to make it. After
installing trac it runs the **test** self-check automatically (skip with
`--no-test`).

### uninstall `<services>`
Remove a service's drop-in (and, for trac, unload its interpreter module) plus the
helper files hzforge created for it — for trac the WSGI shim and egg cache, for svn
the wandisco repo file. **Never** removes packages, the `hzsvn`/`hzgit` groups, or
repository data under `/opt/<svc>/tools` (only the config/serving is torn down).

### doctor `[services]`
Read-only diagnosis; exits non-zero if anything is **FAIL**. Service-specific checks
are scoped to the requested services; global checks always run: `apachectl
configtest`, running-vs-on-disk interpreter state, a stray legacy `trac.conf`, the
service-control mechanism (systemd vs `httpd -k`), presence of the `/run/httpd`
runtime dir on non-systemd hosts, and whether httpd is actually **active**.

### repair `[services]`
Diagnose, then re-assert the requested (configured) services — fixing missing
shim/dirs, file permissions, and module state — then validate and reload/restart.
`repair git` is isolated to git; it won't touch trac.

### test
End-to-end self-check for the Trac (mod_wsgi) handler: creates a throwaway,
uniquely-named Trac environment under `/opt/trac/tools/`, fetches
`/tools/<name>/wiki` over the hub's own vhost, asserts a `200` Trac response, then
removes the environment. It needs no MySQL/forge provisioning — the WSGI shim
serves any env generically — and exits non-zero on failure (handy for CI). Runs
automatically at the end of `install trac`.

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
- `uninstall` never removes packages, the `hzsvn`/`hzgit` groups, or repository
  data under `/opt/<svc>/tools`.
