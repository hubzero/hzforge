# Migration: mod_python → mod_wsgi

Stage 1 of moving HUBzero Trac off the obsolete mod_python handler onto mod_wsgi,
keeping the existing Python 2 Trac environments. `hzforge install trac` automates
this; the steps below explain what it does and the gotchas it handles.

## Why it's more than a handler swap

mod_python let you bind a handler to any `<Location>` and declare the mount point
with `PythonOption TracUriRoot /tools/<name>`. mod_wsgi has no such knob — Trac
derives its base URL from `SCRIPT_NAME`. hzforge restores that control with a small
WSGI shim that re-splits `SCRIPT_NAME`/`PATH_INFO` itself (see
[architecture](../../reference/architecture/#the-wsgi-shim-tracuriroot-replacement)).

## What the install does

1. **Packages** — `hubzero-trac` (+ `hubzero-trac-mysqlauthz`, which provides
   `HubzeroPermissionStore` and the `hubzeroplugin` components the envs need);
   Trac core and `mod_wsgi==4.9.4` via pip (the last Python-2-capable release).
2. **Shim** — `/opt/trac/wsgi/hubtrac.wsgi`.
3. **Module config** — load mod_wsgi (`10-wsgi.conf`), **unload mod_python**
   (`10-python.conf` → `.disabled`); the two interpreters can't coexist.
4. **Drop-in** — `00-forge-trac.conf` with `WSGIScriptAliasMatch` routing the Trac
   verbs to the shim, plus the `/login` LDAP auth block. No CMS carve-out is needed
   because the alias self-diverts.
5. **Validate + cut over** — `apachectl configtest`, then a **full restart** (a
   graceful reload cannot swap the embedded interpreter).

## Gotchas (handled by hzforge)

| Symptom | Cause | Handling |
|---|---|---|
| `Permission denied` on an egg-info under apache; Trac 500s | root `umask 0077` makes pip files unreadable | pip installs run with `umask 022`; conditional, package-scoped `chmod a+rX` only if apache actually can't import Trac |
| `mod_wsgi: cannot be used with mod_python` → `Configuration Failed, exiting` | both interpreter modules loaded | mod_python is unloaded as part of the cutover |
| httpd exits during a graceful reload | reload can't hot-swap the interpreter | hzforge does a full restart when the interpreter set changes |
| repo browser shows "Subversion support not available" | `subversion-python` SWIG bindings absent | optional — installed only when the svn service is also installed |

A non-obvious detail when testing by hand: the hub vhost listens on a specific
public IP, not loopback, so probe it with
`curl --resolve <fqdn>:443:<listen-ip> https://<fqdn>/tools/<env>/wiki`.

## Verify

```
sudo python3 hzforge.py doctor trac
```

Expect `apachectl configtest: Syntax OK`, `apache can import Trac`, the shim
present, and the interpreters matching disk. A live check:

```
curl -k --resolve <fqdn>:443:<ip> -o /dev/null -w '%{http_code}\n' \
  https://<fqdn>/tools/<env>/wiki      # 200 = Trac via mod_wsgi
```

## Stage 2 (later): Python 3 / Trac 1.6

`dnf install python3.11-mod_wsgi` (also 4.9.4) replaces the pip-built
`LoadModule` line; the drop-in, shim, and auth block carry over unchanged. Trac is
upgraded to 1.6 (Python 3) and the 2.2 hubzero plugins are ported. Because both
stages use mod_wsgi 4.9.4, only the `LoadModule` line and the interpreter change.

## Rollback

Config-only — the Trac envs, SVN repos, `trac.ini`, and plugins are untouched:

```
sudo python3 hzforge.py uninstall trac    # removes the drop-in, unloads mod_wsgi
```

Then restore the previous mod_python config if you had one.
