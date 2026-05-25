#!/usr/bin/env python3
"""
hzforge -- install / uninstall / doctor / repair HUBzero Forge services
(svn, git, gitExternal, trac) as Apache drop-ins, independent of the m4
vhost template.

It creates the per-service dirs and the hzsvn/hzgit groups, installs the
required packages, and writes the Apache config as one drop-in per service
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
  hzforge uninstall git                      # stop serving git (packages/data kept)
  hzforge doctor                             # diagnose all (or: hzforge doctor git)
  hzforge repair                             # fix drift (or: hzforge repair trac)
  hzforge test                               # throwaway project per configured service, verify, remove
  hzforge test svn git                       # only the named services
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

TOOLS_DIR    = OPT["trac_tools"][0]   # /opt/trac/tools
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


def ensure_group(group):
    """Create the group if missing. Forge chgrp's repos to it (hzgit for git,
    hzsvn for svn); group *membership* (apache, apps) is provisioned by the forge
    setup, not here."""
    if not group_exists(group):
        run(["groupadd", group])


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


def _svn_module_stream():
    """The currently-enabled subversion module stream, or None."""
    out = subprocess.run(["dnf", "-q", "module", "list", "--enabled", "subversion"],
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                         universal_newlines=True).stdout or ""
    m = re.search(r"^subversion\s+(\S+)", out, re.M)
    return m.group(1) if m else None


def _enable_svn_source():
    """Make subversion-* installable (needed even for trac: hubzero-trac rpm-requires
    subversion-devel). Does NOT install anything itself."""
    if ARGS.svn_source == "wandisco":
        ensure_wandisco_repo()
        # Only reset if a module stream is actually enabled; with module_hotfixes=1
        # the non-modular wandisco packages then supersede it. (No stream -> nothing
        # to reset.)
        if _svn_module_stream():
            run(["dnf", "-y", "module", "reset", "subversion"], check=False)
        else:
            log("subversion module: no stream enabled -> no reset needed")
    elif _svn_module_stream() != "1.10":
        run(["dnf", "-y", "module", "enable", "subversion:1.10"], check=False)
    else:
        log("subversion module: 1.10 already enabled")


_SVN_PKGS_DONE = False

def ensure_subversion_packages():
    """svn SERVICE package set: subversion + mod_dav_svn (chosen source) + the
    subversion-python SWIG bindings (from hubzero). Installing these also lights up
    Trac's repo browser."""
    global _SVN_PKGS_DONE
    if _SVN_PKGS_DONE:
        return
    _SVN_PKGS_DONE = True
    need = [p for p in ("subversion", "mod_dav_svn", "subversion-python")
            if not rpm_installed(p)]
    if not need:
        log("subversion packages already installed")
        return
    _enable_svn_source()                            # only when something's missing
    svc = [p for p in ("subversion", "mod_dav_svn") if p in need]
    if svc:                                         # subversion + mod_dav_svn from chosen source
        dnf_install(svc, enablerepo=_svn_repo())
    if "subversion-python" in need:                # Py2 SWIG bindings, always from hubzero
        dnf_install(["subversion-python"])


# ---------------------------------------------------------------------------- #
# Subsystem setup (dirs / groups / packages)
# ---------------------------------------------------------------------------- #
def setup_svn():
    step("Subversion (DAV) -- packages, group, dirs")
    ensure_subversion_packages()
    ensure_group("hzsvn")
    makedir(*OPT["svn"]); makedir(*OPT["svn_tools"])
    makedir(os.path.join(ARGS.include_dir, "svn"), 0o700)


def setup_git():
    step("Git (local) -- package, group, dirs")
    if not rpm_installed("git"):
        dnf_install(["git"])
    ensure_group("hzgit")
    makedir(*OPT["git"]); makedir(*OPT["git_tools"])
    makedir(os.path.join(ARGS.include_dir, "git"), 0o700)


def setup_gitexternal():
    step("Git (external) -- group, dirs")
    if not rpm_installed("git"):
        dnf_install(["git"])
    ensure_group("hzgit")
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
    try:
        out = subprocess.run(["mod_wsgi-express", "module-config"],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             universal_newlines=True).stdout or ""
    except FileNotFoundError:
        return ""                       # mod_wsgi not pip-installed yet (fresh host)
    m = re.search(r'LoadModule\s+wsgi_module\s+"([^"]+)"', out)
    return m.group(1) if m else ""


def _ensure_modwsgi_loaded():
    try:
        out = subprocess.run(["mod_wsgi-express", "module-config"], stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, universal_newlines=True).stdout or ""
    except FileNotFoundError:
        out = ""                        # not pip-installed yet (e.g. dry-run on a fresh host)
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
         "# Self-contained drop-in (vhost scope); independent of the m4 vhost template.",
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
    probe = subprocess.run(["runuser", "-u", "apache", "--", "python2", "-c", "import trac"],
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


def _systemd():
    return os.path.isdir("/run/systemd/system")


def _httpd_running():
    return subprocess.run(["pgrep", "-x", "httpd"], stdout=subprocess.DEVNULL).returncode == 0


def ensure_httpd_runtime_dir():
    """httpd needs /run/httpd (pid, lock, scoreboard socket).  On systemd hosts
    systemd-tmpfiles (tmpfiles.d/httpd.conf) and the unit's RuntimeDirectory=
    create it; without systemd -- a container/chroot, or simply before tmpfiles
    has run -- it's absent and `httpd -k start` dies with 'AH00015: Unable to
    open logs' / 'no listening sockets'.  Create it the way tmpfiles.d does
    (0710 root:apache).  makedir() is idempotent, so this is a no-op when present."""
    makedir("/run/httpd", 0o710, owner="root", group="apache")


def apache_apply(restart):
    """Restart/reload httpd via systemd when it's the init, else drive the httpd
    binary directly with -k (so the same code path works in a container/chroot
    without systemd).  We bypass `apachectl start|restart|graceful` here because
    EL8's apachectl delegates those verbs to systemctl unconditionally, which
    fails when systemd isn't running; `httpd -k <verb>` talks to httpd directly."""
    if _systemd():
        run(["systemctl", "restart" if restart else "reload", "httpd"])
        return
    ensure_httpd_runtime_dir()       # systemd-tmpfiles isn't around to make it
    if not _httpd_running():
        run(["httpd", "-k", "start"])
    else:
        run(["httpd", "-k", "restart" if restart else "graceful"])


def apache_active():
    if _systemd():
        return subprocess.run(["systemctl", "is-active", "httpd"], stdout=subprocess.PIPE,
                              universal_newlines=True).stdout.strip()
    return "active" if _httpd_running() else "inactive"


def apply_changes():
    step("Validate and apply")
    # Full restart iff the RUNNING interpreter set differs from the on-disk
    # desired set (graceful reload can't swap an embedded interpreter). This is
    # uniform for install / uninstall / repair / handler-switch.
    need_restart = (running_has_so("mod_wsgi") != _ondisk_module("wsgi_module")
                    or running_has_so("mod_python") != _ondisk_module("python_module"))
    if CTX.dry:
        log("[dry-run] apachectl configtest")
        if not _systemd() and not os.path.isdir("/run/httpd"):
            log("[dry-run] mkdir /run/httpd (0710 root:apache) -- no systemd-tmpfiles")
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
        apache_apply(restart=True)
    elif CTX.config_changed:
        log("config changed, module set stable -> graceful reload")
        apache_apply(restart=False)
    else:
        log("nothing changed.")
    active = apache_active()
    log("httpd: " + active)
    if active != "active":
        die("httpd not active after apply -- check the error log / 'journalctl -u httpd'.")


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
# test -- create throwaway backing resources per service and verify they serve
# ---------------------------------------------------------------------------- #
def _vhost_target():
    """Detect how to reach the hub's vhost: {ip, port, host, scheme} or None."""
    path = "/etc/httpd/sites.d/%s-ssl.conf" % ARGS.hub
    if not os.path.exists(path):
        return None
    t = open(path).read()
    m = re.search(r"^\s*Listen\s+(?:(\S+):)?(\d+)", t, re.M)
    if not m:
        return None
    sn = re.search(r"^\s*ServerName\s+(\S+)", t, re.M)
    ip = m.group(1) or "127.0.0.1"
    port = m.group(2)
    return {"ip": ip, "port": port,
            "host": sn.group(1) if sn else ip,
            "scheme": "https" if port == "443" else "http"}


def _curl(tgt, path):
    """GET tgt+path (pinning host->ip), return (http_code, body)."""
    fd, tmp = tempfile.mkstemp()
    os.close(fd)
    try:
        code = subprocess.run(
            ["curl", "-s", "-k", "--resolve", "%s:%s:%s" % (tgt["host"], tgt["port"], tgt["ip"]),
             "-o", tmp, "-w", "%{http_code}",
             "%s://%s%s" % (tgt["scheme"], tgt["host"], path)],
            stdout=subprocess.PIPE, universal_newlines=True).stdout.strip()
        with open(tmp, errors="replace") as fh:
            return code, fh.read()
    finally:
        os.unlink(tmp)


TESTABLE = ["trac", "svn", "git", "gitExternal"]


def _selftest_conf_path():
    return os.path.join(ARGS.include_dir, DROPIN_PREFIX + "selftest.conf")


def _reload_for_test(why):
    """configtest then graceful reload -- a self-test never changes the module set."""
    out = run(["apachectl", "configtest"], capture=True, check=False, mutating=False)
    if "Syntax OK" not in out:
        print("    " + out.strip().replace("\n", "\n    "))
        die("self-test route failed configtest (%s) -- running server untouched" % why)
    apache_apply(restart=False)


def _svn_route(name, repo):
    return ['<Location /tools/%s/svn>' % name,
            '    DAV svn',
            '    SVNPath %s' % repo,
            '    Require all granted',          # throwaway repo, removed right after
            '</Location>', '']


def _git_route(svc, name, root, repo):
    # http-backend route for the throwaway repo, mirroring the MySQL-generated conf
    return ['SetEnvIf Request_URI "^/tools/%s/%s/%s/" GIT_PROJECT_ROOT=%s' % (name, svc, name, root),
            'SetEnv GIT_HTTP_EXPORT_ALL',
            'ScriptAliasMatch "^/tools/%s/%s/%s/(.*)$" '
            '/usr/libexec/git-core/git-http-backend/%s.git/$1' % (name, svc, name, name),
            '<LocationMatch "^/tools/%s/%s">' % (name, svc),
            '    Require all granted',          # anonymous read for the throwaway repo
            '</LocationMatch>', '']


def _check_trac(tgt, name):
    code, body = _curl(tgt, "/tools/%s/wiki" % name)
    ok = code == "200" and ("Trac" in body or "Wiki" in body or "powered by" in body.lower())
    log("trac:        GET /tools/%s/wiki -> HTTP %s%s" % (name, code, "  (Trac)" if ok else ""))
    return ok


def _check_svn(tgt, name):
    code, body = _curl(tgt, "/tools/%s/svn/" % name)
    ok = code == "200" and ("subversion" in body.lower() or "Revision 0" in body)
    log("svn:         GET /tools/%s/svn/ -> HTTP %s%s" % (name, code, "  (mod_dav_svn)" if ok else ""))
    return ok


def _check_git(tgt, svc, name):
    p = "/tools/%s/%s/%s/info/refs?service=git-upload-pack" % (name, svc, name)
    code, body = _curl(tgt, p)
    ok = code == "200" and "service=git-upload-pack" in body
    log("%-12s GET /tools/%s/%s/%s/info/refs -> HTTP %s%s"
        % (svc + ":", name, svc, name, code, "  (git-http-backend)" if ok else ""))
    return ok


def cmd_test():
    """Create throwaway, uniquely-named backing resources for each requested service,
    verify each actually serves over HTTP, then remove them.  Self-contained: needs no
    MySQL/forge provisioning.  trac is served by hzforge's generic WSGI route, so it needs
    no config change; svn/git need a per-repo route, so a temporary self-test drop-in is
    added and removed around the checks (graceful reload)."""
    configured = detect_configured_services()
    for s in (ARGS.services or []):
        if s not in configured:
            die("'%s' is not configured here -- run 'install %s' first" % (s, s))
    targets = [s for s in (ARGS.services or [t for t in TESTABLE if t in configured])
               if s in TESTABLE]
    if not targets:
        die("nothing testable here (configured: %s) -- install a service first"
            % (",".join(configured) or "none"))
    tgt = _vhost_target()
    if not tgt:
        die("could not detect the hub vhost (/etc/httpd/sites.d/%s-ssl.conf)" % ARGS.hub)
    if "trac" in targets and (detect_handler() or ARGS.trac_handler) != "mod_wsgi":
        warn("trac self-test supports the mod_wsgi handler only -- skipping trac")
        targets = [s for s in targets if s != "trac"]
    if not targets:
        die("nothing to test after skips")

    name = "hzforge-selftest-%d" % os.getpid()
    step("Self-test: %s -- throwaway name '%s' via %s://%s"
         % (",".join(targets), name, tgt["scheme"], tgt["host"]))
    if CTX.dry:
        log("[dry-run] would create throwaway %s, curl each, then remove" % ",".join(targets))
        return

    conf = _selftest_conf_path()
    if os.path.exists(conf):
        die("stale self-test conf present: %s (remove it and retry)" % conf)
    created, results = [], {}
    route_lines = ["# hzforge self-test routes -- throwaway, removed automatically",
                   "RewriteEngine On", ""]
    route_svcs = [s for s in targets if s != "trac"]
    try:
        if "trac" in targets:
            env = os.path.join(OPT["trac_tools"][0], name)
            if os.path.exists(env):
                die("test path already exists: %s" % env)
            run(["trac-admin", env, "initenv", name, "sqlite:db/trac.db"], capture=True)
            run(["chown", "-R", "apache:apache", env]); created.append(env)
        if "svn" in targets:
            repo = os.path.join(OPT["svn_tools"][0], name)
            if os.path.exists(repo):
                die("test path already exists: %s" % repo)
            run(["svnadmin", "create", repo], capture=True)
            run(["chown", "-R", "apache:apache", repo]); created.append(repo)
            route_lines += _svn_route(name, repo)
        for gsvc, key in (("git", "git_tools"), ("gitExternal", "gext_tools")):
            if gsvc in targets:
                root = OPT[key][0]
                repo = os.path.join(root, name + ".git")
                if os.path.exists(repo):
                    die("test path already exists: %s" % repo)
                run(["git", "init", "--bare", "-q", repo], capture=True)
                run(["chown", "-R", "apache:apache", repo]); created.append(repo)
                route_lines += _git_route(gsvc, name, root, repo)
        if route_svcs:
            write_file(conf, "\n".join(route_lines).rstrip() + "\n", 0o640)
            _reload_for_test("add self-test routes")
        if "trac" in targets:        results["trac"] = _check_trac(tgt, name)
        if "svn" in targets:         results["svn"] = _check_svn(tgt, name)
        if "git" in targets:         results["git"] = _check_git(tgt, "git", name)
        if "gitExternal" in targets: results["gitExternal"] = _check_git(tgt, "gitExternal", name)
    finally:
        for p in created:
            if os.path.isdir(p):
                run(["rm", "-rf", p], check=False)
        if os.path.exists(conf):
            _remove_file(conf)
            _reload_for_test("remove self-test routes")
        log("removed throwaway resources")

    failed = [s for s in targets if results.get(s) is False]
    if failed:
        die("self-test FAILED: %s" % ", ".join(failed))
    step("Self-test PASSED: %s" % ", ".join(targets))


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
    # verify the just-installed services actually serve, unless suppressed.
    # mod_python Trac has no self-test, so drop it from the testable set.
    testable = [x for x in s if x in TESTABLE]
    if "trac" in testable and ARGS.trac_handler != "mod_wsgi":
        testable = [x for x in testable if x != "trac"]
    if testable and not ARGS.no_test and not ARGS.no_restart and not CTX.dry:
        cmd_test()


def uninstall(remove):
    if not remove:
        die("uninstall needs at least one service (e.g. 'hzforge uninstall git')")
    configured = detect_configured_services()
    handler = detect_handler() or ARGS.trac_handler
    ARGS.trac_handler = handler
    actual = [s for s in remove if s in configured]      # only what's actually wired
    for s in remove:
        if s not in configured:
            warn("'%s' is not configured here -> nothing to uninstall" % s)
    if not actual:
        step("Uninstall: none of %s are configured (configured=%s) -- nothing to do"
             % (",".join(remove), ",".join(configured) or "-"))
        return                              # leave the running server untouched
    remaining = [x for x in configured if x not in actual]
    step("Uninstall %s   (configured=%s -> remaining=%s)"
         % (",".join(actual), ",".join(configured) or "-", ",".join(remaining) or "-"))
    ARGS.services = remaining
    for svc in actual:
        _remove_file(dropin_path(svc))      # each service is its own file
    if "trac" in actual:                    # trac is a single service -> fully removed
        _disable_legacy_trac()
        (_ensure_modpython_unloaded if handler == "mod_python" else _ensure_modwsgi_unloaded)()
        _remove_file(SHIM_PATH)             # hzforge's own helper files for trac
        if os.path.isdir(EGG_CACHE):
            run(["rm", "-rf", EGG_CACHE], check=False)
    if "svn" in actual:
        _remove_file(WANDISCO_REPO_PATH)    # hzforge wrote this repo file
    apply_changes()
    log("Packages, the hzsvn/hzgit groups, and all /opt repo data were left intact.")


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
            _chk(r, "OK", "%s: %s" % (name, ld(disk)))
        else:
            _chk(r, "WARN", "%s: on-disk %s but running %s -- restart pending (repair)"
                 % (name, ld(disk), ld(run)))

    out = subprocess.run(["apachectl", "configtest"], stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, universal_newlines=True).stdout or ""
    last = (out.strip().splitlines() or ["(no output)"])[-1]
    _chk(r, "OK" if "Syntax OK" in out else "FAIL", "apachectl configtest: " + last)

    # Service control + runtime state.  On EL8 `apachectl start/restart/graceful`
    # always defers to systemctl, so hzforge uses systemd when it's the init and
    # otherwise drives `httpd -k` directly -- which needs /run/httpd, normally made
    # by systemd-tmpfiles.  Surface both, plus whether httpd is actually up.
    if _systemd():
        _chk(r, "INFO", "service control: systemd (systemctl restart/reload httpd)")
    else:
        _chk(r, "INFO", "service control: httpd -k (no systemd; apachectl would defer to systemctl)")
        rd = os.path.isdir("/run/httpd")
        _chk(r, "OK" if rd else "WARN",
             "/run/httpd " + ("present" if rd else "MISSING -> httpd -k start would fail (repair)"))
    active = apache_active()
    _chk(r, "OK" if active == "active" else "WARN", "httpd: " + active)

    if "trac" in target:
        if handler == "mod_wsgi":
            _chk(r, "OK" if os.path.exists(SHIM_PATH) else "FAIL",
                 "shim %s" % ("present" if os.path.exists(SHIM_PATH) else "MISSING (repair)"))
        ti = subprocess.run(["runuser", "-u", "apache", "--", "python2", "-c", "import trac"],
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
            sc = subprocess.run(["runuser", "-u", "apache", "--", "python2", "-c", "import svn.core"],
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
    step("Re-check after repair")
    doctor()


# ---------------------------------------------------------------------------- #
def build_parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--hub")
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
    pi.add_argument("--no-test", action="store_true",
                    help="skip the post-install Trac self-test")

    pu = sub.add_parser("uninstall", parents=[common], help="remove services (packages/data preserved)")
    pu.add_argument("services", nargs="*", metavar="SERVICE", help=svc_help + " to remove")

    pd = sub.add_parser("doctor", parents=[common], help="diagnose (read-only); exit 1 if any FAIL")
    pd.add_argument("services", nargs="*", metavar="SERVICE",
                    help=svc_help + " to check; default: all configured")
    prp = sub.add_parser("repair", parents=[common], help="diagnose then fix drift")
    prp.add_argument("services", nargs="*", metavar="SERVICE",
                     help=svc_help + " to re-assert; default: all configured")
    pt = sub.add_parser("test", parents=[common],
                        help="create throwaway projects and verify each service serves")
    pt.add_argument("services", nargs="*", metavar="SERVICE",
                    help=svc_help + " to test; default: all configured")
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
                          ("force_pip", False), ("no_test", False)):
        if not hasattr(args, attr):
            setattr(args, attr, default)

    CTX = Ctx(args.dry)
    if os.geteuid() != 0:
        die("must run as root (sudo).")
    args.hub = args.hub or detect_hub() or "help"
    args.include_dir = "/etc/httpd/%s.conf.d" % args.hub
    # services come in as bare positional args (a list); allow comma-joined too
    services = []
    for tok in (getattr(args, "services", None) or []):   # positional on install/uninstall/doctor/repair/test
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
    elif args.command == "test":
        cmd_test()

    step("Done")
    for n in CTX.notes:
        print("[!] " + n)


if __name__ == "__main__":
    main()
