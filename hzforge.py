#!/usr/bin/env python3
"""
hzforge -- install / uninstall / doctor / repair HUBzero Forge services
(svn, git, gitExternal, trac) as Apache drop-ins, independent of the m4
template / hzcms.

Modeled on hzcms's subversionConfigure/gitConfigure/tracConfigure (dirs, perms,
hzsvn/hzgit groups) but it ALSO installs the packages (which hzcms leaves to rpm
deps) and writes the Apache config as one drop-in per service
(/etc/httpd/<hub>.conf.d/00-forge-<svc>.conf) instead of regenerating the m4 vhost.

Per-tool svn.conf/git.conf/gitExternal.conf are NOT generated here -- they come
from the hub's existing MySQL-driven generator; this script just includes them
and protects them from the CMS catch-all rewrite.

Trac handler is selectable:
  --trac-handler mod_wsgi   (default) Py2 Trac via mod_wsgi 4.9.4 + a shim.
  --trac-handler mod_python (legacy)  per-tool <Location> + trac.web.modpython_frontend.
The two interpreters cannot coexist, so the script loads exactly one.

Carve-out: none in the m4. svn (DAV) and mod_python-trac (<Location>) are
protected from the per-directory CMS rewrite by vhost-scope `RewriteRule ... [END]`
rules in the drop-in; git/gitExternal (ScriptAliasMatch) and mod_wsgi-trac
(WSGIScriptAliasMatch) self-divert at the translate-name phase.

Run as root.  Services are positional; preview any command with --dry-run:
  hzforge install trac                       # trac only (mod_wsgi)
  hzforge install                            # no services = all
  hzforge install svn git gitExternal trac
  hzforge install trac svn --trac-handler mod_python
  hzforge uninstall git                      # stop serving git (data kept)
  hzforge uninstall trac --purge            # also remove packages/artifacts
  hzforge doctor                             # diagnose all (or: hzforge doctor git)
  hzforge repair                             # fix drift (or: hzforge repair trac)
Bare `hzforge` (no command) prints help.
"""

import argparse
import os
import re
import subprocess
import sys
import tempfile

# ---------------------------------------------------------------------------- #
# Defaults
# ---------------------------------------------------------------------------- #
TRAC_SPEC     = "Trac>=1.0,<1.1"     # match envs at DB schema 26 (no upgrade)
MODWSGI_SPEC  = "mod_wsgi==4.9.4"    # last Python-2-capable mod_wsgi release
ALL_SERVICES  = ["svn", "git", "gitExternal", "trac"]

OPT = {
    "trac":        ("/opt/trac",          0o755),
    "trac_tools":  ("/opt/trac/tools",    0o750),
    "svn":         ("/opt/svn",           0o755),
    "svn_tools":   ("/opt/svn/tools",     0o750),
    "git":         ("/opt/git",           0o755),
    "git_tools":   ("/opt/git/tools",     0o755),
    "gext":        ("/opt/gitExternal",   0o755),
    "gext_tools":  ("/opt/gitExternal/tools", 0o755),
}

WSGI_DIR     = "/opt/trac/wsgi"
SHIM_PATH    = os.path.join(WSGI_DIR, "hubtrac.wsgi")
EGG_CACHE    = "/opt/trac/.egg-cache"
MODCONF_WSGI = "/etc/httpd/conf.modules.d/10-wsgi.conf"
MODCONF_PY   = "/etc/httpd/conf.modules.d/10-python.conf"
DROPIN_PREFIX = "00-forge-"   # one file per service: 00-forge-svn.conf, 00-forge-trac.conf, ...
WANDISCO_REPO_PATH = "/etc/yum.repos.d/wandisco-svn110.repo"

WANDISCO_REPO = """\
[wandisco-svn110]
name=Wandisco SVN 1.10 RPM repository for Rocky 8
baseurl=http://opensource.wandisco.com/rhel/8/svn-1.10/RPMS/$basearch/
enabled=0
gpgcheck=1
gpgkey=http://opensource.wandisco.com/RPM-GPG-KEY-WANdisco
priority=1
module_hotfixes=1
"""

TRAC_VERBS = [
    "wiki", "wiki_render", "timeline", "roadmap", "browser", "changeset",
    "ticket", "newticket", "report", "query", "search", "admin", "prefs",
    "login", "logout", "about", "diff", "attachment", "raw-attachment",
    "export", "chrome", "log", "pygments",
]

SHIM_CONTENT = """\
# HUBzero Trac WSGI entry point.  Maps /tools/<name>/<verb>/... to the Trac env
# at /opt/trac/tools/<name>, fixing SCRIPT_NAME=/tools/<name>, PATH_INFO=/<verb>/...
# regardless of how Apache split the URL (mod_wsgi's TracUriRoot replacement).
import os, re
from trac.web.main import dispatch_request

TOOLS = '/opt/trac/tools'
PAT = re.compile(r'^(/tools/([^/]+))(/.*)?$')

def application(environ, start_response):
    full = environ.get('SCRIPT_NAME', '') + environ.get('PATH_INFO', '')
    m = PAT.match(full)
    if not m or not os.path.isdir(os.path.join(TOOLS, m.group(2))):
        start_response('404 Not Found', [('Content-Type', 'text/plain')])
        return ['No such Trac environment\\n']
    environ['trac.env_path'] = os.path.join(TOOLS, m.group(2))
    environ['SCRIPT_NAME'] = m.group(1)
    environ['PATH_INFO'] = m.group(3) or '/'
    return dispatch_request(environ, start_response)
"""


# ---------------------------------------------------------------------------- #
# Plumbing
# ---------------------------------------------------------------------------- #
class Ctx:
    def __init__(self, dry):
        self.dry = dry
        self.config_changed = False
        self.notes = []

CTX = None
ARGS = None

def log(m):   print("    " + m)
def step(m):  print("\n==> " + m)
def warn(m):  print("[!] " + m); CTX.notes.append(m)
def die(m):   print("[FATAL] " + m); sys.exit(1)


def run(cmd, check=True, capture=False, mutating=True):
    pretty = " ".join(cmd)
    if mutating and CTX.dry:
        log("[dry-run] " + pretty); return ""
    log("$ " + pretty)
    res = subprocess.run(cmd, stdout=subprocess.PIPE if capture else None,
                         stderr=subprocess.STDOUT if capture else None,
                         universal_newlines=True)
    if check and res.returncode != 0:
        if capture and res.stdout:
            print(res.stdout)
        die("command failed (rc=%d): %s" % (res.returncode, pretty))
    return (res.stdout or "") if capture else ""


def rpm_installed(pkg):
    return subprocess.run(["rpm", "-q", pkg], stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL).returncode == 0


def group_exists(g):
    return subprocess.run(["getent", "group", g], stdout=subprocess.DEVNULL).returncode == 0


def py2_can_import(mod):
    return subprocess.run(["python2", "-c", "import %s" % mod],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


def running_has_so(soname):
    """Is <soname> mapped into the RUNNING httpd? (decides restart vs reload)."""
    pids = subprocess.run(["pgrep", "-x", "httpd"], stdout=subprocess.PIPE,
                          universal_newlines=True).stdout.split()
    for pid in pids:
        try:
            with open("/proc/%s/maps" % pid) as fh:
                if soname in fh.read():
                    return True
        except OSError:
            pass
    return False


def makedir(path, mode, owner="apache", group="apache"):
    import pwd, grp, stat
    if os.path.isdir(path):
        st = os.stat(path)
        if (stat.S_IMODE(st.st_mode) == mode and
                st.st_uid == pwd.getpwnam(owner).pw_uid and
                st.st_gid == grp.getgrnam(group).gr_gid):
            return
    if CTX.dry:
        log("[dry-run] mkdir %s (mode %o, %s:%s)" % (path, mode, owner, group)); return
    if not os.path.isdir(path):
        os.makedirs(path)
    os.chmod(path, mode)
    os.chown(path, pwd.getpwnam(owner).pw_uid, grp.getgrnam(group).gr_gid)
    log("dir: %s (%o %s:%s)" % (path, mode, owner, group))


def write_file(path, content, mode, owner="root", group="root"):
    import pwd, grp, stat
    cur = None
    if os.path.exists(path):
        with open(path) as fh:
            cur = fh.read()
    ok = (cur == content and os.path.exists(path))
    if ok:
        st = os.stat(path)
        ok = (stat.S_IMODE(st.st_mode) == mode and
              st.st_uid == pwd.getpwnam(owner).pw_uid and
              st.st_gid == grp.getgrnam(group).gr_gid)
    if ok:
        log("unchanged: " + path); return False
    if CTX.dry:
        log("[dry-run] write %s (mode %o, %s:%s)" % (path, mode, owner, group)); return True
    d = os.path.dirname(path)
    if not os.path.isdir(d):
        os.makedirs(d)
    fd, tmp = tempfile.mkstemp(dir=d)
    with os.fdopen(fd, "w") as fh:
        fh.write(content)
    os.replace(tmp, path)
    os.chmod(path, mode)
    os.chown(path, pwd.getpwnam(owner).pw_uid, grp.getgrnam(group).gr_gid)
    log("wrote: " + path)
    return True


def add_to_group_if_present(group, users):
    if not group_exists(group):
        log("group %s absent -> skip membership (matches hzcms)" % group); return
    for u in users:
        run(["usermod", "-aG", group, u], check=False)


# ---------------------------------------------------------------------------- #
# Package helpers
# ---------------------------------------------------------------------------- #
def dnf_install(pkgs, enablerepo=None):
    cmd = ["dnf", "-y", "--disablerepo=media-*"]
    if enablerepo:
        cmd += ["--enablerepo=" + enablerepo]
    cmd += ["install"] + pkgs
    run(cmd)


def ensure_wandisco_repo():
    write_file(WANDISCO_REPO_PATH, WANDISCO_REPO, 0o644)


def _svn_repo():
    return "wandisco-svn110" if ARGS.svn_source == "wandisco" else None


def _enable_svn_source():
    """Make subversion-* installable (needed even for trac: hubzero-trac rpm-requires
    subversion-devel). Does NOT install anything itself."""
    if ARGS.svn_source == "wandisco":
        ensure_wandisco_repo()
        # let non-modular wandisco packages supersede the appstream subversion module
        run(["dnf", "-y", "module", "reset", "subversion"], check=False)
    else:
        run(["dnf", "-y", "module", "enable", "subversion:1.10"], check=False)


_SVN_PKGS_DONE = False

def ensure_subversion_packages():
    """svn SERVICE package set: subversion + mod_dav_svn (chosen source) + the
    subversion-python SWIG bindings (from hubzero). Installing these also lights up
    Trac's repo browser."""
    global _SVN_PKGS_DONE
    if _SVN_PKGS_DONE:
        return
    _SVN_PKGS_DONE = True
    _enable_svn_source()
    dnf_install(["subversion", "mod_dav_svn"], enablerepo=_svn_repo())
    if not rpm_installed("subversion-python"):      # Py2 SWIG bindings, always from hubzero
        dnf_install(["subversion-python"])


# ---------------------------------------------------------------------------- #
# Subsystem setup (dirs/groups/packages) -- modeled on hzcms *Configure()
# ---------------------------------------------------------------------------- #
def setup_svn():
    step("Subversion (DAV) -- packages, group, dirs")
    ensure_subversion_packages()
    add_to_group_if_present("hzsvn", ["apps", "apache"])
    makedir(*OPT["svn"]); makedir(*OPT["svn_tools"])
    makedir(os.path.join(ARGS.include_dir, "svn"), 0o700)


def setup_git():
    step("Git (local) -- package, group, dirs")
    if not rpm_installed("git"):
        dnf_install(["git"])
    add_to_group_if_present("hzgit", ["apps", "apache"])
    makedir(*OPT["git"]); makedir(*OPT["git_tools"])
    makedir(os.path.join(ARGS.include_dir, "git"), 0o700)


def setup_gitexternal():
    step("Git (external) -- dirs")
    if not rpm_installed("git"):
        dnf_install(["git"])
    makedir(*OPT["gext"]); makedir(*OPT["gext_tools"])
    makedir(os.path.join(ARGS.include_dir, "git"), 0o700)


def setup_trac():
    step("Trac -- packages, dirs, handler (%s)" % ARGS.trac_handler)
    # plugins (HubzeroPermissionStore + hubzeroplugin); hubzero-trac rpm-requires
    # subversion-devel, so the svn source must be enabled -- but we do NOT install
    # the svn repo browser bindings here. Trac runs fine without them; the browser
    # is auto-enabled only when the svn service is installed (it pulls subversion-python).
    if not rpm_installed("hubzero-trac"):
        _enable_svn_source()
        dnf_install(["hubzero-trac"], enablerepo=_svn_repo())
    makedir(*OPT["trac"]); makedir(*OPT["trac_tools"])
    if ARGS.trac_handler == "mod_wsgi":
        _trac_modwsgi()
    else:
        _trac_modpython()


def _trac_modwsgi():
    os.umask(0o022)  # so root-built pip files are readable by apache
    if py2_can_import("trac") and not ARGS.force_pip:
        log("Trac importable (skip pip; --force-pip to reinstall)")
    else:
        run(["sh", "-c", "umask 022; pip2 install '%s'" % ARGS.trac_spec])
    if not os.path.exists(_modwsgi_so()) and not py2_can_import("mod_wsgi"):
        run(["sh", "-c", "umask 022; pip2 install '%s'" % ARGS.modwsgi_spec])
    else:
        log("mod_wsgi present (skip pip)")
    # shim
    makedir(WSGI_DIR, 0o755)
    write_file(SHIM_PATH, SHIM_CONTENT, 0o644, "apache", "apache") and _mark()
    # server-scope module config; ensure mod_python NOT loaded
    _ensure_modwsgi_loaded()
    _ensure_modpython_unloaded()


def _trac_modpython():
    # mod_python serves Trac in-process; ensure the module is loaded and mod_wsgi isn't
    if not rpm_installed("mod_python"):
        dnf_install(["mod_python"])
    makedir(EGG_CACHE, 0o755)
    _ensure_modpython_loaded()
    _ensure_modwsgi_unloaded()


def _modwsgi_so():
    out = subprocess.run(["mod_wsgi-express", "module-config"],
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                         universal_newlines=True).stdout or ""
    m = re.search(r'LoadModule\s+wsgi_module\s+"([^"]+)"', out)
    return m.group(1) if m else ""


def _ensure_modwsgi_loaded():
    out = subprocess.run(["mod_wsgi-express", "module-config"], stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, universal_newlines=True).stdout or ""
    load = next((l for l in out.splitlines() if l.startswith("LoadModule")),
                'LoadModule wsgi_module "<run mod_wsgi-express module-config>"')
    home = next((l for l in out.splitlines() if l.startswith("WSGIPythonHome")),
                'WSGIPythonHome "/usr"')
    content = ("# Python2 mod_wsgi -- managed by setup_tool_services.py\n"
               + load + "\n" + home + "\nWSGISocketPrefix run/wsgi\nWSGIRestrictEmbedded On\n")
    write_file(MODCONF_WSGI, content, 0o644) and _mark()


def _ensure_modwsgi_unloaded():
    if os.path.exists(MODCONF_WSGI):
        _rename(MODCONF_WSGI, MODCONF_WSGI + ".disabled")


def _ensure_modpython_loaded():
    dis = MODCONF_PY + ".disabled"
    if os.path.exists(dis) and not os.path.exists(MODCONF_PY):
        _rename(dis, MODCONF_PY)
    elif not os.path.exists(MODCONF_PY):
        write_file(MODCONF_PY, "LoadModule python_module modules/mod_python.so\n", 0o644) and _mark()


def _ensure_modpython_unloaded():
    if os.path.exists(MODCONF_PY):
        _rename(MODCONF_PY, MODCONF_PY + ".disabled")


def _rename(a, b):
    if CTX.dry:
        log("[dry-run] mv %s -> %s" % (a, b))
    else:
        os.rename(a, b); log("mv %s -> %s" % (a, b))
    _mark()


def _mark():
    CTX.config_changed = True
    return True


# ---------------------------------------------------------------------------- #
# Per-service drop-ins -- one file each (vhost scope via IncludeOptional *.conf)
# ---------------------------------------------------------------------------- #
def dropin_path(svc):
    return os.path.join(ARGS.include_dir, DROPIN_PREFIX + svc + ".conf")


def write_dropin(services):
    """Write one drop-in per requested service (others left untouched)."""
    for svc in [s for s in ALL_SERVICES if s in services]:
        write_service_conf(svc)


def write_service_conf(svc):
    inc = ARGS.include_dir
    verbs = "|".join(TRAC_VERBS)
    L = ["# HUBzero '%s' service -- managed by hzforge" % svc,
         "# Self-contained drop-in (vhost scope); independent of the m4 template / hzcms.",
         "RewriteEngine On",
         ""]
    if svc == "svn":
        L += ['# DAV-svn is a <Location> handler -> shield from the CMS catch-all rewrite',
              'RewriteRule "^/tools/[^/]+/svn(/|$)" - [END]',
              "",
              "IncludeOptional %s/svn/svn.conf" % inc]
    elif svc == "git":
        L += ['# git http-backend (ScriptAliasMatch self-diverts; shield non-protocol paths)',
              'RewriteRule "^/tools/[^/]+/git(/|$)" - [END]',
              "",
              "IncludeOptional %s/git/git.conf" % inc]
    elif svc == "gitExternal":
        L += ['RewriteRule "^/tools/[^/]+/gitExternal(/|$)" - [END]',
              "",
              "IncludeOptional %s/git/gitExternal.conf" % inc]
    elif svc == "trac":
        if ARGS.trac_handler == "mod_wsgi":
            L += ["# Trac via mod_wsgi (WSGIScriptAliasMatch self-diverts; no carve-out needed)",
                  "WSGIDaemonProcess trac user=apache group=apache processes=2 threads=15 "
                  "python-home=/usr display-name=%{GROUP}",
                  "WSGIApplicationGroup %{GLOBAL}",
                  'WSGIScriptAliasMatch "^/tools/[^/]+(?=/(?:%s)(?:/|$))" %s process-group=trac'
                  % (verbs, SHIM_PATH),
                  "<Directory %s>" % WSGI_DIR,
                  "    <Files hubtrac.wsgi>",
                  "        Require all granted",
                  "    </Files>",
                  "</Directory>"]
        else:
            L += ["# Trac via mod_python -- <Location> per env; shield verbs from the CMS rewrite",
                  'RewriteRule "^/tools/[^/]+/(%s)(/|$)" - [END]' % verbs,
                  "PythonOption PYTHON_EGG_CACHE %s" % EGG_CACHE]
            for env in _trac_envs():
                L += ["<Location /tools/%s>" % env,
                      "    SetHandler mod_python",
                      "    PythonHandler trac.web.modpython_frontend",
                      "    PythonInterpreter main_interpreter",
                      "    PythonOption TracEnv /opt/trac/tools/%s" % env,
                      "    PythonOption TracUriRoot /tools/%s" % env,
                      "</Location>"]
        ab = _auth_block()
        if ab:
            L += ["", ab]
    content = "\n".join(L).rstrip() + "\n"
    step("Apache drop-in: %s" % dropin_path(svc))
    write_file(dropin_path(svc), content, 0o640) and _mark()


def _trac_envs():
    """Trac env names = subdirs of /opt/trac/tools containing conf/trac.ini."""
    base = OPT["trac_tools"][0]
    if not os.path.isdir(base):
        return []
    return sorted(d for d in os.listdir(base)
                  if os.path.exists(os.path.join(base, d, "conf", "trac.ini")))


def _auth_block():
    url    = ARGS.ldap_url    or _grep_conf("AuthLDAPURL")
    binddn = ARGS.ldap_binddn or _grep_conf("AuthLDAPBindDN")
    bindpw = ARGS.ldap_bindpw or _grep_conf("AuthLDAPBindPassword")
    if not (url and binddn and bindpw):
        warn("LDAP /login auth not configured (no creds found); pass --ldap-* to enable it.")
        return ""
    return ('# Apache LDAP auth so Trac sees REMOTE_USER at /login\n'
            '<LocationMatch "^/tools/[^/]+/login">\n'
            '    AuthType Basic\n'
            '    AuthBasicProvider ldap\n'
            '    AuthName "HUBzero Trac"\n'
            '    AuthLDAPURL ' + url + '\n'
            '    AuthLDAPBindDN ' + binddn + '\n'
            '    AuthLDAPBindPassword ' + bindpw + '\n'
            '    Require valid-user\n'
            '</LocationMatch>')


def _grep_conf(directive):
    for path in (os.path.join(ARGS.include_dir, "svn", "svn.conf"),
                 dropin_path("trac"),
                 os.path.join(ARGS.include_dir, "trac.conf")):
        if os.path.exists(path):
            try:
                with open(path) as fh:
                    for line in fh:
                        s = line.strip()
                        if s.startswith(directive + " ") or s.startswith(directive + "\t"):
                            return s[len(directive):].strip()
            except OSError:
                pass
    return None


# ---------------------------------------------------------------------------- #
# Validate / apply / smoke test
# ---------------------------------------------------------------------------- #
def _other_can_read(path):
    """Can a non-owner/non-group user (i.e. apache) read/traverse this path?"""
    import stat
    try:
        st = os.stat(path)
    except OSError:
        return True
    m = stat.S_IMODE(st.st_mode)
    if stat.S_ISDIR(st.st_mode):
        return bool(m & 0o004) and bool(m & 0o001)   # o+r and o+x
    return bool(m & 0o004)                            # o+r


def _pip_pkg_closure(roots):
    """Top-level installed paths of <roots> and their pip Requires closure."""
    seen, stack, paths = set(), list(roots), set()
    while stack:
        pkg = stack.pop()
        key = pkg.lower().replace("_", "-")
        if key in seen:
            continue
        seen.add(key)
        out = run(["pip2", "show", "-f", pkg], capture=True, check=False, mutating=False)
        if "Name:" not in out:
            continue                                  # not a pip package (skip rpm/unknown)
        loc, in_files = None, False
        for line in out.splitlines():
            if line.startswith("Location:"):
                loc = line.split(":", 1)[1].strip()
            elif line.startswith("Requires:"):
                stack += [x.strip() for x in line.split(":", 1)[1].split(",") if x.strip()]
            elif line.startswith("Files:"):
                in_files = True
            elif in_files and line[:1] in (" ", "\t"):
                f = line.strip()
                if f and not f.startswith(".."):
                    if loc:
                        paths.add(os.path.join(loc, f.split("/", 1)[0]))
    return paths


def fix_pip_perms():
    """If (and only if) apache can't import our pip-installed Trac stack because of
    the root-umask-0077 problem, chmod a+rX just those packages' files."""
    if ARGS.trac_handler != "mod_wsgi" or "trac" not in ARGS.services:
        return
    probe = subprocess.run(["sudo", "-u", "apache", "python2", "-c", "import trac"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                           universal_newlines=True)
    if probe.returncode == 0:
        return                                        # readable already -> nothing to do
    if "Permission denied" not in (probe.stderr or ""):
        warn("apache cannot import Trac, but not a permission problem -- left as-is")
        return
    step("apache can't read pip files (umask 0077) -- fixing only the Trac/mod_wsgi packages")
    targets = [p for p in _pip_pkg_closure(["Trac", "mod_wsgi"]) if not _other_can_read(p)]
    for path in sorted(targets):
        run(["chmod", "-R", "a+rX", path])
    if not targets:
        warn("no unreadable pip paths found for Trac/mod_wsgi; the import error is elsewhere")


def _ondisk_module(key):
    """Is an interpreter module (key='wsgi_module'/'python_module') enabled on disk?"""
    d = "/etc/httpd/conf.modules.d"
    for f in os.listdir(d):
        if f.endswith(".conf"):
            try:
                with open(os.path.join(d, f)) as fh:
                    if ("LoadModule " + key) in fh.read():
                        return True
            except OSError:
                pass
    return False


def apply_changes():
    step("Validate and apply")
    # Full restart iff the RUNNING interpreter set differs from the on-disk
    # desired set (graceful reload can't swap an embedded interpreter). This is
    # uniform for install / uninstall / repair / handler-switch.
    need_restart = (running_has_so("mod_wsgi") != _ondisk_module("wsgi_module")
                    or running_has_so("mod_python") != _ondisk_module("python_module"))
    if CTX.dry:
        log("[dry-run] apachectl configtest")
        log("[dry-run] would " + ("restart httpd" if need_restart else
            ("reload httpd" if CTX.config_changed else "do nothing (no changes)")))
        return
    out = run(["apachectl", "configtest"], capture=True, check=False, mutating=False)
    print("    " + out.strip().replace("\n", "\n    "))
    if "Syntax OK" not in out:
        die("configtest failed -- running server untouched. Fix and re-run.")
    if ARGS.no_restart:
        warn("--no-restart: staged but not applied; %s httpd to activate."
             % ("restart" if need_restart else "reload"))
        return
    if need_restart:
        log("interpreter module set changed -> full restart")
        run(["systemctl", "restart", "httpd"])
    elif CTX.config_changed:
        log("config changed, module set stable -> graceful reload")
        run(["systemctl", "reload", "httpd"])
    else:
        log("nothing changed.")
    active = subprocess.run(["systemctl", "is-active", "httpd"], stdout=subprocess.PIPE,
                            universal_newlines=True).stdout.strip()
    log("httpd: " + active)
    if active != "active":
        die("httpd not active after apply -- check journalctl -u httpd and error_log.")


def smoke_test():
    if CTX.dry or ARGS.no_restart:
        return
    step("Smoke test")
    ip, fqdn = _vhost_target()
    if not (ip and fqdn):
        warn("could not detect listen IP / ServerName; skipping smoke test"); return
    env = ARGS.test_env
    checks = []
    if "trac" in ARGS.services:
        checks += [("trac wiki", "/tools/%s/wiki" % env),
                   ("trac browser", "/tools/%s/browser" % env),
                   ("cms (bare)", "/tools/%s" % env)]
    if "svn" in ARGS.services:
        checks += [("svn", "/tools/%s/svn" % env)]
    for label, path in checks:
        code = subprocess.run(
            ["curl", "-k", "-s", "--resolve", "%s:443:%s" % (fqdn, ip),
             "-o", "/dev/null", "-w", "%{http_code}", "https://%s%s" % (fqdn, path)],
            stdout=subprocess.PIPE, universal_newlines=True).stdout.strip()
        log("%-12s %s -> HTTP %s" % (label, path, code))


def _vhost_target():
    path = "/etc/httpd/sites.d/%s-ssl.conf" % ARGS.hub
    if not os.path.exists(path):
        return None, None
    with open(path) as fh:
        t = fh.read()
    ip = re.search(r"^\s*Listen\s+(\S+):443", t, re.M)
    sn = re.search(r"^\s*ServerName\s+(\S+)", t, re.M)
    return (ip.group(1) if ip else None), (sn.group(1) if sn else None)


def detect_hub():
    sites = "/etc/httpd/sites.d"
    if os.path.isdir(sites):
        for f in sorted(os.listdir(sites)):
            if f.endswith("-ssl.conf"):
                with open(os.path.join(sites, f)) as fh:
                    m = re.search(r"IncludeOptional\s+(\w[\w.-]*)\.conf\.d/", fh.read())
                if m:
                    return m.group(1)
    return None


# ---------------------------------------------------------------------------- #
# Introspection / uninstall / doctor / repair
# ---------------------------------------------------------------------------- #
def _remove_file(path):
    if not os.path.exists(path):
        return
    if CTX.dry:
        log("[dry-run] rm " + path)
    else:
        os.remove(path); log("rm " + path)
    _mark()


def detect_configured_services():
    """Which services are wired now: one 00-forge-<svc>.conf each (+ a legacy trac.conf)."""
    found = [s for s in ALL_SERVICES if os.path.exists(dropin_path(s))]
    if "trac" not in found and os.path.exists(os.path.join(ARGS.include_dir, "trac.conf")):
        found.append("trac")
    return found


def detect_handler():
    """Trac handler currently wired (from the trac drop-in or a legacy trac.conf), else None."""
    for p in (dropin_path("trac"), os.path.join(ARGS.include_dir, "trac.conf")):
        if os.path.exists(p):
            with open(p) as fh:
                t = fh.read()
            if "modpython_frontend" in t:   return "mod_python"
            if "WSGIScriptAliasMatch" in t: return "mod_wsgi"
    return None


def _disable_legacy_trac():
    """Move a standalone <hub>.conf.d/trac.conf aside (replaced by the trac drop-in)."""
    legacy = os.path.join(ARGS.include_dir, "trac.conf")
    if os.path.exists(legacy):
        warn("disabling standalone %s (replaced by %s)" % (legacy, dropin_path("trac")))
        _rename(legacy, legacy + ".disabled")


def do_install():
    s = ARGS.services
    if "svn" in s:         setup_svn()
    if "git" in s:         setup_git()
    if "gitExternal" in s: setup_gitexternal()
    if "trac" in s:        setup_trac()
    if "trac" in s:        _disable_legacy_trac()   # fold any standalone trac.conf in
    fix_pip_perms()
    write_dropin(s)
    apply_changes()
    smoke_test()


def uninstall(remove):
    if not remove:
        die("uninstall needs at least one service (e.g. 'hzforge uninstall git')")
    configured = detect_configured_services()
    handler = detect_handler() or ARGS.trac_handler
    ARGS.trac_handler = handler
    remaining = [x for x in configured if x not in remove]
    step("Uninstall %s   (configured=%s -> remaining=%s)"
         % (",".join(remove), ",".join(configured) or "-", ",".join(remaining) or "-"))
    ARGS.services = remaining
    for svc in remove:
        _remove_file(dropin_path(svc))      # each service is its own file
    if "trac" in remove:
        _disable_legacy_trac()
    if "trac" in remove and "trac" not in remaining:
        (_ensure_modpython_unloaded if handler == "mod_python" else _ensure_modwsgi_unloaded)()
    if ARGS.purge:
        _purge(remove, handler)
    apply_changes()
    log("Repository DATA under /opt/<svc>/tools (svn/git repos, Trac envs) LEFT INTACT.")


def _purge(remove, handler):
    step("Purge packages & script artifacts  (repository DATA under /opt is NOT touched)")
    if "trac" in remove:
        _remove_file(SHIM_PATH)
        if os.path.isdir(EGG_CACHE):
            run(["rm", "-rf", EGG_CACHE], check=False)
        for mc in (MODCONF_WSGI, MODCONF_PY):
            _remove_file(mc); _remove_file(mc + ".disabled")
        run(["dnf", "-y", "remove", "hubzero-trac", "hubzero-trac-mysqlauthz"], check=False)
        run(["sh", "-c", "pip2 uninstall -y mod_wsgi Trac"], check=False)
    if "svn" in remove:
        run(["dnf", "-y", "remove", "mod_dav_svn"], check=False)
        _remove_file(WANDISCO_REPO_PATH)
    warn("purge kept subversion/git/subversion-python and ALL /opt data; remove by hand if intended.")


def _chk(results, level, msg):
    results.append((level, msg))
    print("    %-7s %s" % ({"INFO": "[info]", "OK": "[ ok ]",
                            "WARN": "[warn]", "FAIL": "[FAIL]"}[level], msg))


def doctor():
    step("Doctor -- hub=%s" % ARGS.hub)
    r = []
    inc = ARGS.include_dir
    services = detect_configured_services()
    target = ARGS.services or services           # positional services = explicit scope
    handler = detect_handler()
    legacy = os.path.join(inc, "trac.conf")
    if os.path.exists(legacy):
        _chk(r, "WARN", "standalone %s present -> consolidate via 'install' (avoid duplicate WSGI)" % legacy)
    _chk(r, "INFO", "configured services: %s" % (",".join(services) or "(none)"))
    if ARGS.services:
        _chk(r, "INFO", "scope: %s" % " ".join(target))
        for s in ARGS.services:
            if s not in services:
                _chk(r, "WARN", "'%s' requested but not currently configured" % s)
    for svc in target:
        p = dropin_path(svc)
        present = os.path.exists(p) or (svc == "trac" and os.path.exists(legacy))
        _chk(r, "OK" if present else "WARN",
             "%s drop-in %s" % (svc, "present" if present else "%s ABSENT (run install)" % p))
    if "trac" in target:
        _chk(r, "INFO", "trac handler: %s" % (handler or "n/a"))

    wsgi_disk, py_disk = _ondisk_module("wsgi_module"), _ondisk_module("python_module")
    wsgi_run, py_run = running_has_so("mod_wsgi"), running_has_so("mod_python")
    ld = lambda b: "loaded" if b else "not loaded"
    if wsgi_disk and py_disk:
        _chk(r, "FAIL", "both mod_wsgi and mod_python enabled on disk -> httpd won't start (repair)")
    for name, run, disk in (("mod_wsgi", wsgi_run, wsgi_disk),
                            ("mod_python", py_run, py_disk)):
        if run == disk:
            _chk(r, "OK", "%s: %s (running matches disk)" % (name, ld(disk)))
        else:
            _chk(r, "WARN", "%s: on-disk %s but running %s -- restart pending (repair)"
                 % (name, ld(disk), ld(run)))

    out = subprocess.run(["apachectl", "configtest"], stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, universal_newlines=True).stdout or ""
    last = (out.strip().splitlines() or ["(no output)"])[-1]
    _chk(r, "OK" if "Syntax OK" in out else "FAIL", "apachectl configtest: " + last)

    if "trac" in target:
        if handler == "mod_wsgi":
            _chk(r, "OK" if os.path.exists(SHIM_PATH) else "FAIL",
                 "shim %s" % ("present" if os.path.exists(SHIM_PATH) else "MISSING (repair)"))
        ti = subprocess.run(["sudo", "-u", "apache", "python2", "-c", "import trac"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, universal_newlines=True)
        if ti.returncode == 0:
            _chk(r, "OK", "apache can import Trac")
        else:
            perm = "Permission denied" in (ti.stderr or "")
            _chk(r, "FAIL", "apache cannot import Trac%s (repair: %s)"
                 % (" [umask perms]" if perm else "", "fix-perms" if perm else "reinstall Trac"))
        # Trac's repo browser is optional -- only relevant when the svn service is
        # configured (it pulls subversion-python). Trac-without-svn is a valid config.
        if "svn" in services:
            sc = subprocess.run(["sudo", "-u", "apache", "python2", "-c", "import svn.core"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            _chk(r, "OK" if sc.returncode == 0 else "WARN",
                 "Trac repo browser (svn.core) " + ("available" if sc.returncode == 0
                 else "unavailable -> install subversion-python"))
    if "svn" in target:
        _chk(r, "OK" if rpm_installed("mod_dav_svn") else "FAIL",
             "mod_dav_svn " + ("installed" if rpm_installed("mod_dav_svn") else "MISSING"))
        f = os.path.join(inc, "svn", "svn.conf")
        _chk(r, "OK" if os.path.exists(f) else "WARN",
             "per-tool %s %s" % (f, "present" if os.path.exists(f) else "absent (generated externally)"))
    if "git" in target or "gitExternal" in target:
        _chk(r, "OK" if rpm_installed("git") else "FAIL",
             "git " + ("installed" if rpm_installed("git") else "MISSING"))
        for svc, fn in (("git", "git.conf"), ("gitExternal", "gitExternal.conf")):
            if svc in target:
                f = os.path.join(inc, "git", fn)
                _chk(r, "OK" if os.path.exists(f) else "WARN",
                     "per-tool %s %s" % (f, "present" if os.path.exists(f) else "absent (generated externally)"))
    for key, svc in (("trac_tools", "trac"), ("svn_tools", "svn"),
                     ("git_tools", "git"), ("gext_tools", "gitExternal")):
        if svc in target:
            path = OPT[key][0]
            _chk(r, "OK" if os.path.isdir(path) else "WARN",
                 "%s %s" % (path, "exists" if os.path.isdir(path) else "MISSING (repair)"))

    fails = sum(1 for lvl, _ in r if lvl == "FAIL")
    warns = sum(1 for lvl, _ in r if lvl == "WARN")
    step("Doctor: %d FAIL, %d WARN, %d OK" % (fails, warns, sum(1 for l, _ in r if l == "OK")))
    if fails or warns:
        log("run 'repair' to fix the repairable items.")
    return fails == 0


def repair():
    configured = detect_configured_services()
    handler = detect_handler() or ARGS.trac_handler
    requested = list(ARGS.services)              # [] means "all configured"
    scope = requested or configured             # what to diagnose
    target = [s for s in scope if s in configured]   # what we can actually re-assert
    ARGS.trac_handler = handler
    ARGS.services = scope                        # keep doctor() scoped to the request
    step("Repair -- scope=%s, re-assert=%s (configured: %s)"
         % (",".join(scope) or "all", ",".join(target) or "none", ",".join(configured) or "none"))
    for s in requested:
        if s not in configured:
            warn("'%s' is not configured -> nothing to repair (use 'install %s')" % (s, s))
    doctor()                                     # isolated to scope (+ global checks)
    if not target:
        step("Nothing to re-assert for the requested scope.")
        return
    if "svn" in target:         setup_svn()
    if "git" in target:         setup_git()
    if "gitExternal" in target: setup_gitexternal()
    if "trac" in target:        setup_trac()
    fix_pip_perms()
    write_dropin(target)                         # per-service: only the targeted files
    apply_changes()
    smoke_test()
    step("Re-check after repair")
    doctor()


# ---------------------------------------------------------------------------- #
def build_parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--hub")
    common.add_argument("--test-env", default="histogram", dest="test_env")
    common.add_argument("--no-restart", action="store_true")
    common.add_argument("--dry-run", action="store_true", dest="dry")

    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command")

    svc_help = "services (" + " ".join(ALL_SERVICES) + ")"

    pi = sub.add_parser("install", parents=[common], help="install/wire services")
    pi.add_argument("services", nargs="*", metavar="SERVICE",
                    help=svc_help + "; default: all")
    pi.add_argument("--trac-handler", choices=["mod_wsgi", "mod_python"],
                    default="mod_wsgi", dest="trac_handler")
    pi.add_argument("--svn-source", choices=["wandisco", "appstream"],
                    default="wandisco", dest="svn_source")
    pi.add_argument("--trac-spec", default=TRAC_SPEC, dest="trac_spec")
    pi.add_argument("--modwsgi-spec", default=MODWSGI_SPEC, dest="modwsgi_spec")
    pi.add_argument("--ldap-url", dest="ldap_url")
    pi.add_argument("--ldap-binddn", dest="ldap_binddn")
    pi.add_argument("--ldap-bindpw", dest="ldap_bindpw")
    pi.add_argument("--force-pip", action="store_true")

    pu = sub.add_parser("uninstall", parents=[common], help="remove services (data preserved)")
    pu.add_argument("services", nargs="*", metavar="SERVICE", help=svc_help + " to remove")
    pu.add_argument("--purge", action="store_true",
                    help="also remove packages/repo-file/shim (NEVER /opt repo data)")

    pd = sub.add_parser("doctor", parents=[common], help="diagnose (read-only); exit 1 if any FAIL")
    pd.add_argument("services", nargs="*", metavar="SERVICE",
                    help=svc_help + " to check; default: all configured")
    prp = sub.add_parser("repair", parents=[common], help="diagnose then fix drift")
    prp.add_argument("services", nargs="*", metavar="SERVICE",
                     help=svc_help + " to re-assert; default: all configured")
    return p


def main():
    global CTX, ARGS
    parser = build_parser()
    args = parser.parse_args()
    if not getattr(args, "command", None):
        parser.print_help()                # bare `hzforge` or `--help` => top-level help
        sys.exit(0)

    # fill attrs that only exist on some subparsers
    for attr, default in (("trac_handler", "mod_wsgi"), ("svn_source", "wandisco"),
                          ("trac_spec", TRAC_SPEC), ("modwsgi_spec", MODWSGI_SPEC),
                          ("ldap_url", None), ("ldap_binddn", None), ("ldap_bindpw", None),
                          ("force_pip", False), ("purge", False)):
        if not hasattr(args, attr):
            setattr(args, attr, default)

    CTX = Ctx(args.dry)
    if os.geteuid() != 0:
        die("must run as root (sudo).")
    args.hub = args.hub or detect_hub() or "help"
    args.include_dir = "/etc/httpd/%s.conf.d" % args.hub
    # services come in as bare positional args (a list); allow comma-joined too
    services = []
    for tok in (args.services or []):
        services += [s for s in tok.split(",") if s]
    bad = [s for s in services if s not in ALL_SERVICES]
    if bad:
        die("unknown service(s): %s (valid: %s)" % (", ".join(bad), ", ".join(ALL_SERVICES)))
    if args.command == "install" and not services:
        services = list(ALL_SERVICES)                  # install with no args = all
    if args.command == "uninstall" and not services:
        die("uninstall needs at least one service: " + " ".join(ALL_SERVICES))
    args.services = services
    ARGS = args

    print("=" * 72)
    print(" HUBzero Forge services [%s]  hub=%s%s"
          % (args.command, args.hub, "  DRY-RUN" if args.dry else ""))
    print("=" * 72)
    if not os.path.isdir(args.include_dir):
        die("include dir %s missing (right --hub?)" % args.include_dir)

    if args.command == "install":
        do_install()
    elif args.command == "uninstall":
        uninstall(services)
    elif args.command == "doctor":
        ok = doctor()
        sys.exit(0 if ok else 1)
    elif args.command == "repair":
        repair()

    step("Done")
    for n in CTX.notes:
        print("[!] " + n)
    log("Config: %s/%s<svc>.conf (one per service). Independent of m4/hzcms."
        % (args.include_dir, DROPIN_PREFIX))


if __name__ == "__main__":
    main()
