<p align="center">
  <img src="gh-pages/assets/logo.svg" alt="" width="120" height="120">
</p>

<h1 align="center">hzforge</h1>

<p align="center">
  <em>Install, diagnose &amp; repair HUBzero Forge services &mdash; Apache drop-ins, no m4.</em>
</p>

<p align="center">
  <a href="https://github.com/hubzero/hzforge/actions/workflows/tests.yml"><img alt="tests" src="https://github.com/hubzero/hzforge/actions/workflows/tests.yml/badge.svg"></a>
  <a href="https://github.com/hubzero/hzforge/actions/workflows/docs.yml"><img alt="docs CI" src="https://github.com/hubzero/hzforge/actions/workflows/docs.yml/badge.svg"></a>
  <a href="https://hubzero.github.io/hzforge/"><img alt="documentation" src="https://img.shields.io/badge/docs-hubzero.github.io%2Fhzforge-2456c2?logo=github&logoColor=white"></a>
  <img alt="Python 3.8+" src="https://img.shields.io/badge/python-3.8%2B-3776ab?logo=python&logoColor=white">
  <a href="LICENSE.md"><img alt="license: MIT" src="https://img.shields.io/badge/license-MIT-007ec6"></a>
  <img alt="status: beta" src="https://img.shields.io/badge/status-beta-d54a3c">
</p>

---

`hzforge` installs, uninstalls, diagnoses, and repairs **HUBzero Forge
services** &mdash; Subversion, Git, gitExternal, and Trac &mdash; as
self-contained Apache **drop-ins**, independent of the m4 vhost template.

On a HUBzero hub each tool gets a project area under `/tools/<name>/…`:

| Service       | URL space                          | Apache mechanism |
|---------------|------------------------------------|------------------|
| `svn`         | `/tools/<name>/svn`                | `mod_dav_svn` (`<Location>`) |
| `git`         | `/tools/<name>/git/<name>`         | `git-http-backend` (`ScriptAliasMatch`) |
| `gitExternal` | `/tools/<name>/gitExternal/<name>` | `git-http-backend` (`ScriptAliasMatch`) |
| `trac`        | `/tools/<name>/{wiki,timeline,browser,ticket,…}` | mod_wsgi (default) or mod_python |

hzforge writes **one config file per service** at
`/etc/httpd/<hub>.conf.d/00-forge-<svc>.conf`, picked up by the vhost's existing
`IncludeOptional <hub>.conf.d/*.conf` &mdash; so it never edits the m4-generated
vhost or requires regenerating the vhost. The per-tool `svn.conf` / `git.conf` blocks
still come from the hub's existing MySQL-driven generator; hzforge only *includes*
them and shields them from the CMS catch-all rewrite.

## Documentation

Full docs: **<https://hubzero.github.io/hzforge/>** (sources under [`docs/`](docs/)).

- [Summary](https://hubzero.github.io/hzforge/overview/summary/) &mdash; what it is, at a glance
- [Motivations](https://hubzero.github.io/hzforge/overview/motivations/) &mdash; why it bypasses the m4 vhost
- [Architecture](https://hubzero.github.io/hzforge/reference/architecture/) &mdash; how the drop-ins are wired
- [Services](https://hubzero.github.io/hzforge/reference/services/) &mdash; the four services in detail
- [Usage](https://hubzero.github.io/hzforge/operations/usage/) &mdash; full command reference
- [Migration](https://hubzero.github.io/hzforge/operations/migration/) &mdash; mod_python → mod_wsgi

## Quickstart

Run as **root**. Services are **positional** (space- or comma-separated); a bare
`hzforge` prints help. Preview anything with `--dry-run`.

```sh
sudo python3 hzforge.py install                 # all services
sudo python3 hzforge.py install trac            # one service
sudo python3 hzforge.py install svn git trac
sudo python3 hzforge.py uninstall git           # stop serving git (data kept)
sudo python3 hzforge.py doctor                  # diagnose (exit 1 on FAIL)
sudo python3 hzforge.py doctor git              # diagnose one service
sudo python3 hzforge.py repair                  # fix drift
```

## Commands

| Command | What it does |
|---|---|
| `install [services]` | Install packages, create `/opt/<svc>/tools` dirs (conventional perms), create the `hzsvn`/`hzgit` groups, write the drop-in(s). No services = all. After `install trac` it runs `test` automatically (skip with `--no-test`). |
| `uninstall <services>` | Remove a service's drop-in (and, for trac, unload its interpreter) plus hzforge's own files for it (shim, egg cache, wandisco repo file). **Never** removes packages, the `hzsvn`/`hzgit` groups, or repo data. |
| `doctor [services]` | Read-only diagnosis; exits non-zero on FAIL. Service checks scope to the request; global checks (`configtest`, interpreter state) always run. |
| `repair [services]` | Diagnose, then re-assert the requested services and fix drift, then validate + reload/restart. |
| `test` | Create a throwaway, uniquely-named Trac project, verify it serves over HTTP (`/tools/<name>/wiki` → 200), then remove it. Self-contained (needs no MySQL/forge provisioning). Exits non-zero on failure. |

### Install options

| Option | Default | Notes |
|---|---|---|
| `--trac-handler {mod_wsgi,mod_python}` | `mod_wsgi` | Exactly one interpreter is loaded. |
| `--svn-source {wandisco,appstream}` | `wandisco` | `subversion`+`mod_dav_svn` source; `subversion-python` always from hubzero. |
| `--ldap-url/--ldap-binddn/--ldap-bindpw` | auto-detect | Trac `/login` auth; read from `svn.conf` if omitted. |
| `--force-pip` | off | Reinstall Trac even if importable. |

Common: `--hub <name>` (auto-detected), `--dry-run`, `--no-restart`.

## How it works

- **No carve-out for alias handlers.** `WSGIScriptAliasMatch` (mod_wsgi Trac) and
  `ScriptAliasMatch` (git) self-divert at translate-name, before the CMS rewrite.
  `mod_dav_svn` and mod_python Trac use `<Location>`, so the drop-in shields those
  paths with a vhost-scope `RewriteRule … - [END]`.
- **WSGI shim** (`/opt/trac/wsgi/hubtrac.wsgi`) re-splits `SCRIPT_NAME`/`PATH_INFO`
  and selects the env &mdash; the mod_wsgi replacement for `PythonOption
  TracUriRoot`.
- **Restart vs reload** is decided by comparing the running httpd's loaded modules
  (`/proc/<pid>/maps`) against what's enabled on disk; full restart only when the
  interpreter set must change.
- **Trac ↔ Subversion are decoupled** &mdash; Trac runs without the repo browser;
  the browser is auto-enabled only when the svn service is installed.

See [architecture](https://hubzero.github.io/hzforge/reference/architecture/) for
the full picture.

## Requirements

- Rocky/RHEL 8, Apache 2.4, Python 2.7 (for the Trac stack) and Python 3.8+ (to run
  hzforge). Run as root.
- The `hubzero` yum repo (`hubzero-trac`, `hubzero-trac-mysqlauthz`,
  `subversion-python`); for `--svn-source wandisco`, access to
  `opensource.wandisco.com`; for the mod_wsgi handler, PyPI access.

## License

MIT &mdash; see [LICENSE.md](LICENSE.md). Copyright &copy; 2026 Purdue University.
