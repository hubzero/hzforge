# Manual installation

This page documents, step by step, the **manual** procedure to install each of the
two configurations exercised by the integration matrix:

| Option | Trac handler | Services |
|---|---|---|
| **A** | `mod_wsgi` | `trac` + `svn` + `git` + `gitExternal` |
| **B** | `mod_python` | `trac` |

These are exactly the steps [`hzforge install`](usage.md) automates — written out so
you can run them by hand, audit what the tool does, or recover a host without it.
Everything runs as **root** on Rocky/RHEL 8 with Apache 2.4. Substitute your hub name
for `<hub>` (the directory `/etc/httpd/<hub>.conf.d/` must already be included by the
vhost via `IncludeOptional <hub>.conf.d/*.conf`).

> The two Trac interpreters **cannot coexist** — load exactly one of `mod_wsgi` /
> `mod_python`. Switching handlers means disabling the other module and doing a full
> `systemctl restart httpd` (a graceful reload cannot swap an embedded interpreter).

See [Requirements](requirements.md) for the full host/network prerequisites.

---

## Common prerequisites (both options)

### 1. Enable the package repositories

```sh
dnf -y install dnf-plugins-core epel-release
dnf config-manager --set-enabled powertools || dnf config-manager --set-enabled crb

cat >/etc/yum.repos.d/hubzero.repo <<'REPO'
[hubzero]
name=HUBzero
baseurl=http://packages.hubzero.org/rpm/julian-el8
enabled=1
gpgcheck=1
gpgkey=https://packages.hubzero.org/rpm/hubzero-rpm-key-pub-2025
REPO
rpm --import https://packages.hubzero.org/rpm/hubzero-rpm-key-pub-2025
```

### 2. Base packages

```sh
dnf -y install httpd httpd-devel mod_ssl \
  python2 python2-pip python2-devel gcc redhat-rpm-config
```

`httpd-devel` + `gcc` are needed to pip-build mod_wsgi (Option A); `python2`/`python2-pip`
build the Py2 Trac stack used by both handlers.

### 3. The httpd runtime directory

`httpd` needs `/run/httpd`. With systemd this is created by `systemd-tmpfiles`; in a
container/chroot create it yourself before starting httpd:

```sh
install -d -m 0710 -o root -g apache /run/httpd
```

---

## Option A — mod_wsgi: trac + svn + git + gitExternal

### A1. Subversion source

The `svn` service needs `subversion` + `mod_dav_svn`. Pick **one** source.

**AppStream (simplest):**

```sh
dnf -y module enable subversion:1.10
```

**WanDisco 1.10 (matches some production hubs):**

```sh
cat >/etc/yum.repos.d/wandisco-svn110.repo <<'REPO'
[wandisco-svn110]
name=Wandisco SVN 1.10 RPM repository for Rocky 8
baseurl=http://opensource.wandisco.com/rhel/8/svn-1.10/RPMS/$basearch/
enabled=0
gpgcheck=1
gpgkey=http://opensource.wandisco.com/RPM-GPG-KEY-WANdisco
priority=1
module_hotfixes=1
REPO
# if a subversion module stream is enabled, reset it so module_hotfixes wins:
dnf -y module reset subversion
```

With WanDisco, append `--enablerepo=wandisco-svn110` to the `subversion`/`mod_dav_svn`
`dnf install` below. With AppStream, no `--enablerepo` is needed.

### A2. Trac packages and the WSGI stack

```sh
# auth/permission plugin + build deps for the pip-built mod_wsgi (gcc, Python
# and Apache headers).  We deliberately skip the hubzero-trac metapackage --
# its %post pip-installs Trac 1.0.13 and subvertpy, conflicting with the Trac
# pin and the SWIG svn.core we get from subversion-python.
dnf -y install hubzero-trac-mysqlauthz gcc python2-devel httpd-devel

# Trac + mod_wsgi from PyPI. umask 022 so root-built files are world-readable
# (apache must import them); the default umask 0077 would make Trac return 500.
umask 022
pip2 install 'Trac==1.0.14'          # pinned; 1.0.x is DB schema 26 (matches envs, no upgrade)
pip2 install 'mod_wsgi==4.9.4'       # last Python-2-capable mod_wsgi
```

### A3. Directories

```sh
install -d -m 0755 -o apache -g apache /opt/trac
install -d -m 0750 -o apache -g apache /opt/trac/tools
install -d -m 0755 -o apache -g apache /opt/trac/wsgi
```

### A4. The WSGI shim

`/opt/trac/wsgi/hubtrac.wsgi` re-splits the URL so any `/opt/trac/tools/<name>` env
is served generically (the mod_wsgi replacement for `PythonOption TracUriRoot`):

```python
# /opt/trac/wsgi/hubtrac.wsgi   (mode 0644, apache:apache)
import os, re
from trac.web.main import dispatch_request

TOOLS = '/opt/trac/tools'
PAT = re.compile(r'^(/tools/([^/]+))(/.*)?$')

def application(environ, start_response):
    full = environ.get('SCRIPT_NAME', '') + environ.get('PATH_INFO', '')
    m = PAT.match(full)
    if not m or not os.path.isdir(os.path.join(TOOLS, m.group(2))):
        start_response('404 Not Found', [('Content-Type', 'text/plain')])
        return ['No such Trac environment\n']
    environ['trac.env_path'] = os.path.join(TOOLS, m.group(2))
    environ['SCRIPT_NAME'] = m.group(1)
    environ['PATH_INFO'] = m.group(3) or '/'
    return dispatch_request(environ, start_response)
```

### A5. Load mod_wsgi (and ensure mod_python is off)

The `LoadModule`/`WSGIPythonHome` lines come from the pip build:

```sh
mod_wsgi-express module-config
```

Write its output into `/etc/httpd/conf.modules.d/10-wsgi.conf` (mode 0644):

```apache
# Python2 mod_wsgi
LoadModule wsgi_module "/usr/lib64/python2.7/site-packages/mod_wsgi/server/mod_wsgi-py27.so"
WSGIPythonHome "/usr"
WSGISocketPrefix run/wsgi
WSGIRestrictEmbedded On
```

```sh
# the two interpreters can't coexist -- make sure mod_python is not loaded
[ -f /etc/httpd/conf.modules.d/10-python.conf ] && \
  mv /etc/httpd/conf.modules.d/10-python.conf{,.disabled}
```

### A6. The Trac drop-in

`/etc/httpd/<hub>.conf.d/00-forge-trac.conf` (mode 0640). The `WSGIScriptAliasMatch`
self-diverts at translate-name, so **no rewrite carve-out is needed**:

```apache
# HUBzero 'trac' service
RewriteEngine On

WSGIDaemonProcess trac user=apache group=apache processes=2 threads=15 python-home=/usr display-name=%{GROUP}
WSGIApplicationGroup %{GLOBAL}
WSGIScriptAliasMatch "^/tools/[^/]+(?=/(?:wiki|wiki_render|timeline|roadmap|browser|changeset|ticket|newticket|report|query|search|admin|prefs|login|logout|about|diff|attachment|raw-attachment|export|chrome|log|pygments)(?:/|$))" /opt/trac/wsgi/hubtrac.wsgi process-group=trac
<Directory /opt/trac/wsgi>
    <Files hubtrac.wsgi>
        Require all granted
    </Files>
</Directory>
```

*(Optional)* to have Apache authenticate `/login` so Trac sees `REMOTE_USER`, append:

```apache
<LocationMatch "^/tools/[^/]+/login">
    AuthType Basic
    AuthBasicProvider ldap
    AuthName "HUBzero Trac"
    AuthLDAPURL ldap://127.0.0.1/ou=users,dc=hubzero,dc=org
    AuthLDAPBindDN "cn=search,dc=hubzero,dc=org"
    AuthLDAPBindPassword "..."
    Require valid-user
</LocationMatch>
```

### A7. svn service

```sh
# packages: subversion + mod_dav_svn (from the A1 source) + the Py2 SWIG bindings.
# add --enablerepo=wandisco-svn110 here if you chose WanDisco in A1.
dnf -y install subversion mod_dav_svn
dnf -y install subversion-python          # from hubzero; also lights up Trac's repo browser

groupadd -f hzsvn
install -d -m 0755 -o apache -g apache /opt/svn
install -d -m 0750 -o apache -g apache /opt/svn/tools
install -d -m 0700 -o apache -g apache /etc/httpd/<hub>.conf.d/svn
```

`/etc/httpd/<hub>.conf.d/00-forge-svn.conf` (mode 0640). `mod_dav_svn` is a `<Location>`
handler, so it **must** be shielded from the CMS catch-all rewrite:

```apache
# HUBzero 'svn' service
RewriteEngine On

# DAV-svn is a <Location> handler -> shield from the CMS catch-all rewrite
RewriteRule "^/tools/[^/]+/svn(/|$)" - [END]

IncludeOptional /etc/httpd/<hub>.conf.d/svn/svn.conf
```

The per-tool `<Location /tools/<name>/svn>` blocks live in `svn/svn.conf`, which is
generated from MySQL by the hub's forge tooling — hzforge only *includes* it.

### A8. git and gitExternal services

```sh
dnf -y install git
groupadd -f hzgit
install -d -m 0755 -o apache -g apache /opt/git          /opt/git/tools
install -d -m 0755 -o apache -g apache /opt/gitExternal  /opt/gitExternal/tools
install -d -m 0700 -o apache -g apache /etc/httpd/<hub>.conf.d/git
```

`/etc/httpd/<hub>.conf.d/00-forge-git.conf` (mode 0640):

```apache
# HUBzero 'git' service
RewriteEngine On
# git http-backend (ScriptAliasMatch self-diverts; shield non-protocol paths)
RewriteRule "^/tools/[^/]+/git(/|$)" - [END]
IncludeOptional /etc/httpd/<hub>.conf.d/git/git.conf
```

`/etc/httpd/<hub>.conf.d/00-forge-gitExternal.conf` (mode 0640):

```apache
# HUBzero 'gitExternal' service
RewriteEngine On
RewriteRule "^/tools/[^/]+/gitExternal(/|$)" - [END]
IncludeOptional /etc/httpd/<hub>.conf.d/git/gitExternal.conf
```

As with svn, the `ScriptAliasMatch` git-http-backend routes live in `git/git.conf` /
`git/gitExternal.conf` (MySQL-generated); the drop-ins only include them.

### A9. Validate and start

```sh
apachectl configtest
systemctl restart httpd          # full restart: the interpreter module set changed
#   container/chroot without systemd:  httpd -k start   (or  httpd -k restart)
```

Verify a throwaway env serves (the generic shim handles any env):

```sh
trac-admin /opt/trac/tools/probe initenv probe sqlite:db/trac.db
chown -R apache:apache /opt/trac/tools/probe
curl -sk https://<hub>/tools/probe/wiki | head    # expect a Trac wiki page
rm -rf /opt/trac/tools/probe
```

---

## Option B — mod_python: trac

The legacy in-process handler. Unlike mod_wsgi there is **no generic route**: each Trac
environment needs its own `<Location>` block, so the drop-in is regenerated whenever you
add or remove envs.

### B1. (Optional) Trac repository browser bindings

Trac doesn't need Subversion to run, but if you want the **repo browser** working
under mod_python you need the SWIG svn bindings from the hubzero repo:

```sh
dnf -y install subversion-python      # provides svn.core for Trac's browser
```

Skip this section entirely if you don't need the repo browser.

### B2. Trac packages and mod_python

```sh
# auth/permission plugin (we deliberately skip the hubzero-trac metapackage --
# its %post pip-installs Trac 1.0.13, conflicting with our pin below).
dnf -y install hubzero-trac-mysqlauthz

umask 022
pip2 install 'Trac==1.0.14'               # mod_python loads Trac from site-packages

# mod_python comes from the hubzero (julian-el8) repo, built for the Python 2.7 it embeds
dnf -y install mod_python
```

### B3. Directories and egg cache

```sh
install -d -m 0755 -o apache -g apache /opt/trac
install -d -m 0750 -o apache -g apache /opt/trac/tools
install -d -m 0755 -o apache -g apache /opt/trac/.egg-cache
```

### B4. Load mod_python (and ensure mod_wsgi is off)

```sh
cat >/etc/httpd/conf.modules.d/10-python.conf <<'EOF'
LoadModule python_module modules/mod_python.so
EOF

[ -f /etc/httpd/conf.modules.d/10-wsgi.conf ] && \
  mv /etc/httpd/conf.modules.d/10-wsgi.conf{,.disabled}
```

### B5. The Trac drop-in

`/etc/httpd/<hub>.conf.d/00-forge-trac.conf` (mode 0640). The verb paths are a
`<Location>` handler, so they are shielded from the CMS catch-all; then **one
`<Location>` per env**:

```apache
# HUBzero 'trac' service (mod_python)
RewriteEngine On
# Trac via mod_python -- <Location> per env; shield verbs from the CMS rewrite
RewriteRule "^/tools/[^/]+/(wiki|wiki_render|timeline|roadmap|browser|changeset|ticket|newticket|report|query|search|admin|prefs|login|logout|about|diff|attachment|raw-attachment|export|chrome|log|pygments)(/|$)" - [END]
PythonOption PYTHON_EGG_CACHE /opt/trac/.egg-cache

# repeat this block for every env under /opt/trac/tools/<env>/conf/trac.ini
<Location /tools/myproject>
    SetHandler mod_python
    PythonHandler trac.web.modpython_frontend
    PythonInterpreter main_interpreter
    PythonOption TracEnv /opt/trac/tools/myproject
    PythonOption TracUriRoot /tools/myproject
</Location>
```

> Because the route is per-env, you must **rebuild this drop-in and reload httpd each
> time an env is created or removed** (`hzforge repair trac` does exactly this by
> enumerating `/opt/trac/tools/*/conf/trac.ini`).

### B6. Validate and start

```sh
apachectl configtest
systemctl restart httpd          # full restart: the interpreter module set changed
curl -sk https://<hub>/tools/myproject/wiki | head    # expect a Trac wiki page
```

---

## Doing it with hzforge instead

Each option above is a single command:

```sh
# Option A
sudo python3 hzforge.py install trac svn git gitExternal --svn-source appstream

# Option B
sudo python3 hzforge.py install trac --trac-handler mod_python --svn-source appstream
```

hzforge additionally decides restart-vs-reload from the running interpreter set, fixes
pip file permissions, creates `/run/httpd` on non-systemd hosts, and runs the
[self-test](usage.md#test-services). See [Usage](usage.md) for the full command set and
[Architecture](architecture.md) for why the drop-ins are wired this way.
