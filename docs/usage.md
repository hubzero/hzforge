# Usage

Run as **root** (`sudo`). Services are **positional** (space- or comma-separated).
A bare `hzforge` prints help.

See [Requirements](requirements.md) for host, Python, package, and network
prerequisites before installing, and [Manual installation](manual-install.md) for the
step-by-step by-hand procedure (what these commands automate) for each handler/service set.

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
sudo python3 hzforge.py test                             # throwaway project per configured service, verify, remove
sudo python3 hzforge.py test svn git                     # only the named services
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
repository data under `/opt/<svc>/tools` (only the config/serving is torn down). A
requested service that isn't currently configured is reported and skipped; if none
of the requested services are configured, the running server is left untouched.

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

### test `[services]`
End-to-end self-check per service. For each requested (configured) service it creates
a throwaway, uniquely-named project, fetches it over the hub's own vhost, asserts a
`200`, then removes it. No services = all configured.

| Service | Resource created | URL checked | Pass signal |
|---|---|---|---|
| `trac` | Trac env under `/opt/trac/tools/` | `/tools/<name>/wiki` | Trac wiki page |
| `svn` | repo via `svnadmin create` under `/opt/svn/tools/` | `/tools/<name>/svn/` | mod_dav_svn listing |
| `git` / `gitExternal` | bare repo under `/opt/<svc>/tools/<name>.git` | `…/git/<name>/info/refs?service=git-upload-pack` | git-http-backend advertisement |

It needs no MySQL/forge provisioning. mod_wsgi `trac` is served by hzforge's generic WSGI
route (no config change); `svn`, `git`, and **mod_python** `trac` need a per-resource
route, so a temporary self-test drop-in (`00-forge-selftest.conf`) is added and removed
around the checks (graceful reload). Both Trac handlers are self-testable. Exits non-zero
on failure (handy for CI). The just-installed services are tested automatically at the end
of `install` (skip with `--no-test`).

## Options (install)

| Option | Default | Notes |
|---|---|---|
| `--trac-handler {mod_wsgi,mod_python}` | `mod_wsgi` | Exactly one interpreter is loaded. |
| `--svn-source {wandisco,appstream}` | `wandisco` | `subversion`+`mod_dav_svn` source; `subversion-python` always from hubzero. |
| `--trac-spec` | `Trac==1.0.14` | pinned pip spec; 1.0.x is the DB schema 26 line (matches envs, no upgrade). |
| `--modwsgi-spec` | `mod_wsgi==4.9.4` | last Python-2-capable mod_wsgi. |
| `--ldap-url` / `--ldap-binddn` / `--ldap-bindpw` | auto-detect | for the Trac `/login` auth block; read from the existing `svn.conf` if not given. |
| `--ldap-bindpw-file` | — | read the bind password from a root-only file instead of `--ldap-bindpw` (which is visible in the process list). |
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
