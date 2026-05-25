# Requirements

hzforge runs on the HUBzero web node and configures Apache there. It must be run
as **root** (`sudo`).

## Host

- **Rocky / RHEL 8** with **Apache httpd 2.4** (`mod_rewrite`, plus the standard
  HUBzero vhost, which includes `IncludeOptional <hub>.conf.d/*.conf` — that's how
  hzforge's drop-ins get loaded).
- Root access — hzforge installs packages, writes Apache config, and reloads/restarts
  httpd.

## Python

- **Python 3.6+** to run `hzforge.py` itself — it targets RHEL 8's stock `python3`
  (3.6), so it uses f-strings but avoids 3.7+ APIs.
- **Python 2.7** for the Trac stack it manages — the HUBzero Trac plugins and, for the
  mod_wsgi handler, the pip-built `Trac` and `mod_wsgi==4.9.4` (the last
  Python-2-capable mod_wsgi release).

These are separate interpreters: hzforge is a Python 3 orchestration script that sets
up a Python 2 Trac runtime.

## Packages & repositories

- The **`hubzero` yum repo** — provides `hubzero-trac`, `hubzero-trac-mysqlauthz`
  (the `HubzeroPermissionStore` / `hubzeroplugin` components the Trac environments
  require), and `subversion-python` (the Py2 SVN bindings for the Trac repo browser).
- **Subversion / `mod_dav_svn`** — from **`wandisco-svn110`** by default
  (`--svn-source appstream` uses the appstream `subversion:1.10` module instead).
  hzforge writes the wandisco repo file when it's needed.
- **`git`** — for the `git` and `gitExternal` services (`git-http-backend`).
- A build toolchain for the pip-built mod_wsgi (`gcc`, `python2-devel`, `httpd-devel`)
  — pulled in as needed.

## Network

- **`opensource.wandisco.com`** — when `--svn-source wandisco` (the svn default).
- **PyPI** — for the mod_wsgi Trac handler (builds `Trac` and `mod_wsgi==4.9.4`).
- **Local LDAP** (`127.0.0.1`) — the Trac `/login` auth block binds to the hub's LDAP;
  credentials are auto-detected from the generated `svn.conf`.

## Per-tool content

hzforge **includes** the per-tool `svn.conf` / `git.conf` / `gitExternal.conf` blocks
produced by the hub's existing MySQL-driven generator — it does not generate them. The
Trac environments live under `/opt/trac/tools/<name>`, paired with the matching
`/opt/svn/tools/<name>` repositories.
