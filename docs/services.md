# Services

hzforge manages four Forge services. Each is an independent drop-in; install,
uninstall, and diagnose them separately.

Shared setup for every service:

- create `/opt/<svc>/tools` with conventional permissions;
- create the `hzsvn` / `hzgit` group if missing (forge `chgrp`s repos to it; the
  `apache` + `apps` membership those repos need is provisioned by the forge setup);
- create the `<hub>.conf.d/{svn,git}` include dirs (0700, apache);
- write `00-forge-<svc>.conf` and validate + reload.

> Per-tool repository blocks (`svn/svn.conf`, `git/git.conf`,
> `git/gitExternal.conf`) come from the hub's existing MySQL-driven generator.
> hzforge **includes** them; it does not generate them.

## svn — Subversion over WebDAV

`mod_dav_svn` serves each repo at `/tools/<name>/svn` via a `<Location>` with
`SVNPath /opt/svn/tools/<name>` and LDAP auth (from the generated `svn.conf`).
Because it's a `<Location>` handler, the drop-in adds a `[END]` shield so the CMS
catch-all rewrite doesn't capture `/tools/<name>/svn`.

- Packages: `subversion` + `mod_dav_svn` (from `wandisco-svn110` by default;
  `--svn-source appstream` for the appstream module), plus `subversion-python`
  (from hubzero) — which also lights up Trac's repo browser.
- Repos: `/opt/svn/tools/<name>`.

## git — local Git over HTTP

`git-http-backend` serves `/tools/<name>/git/<name>` via a `ScriptAliasMatch`
(`GIT_PROJECT_ROOT=/opt/git/tools`). Because it's alias-based it self-diverts at
translate-name, so no carve-out is required (a `[END]` is still added for parity
with the m4 behavior on non-protocol paths).

- Package: `git`.
- Repos: `/opt/git/tools/<name>`.

## gitExternal — external Git mirror

Same mechanism as `git` but rooted at `/opt/gitExternal/tools` and served at
`/tools/<name>/gitExternal/<name>`. Used for repositories mirrored/published
outside the hub's own git area.

- Package: `git`.
- Repos: `/opt/gitExternal/tools/<name>`.

## trac — project wiki, tickets, timeline, browser

Per-tool Trac environments under `/opt/trac/tools/<name>`, served at the Trac
verbs (`wiki`, `timeline`, `roadmap`, `browser`, `changeset`, `ticket`,
`newticket`, `report`, `query`, `search`, `admin`, `prefs`, `login`, `logout`,
`about`, `diff`, `attachment`, `raw-attachment`, `export`, `chrome`, `log`,
`pygments`).

- Handler: mod_wsgi (default) or mod_python — see
  [architecture](../architecture/).
- Packages: `hubzero-trac-mysqlauthz` (provides `HubzeroPermissionStore` and the
  `hubzeroplugin` components the envs require); for the mod_wsgi handler, build
  deps (`gcc`, `python2-devel`, `httpd-devel`) plus Trac core + `mod_wsgi==4.9.4`
  via pip. The `hubzero-trac` metapackage is deliberately not installed — its
  `%post` pip-installs Trac 1.0.13 and subvertpy, both unwanted (hzforge pins
  Trac via `TRAC_SPEC` and uses the SWIG `svn.core` from `subversion-python`).
- Envs: `/opt/trac/tools/<name>` (paired with the matching `/opt/svn/tools/<name>`
  repository for the browser).
- Repo browser is optional — see
  [Trac ↔ Subversion are decoupled](../architecture/#trac--subversion-are-decoupled).
