# Architecture

How hzforge wires the Forge services into Apache without touching the m4 vhost.

## Drop-in placement

The HUBzero vhost includes `IncludeOptional <hub>.conf.d/*.conf` inside its
`<VirtualHost>`. hzforge writes one file per service there:

```
/etc/httpd/<hub>.conf.d/00-forge-svn.conf
/etc/httpd/<hub>.conf.d/00-forge-git.conf
/etc/httpd/<hub>.conf.d/00-forge-gitExternal.conf
/etc/httpd/<hub>.conf.d/00-forge-trac.conf
```

The `00-forge-` prefix loads them early and predictably relative to other
drop-ins. Each file is self-contained: its own `RewriteEngine On`, its `[END]`
shield (where needed), an `IncludeOptional` of the externally-generated per-tool
block, and (for trac) the handler.

## Alias handlers vs. `<Location>` handlers — the carve-out

The vhost's catch-all sends everything that isn't a real file to the CMS:

```apache
RewriteCond %{REQUEST_FILENAME} !-f
RewriteCond %{REQUEST_FILENAME} !-d
RewriteRule (.*) index.php
```

How a service avoids that rule decides whether it needs a "carve-out":

- **Alias-based handlers self-divert.** `WSGIScriptAliasMatch` (mod_wsgi Trac) and
  `ScriptAliasMatch` (git, gitExternal) map the request to a script at Apache's
  **translate-name** phase — *before* the per-directory rewrite runs. No carve-out
  is needed. (Verified: `/tools/<t>/query` and `/report`, which aren't in the old
  m4 carve-out, still reach Trac under mod_wsgi.)

- **`<Location>` handlers stay in the docroot.** `mod_dav_svn` (svn) and
  mod_python Trac use `<Location>`, so the request still maps into the docroot
  where the rewrite would clobber it. hzforge shields those paths with a
  vhost-scope rule using the `[END]` flag (which also stops *per-directory*
  rewriting):

  ```apache
  RewriteRule "^/tools/[^/]+/svn(/|$)" - [END]
  ```

This is why the old m4 needed a `USE_TRAC`/`USE_SUBVERSION` carve-out for the
mod_python era, and why the mod_wsgi era needs none for Trac.

## The WSGI shim (TracUriRoot replacement)

mod_python let you declare a Trac mount with `PythonOption TracUriRoot
/tools/<name>`. mod_wsgi has no such knob — Trac derives its base URL from
`SCRIPT_NAME`. A small shim at `/opt/trac/wsgi/hubtrac.wsgi` restores that control:
it re-splits `SCRIPT_NAME=/tools/<name>` and `PATH_INFO=/<verb>/…` from the request
and selects the matching env under `/opt/trac/tools/<name>` — regardless of how
Apache split the URL.

## Trac handlers

- **mod_wsgi** (default): `WSGIDaemonProcess` + `WSGIScriptAliasMatch` routing the
  Trac verbs to the shim. Trac core is pip-installed (`Trac`, `mod_wsgi==4.9.4` —
  the last Python-2-capable release), plus the `hubzero-trac` plugins.
- **mod_python** (legacy): one `<Location /tools/<name>>` per environment plus the
  trac-verb `[END]` shield. Selectable with `--trac-handler mod_python`.

The two interpreters cannot coexist in one Apache, so exactly one is loaded;
`10-wsgi.conf` and `10-python.conf` are enabled/disabled accordingly.

## Restart vs. reload

A graceful reload cannot hot-swap an embedded interpreter. hzforge decides by
comparing the **running** server's loaded modules (read from `/proc/<pid>/maps`)
against what's enabled on disk:

- interpreter set must change → full `systemctl restart httpd`;
- otherwise → graceful `systemctl reload httpd`.

Every apply runs `apachectl configtest` first and aborts on failure, leaving the
running server untouched.

## Trac ↔ Subversion are decoupled

Trac runs fine without Subversion; only the repository *browser* degrades to an
error. So `install trac` does **not** pull `mod_dav_svn` or the `subversion-python`
SWIG bindings. The browser is auto-enabled only when the svn service is also
installed (which brings `subversion-python`), and `doctor` checks the browser only
when svn is configured. (`hzforge` still enables the svn module/repo during trac
setup, because the `hubzero-trac` rpm requires `subversion-devel`.)

## Package sources & permissions

- **Subversion / mod_dav_svn**: from `wandisco-svn110` by default
  (`--svn-source appstream` to use the appstream module instead);
  `subversion-python` always from the hubzero repo.
- **pip files & umask**: pip installs run under `umask 022` so files are readable
  by `apache`. Permission remediation is *conditional* — only when a probe shows
  apache can't import Trac — and scoped to the Trac/mod_wsgi packages, never a
  blanket `chmod` of `site-packages`.
