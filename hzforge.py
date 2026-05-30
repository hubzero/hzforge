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

Requires Python 3.6+ (uses f-strings; deliberately avoids 3.7+ APIs so it runs on
RHEL 8's stock python3, 3.6).  Run as root.  Services are positional; preview any
command with --dry-run:
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
import configparser
import grp
import os
import pwd
import re
import stat
import subprocess
import sys
import tempfile

# ---------------------------------------------------------------------------- #
# Defaults
# ---------------------------------------------------------------------------- #
ALL_SERVICES  = ["svn", "git", "gitExternal", "trac"]

# Per-Python-version install matrix.  Three valid combinations:
#   (py27, mod_python, Trac 1.0.x)   -- legacy in-process handler
#   (py27, mod_wsgi,   Trac 1.0.x)   -- the current help-host config
#   (py36, mod_wsgi,   Trac 1.6.x)   -- Stage 2 target
# (py36 + mod_python is REJECTED in main(): mod_python upstream is Py2-only.)
#
# Per-version differences captured here:
#   py:                interpreter binary name (`python2` vs `python3`)
#   pip:               pip binary name (`pip2` vs `pip3`)
#   trac_spec:         pip-install spec (1.0.14 pinned for py27; 1.6.x range for
#                      py36 -- 1.6 is the first Py3-only release with active
#                      upstream).  Overridable via --trac-spec.
#   modwsgi_source:    "pip" -> pip install + we write our own LoadModule conf
#                      "rpm" -> `dnf install python3-mod_wsgi` (Rocky 8 AppStream
#                      4.6.4-5.el8); the RPM ships its own conf.modules.d file
#                      with the LoadModule wrapped in `<IfModule !wsgi_module>`
#                      so it self-defers.
#   modwsgi_pip_spec:  used only if modwsgi_source == "pip".  Overridable via
#                      --modwsgi-spec.
#   modwsgi_rpm:       used only if modwsgi_source == "rpm".
#   modwsgi_conf:      where the LoadModule conf for THIS mod_wsgi lives.  For
#                      "pip" we write it; for "rpm" the RPM owns it.
#   build_deps:        RPM build deps needed to pip-install mod_wsgi (compiler
#                      + Python + Apache headers).  Empty for rpm-installed
#                      mod_wsgi.
PY = {
    "py27": {
        "py":               "python2",
        "pip":              "pip2",
        "trac_spec":        "Trac==1.0.14",
        "modwsgi_source":   "pip",
        "modwsgi_pip_spec": "mod_wsgi==4.9.4",
        "modwsgi_rpm":      None,
        "modwsgi_conf":     "/etc/httpd/conf.modules.d/10-wsgi.conf",
        "build_deps":       ["gcc", "python2-devel", "httpd-devel"],
    },
    "py36": {
        "py":               "python3",
        "pip":              "pip3",
        "trac_spec":        "Trac>=1.6,<1.7",
        "modwsgi_source":   "rpm",
        "modwsgi_pip_spec": None,
        "modwsgi_rpm":      "python3-mod_wsgi",
        # Owned by the python3-mod_wsgi RPM; hzforge does not write or delete it.
        "modwsgi_conf":     "/etc/httpd/conf.modules.d/10-wsgi-python3.conf",
        "build_deps":       [],
    },
}

# Back-compat aliases (kept for code that still references the old constants).
TRAC_SPEC     = PY["py27"]["trac_spec"]
MODWSGI_SPEC  = PY["py27"]["modwsgi_pip_spec"]

# Options that live only on the `install` subparser, with their defaults.
# Single source of truth: build_parser() wires these defaults into the install
# args, and main() backfills the same attrs onto the other subcommands
# (uninstall/doctor/repair/test read them too).
#
# `trac_spec` / `modwsgi_spec` default to None here so main() can resolve them
# from PY[args.python] AFTER argparsing (an explicit --trac-spec / --modwsgi-spec
# on the command line wins, otherwise the python-version-appropriate default
# from PY[python] takes over).
INSTALL_DEFAULTS = {
    "python":           "py27",
    "trac_handler":     "mod_wsgi",
    "svn_source":       "wandisco",
    "trac_spec":        None,
    "modwsgi_spec":     None,
    "ldap_url":         None,
    "ldap_binddn":      None,
    "ldap_bindpw":      None,
    "ldap_bindpw_file": None,
    "force_pip":        False,
    "no_test":          False,
}


def _py():
    """Python binary name for the chosen --python (e.g. 'python2' or 'python3')."""
    return PY[ARGS.python]["py"]


def _pip():
    """pip binary name for the chosen --python (e.g. 'pip2' or 'pip3')."""
    return PY[ARGS.python]["pip"]


def _modwsgi_conf_path():
    """Path of the Apache LoadModule conf file for the chosen mod_wsgi.

    py27: hzforge writes /etc/httpd/conf.modules.d/10-wsgi.conf.
    py36: the python3-mod_wsgi RPM owns /etc/httpd/conf.modules.d/10-wsgi-python3.conf.
    Either way, this is where Apache picks up the LoadModule line."""
    return PY[ARGS.python]["modwsgi_conf"]

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
# MODCONF_WSGI is now per-python-version; resolve via _modwsgi_conf_path().
# Kept as a back-compat alias for any external callers and the disabled-rename
# path (still 10-wsgi.conf for py27, the only python that wrote one).
MODCONF_WSGI = PY["py27"]["modwsgi_conf"]
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
    name = m.group(2) if m else ''
    if not m or name in ('.', '..') or not os.path.isdir(os.path.join(TOOLS, name)):
        start_response('404 Not Found', [('Content-Type', 'text/plain')])
        return ['No such Trac environment\\n']
    environ['trac.env_path'] = os.path.join(TOOLS, name)
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

def log(m):
    print("    " + m)

def step(m):
    print("\n==> " + m)

def warn(m):
    print("[!] " + m)
    CTX.notes.append(m)

def die(m):
    print("[FATAL] " + m)
    sys.exit(1)


def run(cmd, check=True, capture=False, mutating=True):
    pretty = " ".join(cmd)
    if mutating and CTX.dry:
        log("[dry-run] " + pretty)
        return ""
    log("$ " + pretty)
    try:
        res = subprocess.run(cmd, stdout=subprocess.PIPE if capture else None,
                             stderr=subprocess.STDOUT if capture else None,
                             universal_newlines=True)
    except FileNotFoundError:
        die(f"command not found: {cmd[0]}")
    if check and res.returncode != 0:
        if capture and res.stdout:
            print(res.stdout)
        die(f"command failed (rc={res.returncode}): {pretty}")
    return (res.stdout or "") if capture else ""


def _ok(cmd):
    """True iff <cmd> runs and exits 0; False if it fails or its binary is absent."""
    try:
        return subprocess.run(cmd, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL).returncode == 0
    except FileNotFoundError:
        return False


def _out(cmd):
    """stdout of <cmd> (stderr discarded), or '' if it fails or its binary is absent."""
    try:
        return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              universal_newlines=True).stdout or ""
    except FileNotFoundError:
        return ""


def rpm_installed(pkg):
    return _ok(["rpm", "-q", pkg])


def group_exists(g):
    return _ok(["getent", "group", g])


def py_can_import(mod, py=None):
    """True iff `import <mod>` succeeds under the given (or configured) python.

    Defaults to whichever python the current command resolved (via _py()) --
    `python2` for --python=py27, `python3` for --python=py36.  Pass an explicit
    `py=` to probe one regardless of the current --python (used by fix_pip_perms
    which must probe the running daemon's python, and by `cmd_test` which
    checks both interpreters)."""
    return _ok([py or _py(), "-c", f"import {mod}"])


def py2_can_import(mod):
    """Back-compat alias for code paths that always meant python2 specifically
    (e.g. the legacy mod_python check, which is Py2-only)."""
    return _ok(["python2", "-c", f"import {mod}"])


def running_has_so(soname):
    """Is <soname> mapped into the RUNNING httpd? (decides restart vs reload)."""
    pids = _out(["pgrep", "-x", "httpd"]).split()
    for pid in pids:
        try:
            with open(f"/proc/{pid}/maps") as fh:
                if soname in fh.read():
                    return True
        except OSError:
            pass
    return False


def _uid(owner):
    try:
        return pwd.getpwnam(owner).pw_uid
    except KeyError:
        die(f"user '{owner}' not found -- is httpd installed?")


def _gid(group):
    try:
        return grp.getgrnam(group).gr_gid
    except KeyError:
        die(f"group '{group}' not found -- is httpd installed?")


def makedir(path, mode, owner="apache", group="apache"):
    if os.path.isdir(path):
        st = os.stat(path)
        if (stat.S_IMODE(st.st_mode) == mode and
                st.st_uid == _uid(owner) and
                st.st_gid == _gid(group)):
            return
    if CTX.dry:
        log(f"[dry-run] mkdir {path} (mode {mode:o}, {owner}:{group})")
        return
    if not os.path.isdir(path):
        os.makedirs(path)
    os.chmod(path, mode)
    os.chown(path, _uid(owner), _gid(group))
    log(f"dir: {path} ({mode:o} {owner}:{group})")


def write_file(path, content, mode, owner="root", group="root"):
    cur = None
    if os.path.exists(path):
        with open(path) as fh:
            cur = fh.read()
    ok = (cur == content and os.path.exists(path))
    if ok:
        st = os.stat(path)
        ok = (stat.S_IMODE(st.st_mode) == mode and
              st.st_uid == _uid(owner) and
              st.st_gid == _gid(group))
    if ok:
        log("unchanged: " + path)
        return False
    if CTX.dry:
        log(f"[dry-run] write {path} (mode {mode:o}, {owner}:{group})")
        return True
    d = os.path.dirname(path)
    if not os.path.isdir(d):
        os.makedirs(d)
    fd, tmp = tempfile.mkstemp(dir=d)
    with os.fdopen(fd, "w") as fh:
        fh.write(content)
    os.replace(tmp, path)
    os.chmod(path, mode)
    os.chown(path, _uid(owner), _gid(group))
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


def pip_install(spec):
    """Always run pip behind `umask 022` so the files root installs stay
    world-readable -- apache must be able to import them (root's default umask
    0077 would make them unreadable, breaking Trac under mod_wsgi).  Uses the
    pip that goes with --python (`pip2` for py27, `pip3` for py36).  The spec
    is passed as a shell argument ($2), never interpolated into the command
    string, so it can't break out and inject commands."""
    run(["sh", "-c", 'umask 022; exec "$1" install "$2"', "sh", _pip(), spec])


def pip_install_plugin_wheel(wheel_path, interpreters=("pip2", "pip3")):
    """Install a Trac plugin wheel (cmsauth, mysqlauthz, macros) into every
    available system pip interpreter.  Use this -- NOT `pip_install` -- for
    anything that drops in next to Trac as a Component.

    Two differences from `pip_install`:

    * `--no-deps`.  Plugin `setup.cfg` files deliberately omit `Trac` from
      `install_requires` (the host is the source of truth for which Trac
      version is present), but the host's pip resolver would still re-resolve
      any other listed deps -- and a plugin that ever adds `Trac>=X` back
      would let pip clobber the running mod_wsgi daemon's Trac install.
      `--no-deps` is the second belt.  Incident on 2026-05-29: a routine
      `sudo pip2 install <wheel>` resolved `Trac>=1.0` from a stale
      `install_requires`, upgraded Py2 Trac 1.0.14 -> 1.4.4, and would have
      nuked every Trac env on the next Apache cycle.  Recovery was an hour
      of chmod + pin-back-to-1.0.14; `--no-deps` would have prevented it.

    * Install into BOTH `pip2` and `pip3` (whichever are present).  The help
      host runs Trac on Py2.7 today AND keeps a parallel Py3.6 stack for
      stage 2 prep; plugin wheels are `py2.py3-none-any` and should be
      usable from either interpreter.  Hosts that only have one of the two
      skip the missing one quietly.

    Like `pip_install`, runs behind `umask 022` so apache can read the
    installed files (root's default umask 0077 makes them mode 600/700
    otherwise -- see `fix_pip_perms()` for the recovery path).

    --upgrade so re-installing a higher version replaces the older one in
    place; same-version reinstalls are a no-op (pip 9's `--force-reinstall`
    same-version quirk is avoided by version-bumping, not by forcing here).
    """
    installed_any = False
    for pip in interpreters:
        if not _ok([pip, "--version"]):
            log(f"{pip} not present -- skipping plugin install on this interpreter")
            continue
        run(["sh", "-c",
             'umask 022; exec "$1" install --no-deps --upgrade "$2"',
             "sh", pip, wheel_path])
        installed_any = True
    if not installed_any:
        die(f"no pip interpreter found ({', '.join(interpreters)}); "
            "install python2 and/or python3 first")


def ensure_wandisco_repo():
    write_file(WANDISCO_REPO_PATH, WANDISCO_REPO, 0o644)


def _svn_repo():
    return "wandisco-svn110" if ARGS.svn_source == "wandisco" else None


def _svn_module_stream():
    """The currently-enabled subversion module stream, or None."""
    out = _out(["dnf", "-q", "module", "list", "--enabled", "subversion"])
    m = re.search(r"^subversion\s+(\S+)", out, re.M)
    return m.group(1) if m else None


def _enable_svn_source():
    """Make subversion-* installable for the svn SERVICE.  (The trac stack no
    longer needs this -- hzforge skips the `hubzero-trac` metapackage, whose
    %post pip-installed subvertpy was the only reason subversion-devel was
    pulled.) Does NOT install anything itself."""
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
    makedir(*OPT["svn"])
    makedir(*OPT["svn_tools"])
    makedir(os.path.join(ARGS.include_dir, "svn"), 0o700)


def setup_git():
    step("Git (local) -- package, group, dirs")
    if not rpm_installed("git"):
        dnf_install(["git"])
    ensure_group("hzgit")
    makedir(*OPT["git"])
    makedir(*OPT["git_tools"])
    makedir(os.path.join(ARGS.include_dir, "git"), 0o700)


def setup_gitexternal():
    step("Git (external) -- group, dirs")
    if not rpm_installed("git"):
        dnf_install(["git"])
    ensure_group("hzgit")
    makedir(*OPT["gext"])
    makedir(*OPT["gext_tools"])
    makedir(os.path.join(ARGS.include_dir, "git"), 0o700)


def setup_trac():
    step(f"Trac -- packages, dirs, handler ({ARGS.trac_handler}, {ARGS.python})")
    # We deliberately do NOT install the `hubzero-trac` metapackage: its only
    # meaningful runtime payload is hubzero-trac-mysqlauthz (the auth plugin),
    # and its %post pip-installs Trac==1.0.13 + subvertpy -- both of which we
    # don't want.  Pull just the runtime + build deps we actually need.
    needed = ["hubzero-trac-mysqlauthz"]               # from the hubzero repo
    if ARGS.trac_handler == "mod_wsgi":
        # py27+mod_wsgi: pip-built mod_wsgi 4.9.4 needs a C toolchain + Apache +
        # Py2 headers.  py36+mod_wsgi: nothing to build (python3-mod_wsgi RPM
        # ships the .so).
        needed += PY[ARGS.python]["build_deps"]
        if PY[ARGS.python]["modwsgi_source"] == "rpm":
            needed.append(PY[ARGS.python]["modwsgi_rpm"])    # python3-mod_wsgi
    to_install = [p for p in needed if not rpm_installed(p)]
    if to_install:
        dnf_install(to_install)
    # Repo browser support (svn.core) is auto-enabled via subversion-python
    # when the svn SERVICE is installed; Trac runs fine without it.
    makedir(*OPT["trac"])
    makedir(*OPT["trac_tools"])
    if ARGS.trac_handler == "mod_wsgi":
        _trac_modwsgi()
    else:
        _trac_modpython()


def _trac_spec_exact_version():
    """If --trac-spec is an exact pin (`Trac==X.Y.Z`), return the version; else None
    (e.g. for a range like `Trac>=1.0,<1.1`, no exact version to enforce)."""
    m = re.match(r"^\s*Trac\s*==\s*([^\s,]+)\s*$", ARGS.trac_spec, re.I)
    return m.group(1) if m else None


def _trac_installed_version():
    """The chosen pip's view of the installed Trac dist version, or None if
    pip / Trac absent.  This is the pip dist version, which is what
    `Trac==X.Y.Z` pins -- the quirky `trac.__version__` (frozen at 1.0.13
    across 1.0.x dists) is not used."""
    m = re.search(r"^Version:\s*(\S+)", _out([_pip(), "show", "Trac"]), re.M)
    return m.group(1) if m else None


def _ensure_trac_installed():
    """pip-install Trac unless it already imports at the pinned version
    (or --force-pip is set).  When --trac-spec is an exact pin and the
    installed dist differs, reinstall so doctor/repair/install converge."""
    if not ARGS.force_pip and py_can_import("trac"):
        want = _trac_spec_exact_version()
        if not want:
            log("Trac importable (skip pip; --force-pip to reinstall)")
            return
        have = _trac_installed_version()
        if have == want:
            log(f"Trac importable ({have}) (skip pip; --force-pip to reinstall)")
            return
        log(f"Trac {have or '?'} installed but pin is {want} -- reinstalling")
    pip_install(ARGS.trac_spec)


def _trac_modwsgi():
    _ensure_trac_installed()
    src = PY[ARGS.python]["modwsgi_source"]
    if src == "pip":
        # py27: pip-install mod_wsgi (4.9.4 last Py2-capable).  Check
        # filesystem .so first (mod_wsgi-express won't be on PATH on a fresh
        # host).
        if not os.path.exists(_modwsgi_so()) and not py_can_import("mod_wsgi"):
            pip_install(ARGS.modwsgi_spec)
        else:
            log("mod_wsgi present (skip pip)")
    else:
        # py36: the python3-mod_wsgi RPM was already added to setup_trac()'s
        # dnf_install list above; nothing for us to install separately.
        log("mod_wsgi from python3-mod_wsgi RPM (already installed)")
    # shim
    makedir(WSGI_DIR, 0o755)
    if write_file(SHIM_PATH, SHIM_CONTENT, 0o644, "apache", "apache"):
        _mark()
    # server-scope module config; ensure mod_python NOT loaded
    _ensure_modwsgi_loaded()
    _ensure_modpython_unloaded()


def _trac_modpython():
    # mod_python serves Trac in-process; ensure Trac is importable, the module
    # is loaded, and mod_wsgi isn't.  mod_python comes from the hubzero
    # (julian-el8) repo, built for the Python 2.7 it embeds.  Only valid with
    # --python=py27 (main() rejects py36+mod_python upstream).
    _ensure_trac_installed()
    if not rpm_installed("mod_python"):
        dnf_install(["mod_python"])
    makedir(EGG_CACHE, 0o755)
    _ensure_modpython_loaded()
    _ensure_modwsgi_unloaded()


def _modwsgi_module_config():
    """`mod_wsgi-express module-config` output, or '' if not pip-installed yet
    (fresh host / dry-run)."""
    return _out(["mod_wsgi-express", "module-config"])


def _modwsgi_so():
    m = re.search(r'LoadModule\s+wsgi_module\s+"([^"]+)"', _modwsgi_module_config())
    return m.group(1) if m else ""


def _ensure_modwsgi_loaded():
    if PY[ARGS.python]["modwsgi_source"] == "rpm":
        # py36: the python3-mod_wsgi RPM owns its own 10-wsgi-python3.conf
        # (LoadModule wrapped in `<IfModule !wsgi_module>` so it self-defers
        # if the py2 mod_wsgi is also loaded -- we have to make sure it
        # ISN'T, by ALSO renaming the py27 conf if present).  Nothing for us
        # to write; just ensure the py27 conf is out of the way.
        log("mod_wsgi loaded via python3-mod_wsgi RPM "
            f"({_modwsgi_conf_path()})")
        py27_conf = PY["py27"]["modwsgi_conf"]
        if os.path.exists(py27_conf):
            _rename(py27_conf, py27_conf + ".disabled")
        return
    # py27: derive LoadModule + WSGIPythonHome from `mod_wsgi-express
    # module-config` (the pip-installed mod_wsgi-express ships the right .so
    # path for the running Py2) and write our own conf.
    out = _modwsgi_module_config()
    load = next((l for l in out.splitlines() if l.startswith("LoadModule")),
                'LoadModule wsgi_module "<run mod_wsgi-express module-config>"')
    home = next((l for l in out.splitlines() if l.startswith("WSGIPythonHome")),
                'WSGIPythonHome "/usr"')
    content = ("# Python2 mod_wsgi -- managed by hzforge\n"
               + load + "\n" + home + "\nWSGISocketPrefix run/wsgi\nWSGIRestrictEmbedded On\n")
    if write_file(_modwsgi_conf_path(), content, 0o644):
        _mark()


def _ensure_modwsgi_unloaded():
    # Both the py27 (hzforge-managed) and py36 (RPM-managed) confs need to be
    # turned off if we're switching off mod_wsgi entirely.
    for path in (PY["py27"]["modwsgi_conf"], PY["py36"]["modwsgi_conf"]):
        if os.path.exists(path):
            _rename(path, path + ".disabled")


def _ensure_modpython_loaded():
    dis = MODCONF_PY + ".disabled"
    if os.path.exists(dis) and not os.path.exists(MODCONF_PY):
        _rename(dis, MODCONF_PY)
    elif not os.path.exists(MODCONF_PY):
        if write_file(MODCONF_PY, "LoadModule python_module modules/mod_python.so\n", 0o644):
            _mark()


def _ensure_modpython_unloaded():
    if os.path.exists(MODCONF_PY):
        _rename(MODCONF_PY, MODCONF_PY + ".disabled")


def _rename(a, b):
    if CTX.dry:
        log(f"[dry-run] mv {a} -> {b}")
    else:
        os.rename(a, b)
        log(f"mv {a} -> {b}")
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
    L = [f"# HUBzero '{svc}' service -- managed by hzforge",
         "# Self-contained drop-in (vhost scope); independent of the m4 vhost template.",
         "RewriteEngine On",
         ""]
    if svc == "svn":
        L += ['# DAV-svn is a <Location> handler -> shield from the CMS catch-all rewrite',
              'RewriteRule "^/tools/[^/]+/svn(/|$)" - [END]',
              "",
              f"IncludeOptional {inc}/svn/svn.conf"]
    elif svc == "git":
        L += ['# git http-backend (ScriptAliasMatch self-diverts; shield non-protocol paths)',
              'RewriteRule "^/tools/[^/]+/git(/|$)" - [END]',
              "",
              f"IncludeOptional {inc}/git/git.conf"]
    elif svc == "gitExternal":
        L += ['RewriteRule "^/tools/[^/]+/gitExternal(/|$)" - [END]',
              "",
              f"IncludeOptional {inc}/git/gitExternal.conf"]
    elif svc == "trac":
        if ARGS.trac_handler == "mod_wsgi":
            L += ["# Trac via mod_wsgi (WSGIScriptAliasMatch self-diverts; no carve-out needed).",
                  "# processes=1 is intentional: Trac's SQLite connection pool keeps a long-lived",
                  "# connection per worker, and pooled SQLite connections don't reliably see rows",
                  "# INSERTed by other worker processes (the trac_auth `auth_cookie` table is the",
                  "# canonical symptom -- a row written by worker A becomes invisible to a SELECT in",
                  "# worker B with default `journal_mode=delete`).  threads=30 gives equivalent",
                  "# concurrency to the previous processes=2 threads=15.",
                  "WSGIDaemonProcess trac user=apache group=apache processes=1 threads=30 "
                  "python-home=/usr display-name=%{GROUP}",
                  "WSGIApplicationGroup %{GLOBAL}",
                  f'WSGIScriptAliasMatch "^/tools/[^/]+(?=/(?:{verbs})(?:/|$))" '
                  f'{SHIM_PATH} process-group=trac',
                  f"<Directory {WSGI_DIR}>",
                  "    <Files hubtrac.wsgi>",
                  "        Require all granted",
                  "    </Files>",
                  "</Directory>"]
        else:
            L += ["# Trac via mod_python -- <Location> per env; shield verbs from the CMS rewrite",
                  f'RewriteRule "^/tools/[^/]+/({verbs})(/|$)" - [END]',
                  f"PythonOption PYTHON_EGG_CACHE {EGG_CACHE}"]
            for env in _trac_envs():
                L += [f"<Location /tools/{env}>",
                      "    SetHandler mod_python",
                      "    PythonHandler trac.web.modpython_frontend",
                      "    PythonInterpreter main_interpreter",
                      f"    PythonOption TracEnv /opt/trac/tools/{env}",
                      f"    PythonOption TracUriRoot /tools/{env}",
                      "</Location>"]
        ab = _auth_block()
        if ab:
            L += ["", ab]
    content = "\n".join(L).rstrip() + "\n"
    step(f"Apache drop-in: {dropin_path(svc)}")
    if write_file(dropin_path(svc), content, 0o640):
        _mark()


def _trac_envs():
    """Trac env names = subdirs of /opt/trac/tools containing conf/trac.ini."""
    base = OPT["trac_tools"][0]
    if not os.path.isdir(base):
        return []
    return sorted(d for d in os.listdir(base)
                  if os.path.exists(os.path.join(base, d, "conf", "trac.ini")))


def _ldap_bindpw():
    """Resolve the LDAP bind password: a --ldap-bindpw-file (read root-only)
    is preferred over an inline --ldap-bindpw (which leaks via the process
    list); fall back to harvesting it from the existing conf."""
    if ARGS.ldap_bindpw_file:
        path = ARGS.ldap_bindpw_file
        try:
            mode = stat.S_IMODE(os.stat(path).st_mode)
            with open(path) as fh:
                pw = fh.read().strip()
        except OSError as e:
            die(f"--ldap-bindpw-file: cannot read {path}: {e}")
        if mode & 0o077:
            warn(f"{path} is group/other-accessible (mode {mode:o}); chmod 600 it")
        return pw
    if ARGS.ldap_bindpw:
        warn("--ldap-bindpw exposes the password in the process list; "
             "prefer --ldap-bindpw-file")
        return ARGS.ldap_bindpw
    return _grep_conf("AuthLDAPBindPassword")


def _auth_block():
    url    = ARGS.ldap_url    or _grep_conf("AuthLDAPURL")
    binddn = ARGS.ldap_binddn or _grep_conf("AuthLDAPBindDN")
    bindpw = _ldap_bindpw()
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
        out = run([_pip(), "show", "-f", pkg], capture=True, check=False, mutating=False)
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
    probe = subprocess.run(["runuser", "-u", "apache", "--", _py(), "-c", "import trac"],
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
    return _ok(["pgrep", "-x", "httpd"])


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
        return _out(["systemctl", "is-active", "httpd"]).strip()
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
        warn(f"--no-restart: staged but not applied; {'restart' if need_restart else 'reload'} "
             "httpd to activate.")
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
    path = f"/etc/httpd/sites.d/{ARGS.hub}-ssl.conf"
    if not os.path.exists(path):
        return None
    with open(path) as fh:
        t = fh.read()
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
            ["curl", "-s", "-k", "--resolve", f"{tgt['host']}:{tgt['port']}:{tgt['ip']}",
             "-o", tmp, "-w", "%{http_code}",
             f"{tgt['scheme']}://{tgt['host']}{path}"],
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
        die(f"self-test route failed configtest ({why}) -- running server untouched")
    apache_apply(restart=False)


def _svn_route(name, repo):
    return [f'<Location /tools/{name}/svn>',
            '    DAV svn',
            f'    SVNPath {repo}',
            '    Require all granted',          # throwaway repo, removed right after
            '</Location>', '']


def _git_route(svc, name, root, reponame):
    # http-backend route for the throwaway repo, mirroring the MySQL-generated conf.
    # On-disk layout differs by service: git -> <name>.git, gitExternal -> <name>
    # (passed in as reponame), so the CGI PATH_INFO must match.
    return [f'SetEnvIf Request_URI "^/tools/{name}/{svc}/{name}/" GIT_PROJECT_ROOT={root}',
            'SetEnv GIT_HTTP_EXPORT_ALL',
            f'ScriptAliasMatch "^/tools/{name}/{svc}/{name}/(.*)$" '
            f'/usr/libexec/git-core/git-http-backend/{reponame}/$1',
            f'<LocationMatch "^/tools/{name}/{svc}">',
            '    Require all granted',          # anonymous read for the throwaway repo
            '</LocationMatch>', '']


def _trac_modpython_route(name, env):
    # per-env <Location> for the legacy mod_python handler (mirrors the install block);
    # mod_wsgi needs none of this -- its generic WSGIScriptAliasMatch serves any env.
    return [f'<Location /tools/{name}>',
            '    SetHandler mod_python',
            '    PythonHandler trac.web.modpython_frontend',
            '    PythonInterpreter main_interpreter',
            f'    PythonOption TracEnv {env}',
            f'    PythonOption TracUriRoot /tools/{name}',
            f'    PythonOption PYTHON_EGG_CACHE {EGG_CACHE}',
            '    Require all granted',          # anonymous read for the throwaway env
            '</Location>', '']


def _check_trac(tgt, name):
    code, body = _curl(tgt, f"/tools/{name}/wiki")
    ok = code == "200" and ("Trac" in body or "Wiki" in body or "powered by" in body.lower())
    log(f"trac:        GET /tools/{name}/wiki -> HTTP {code}{'  (Trac)' if ok else ''}")
    return ok


def _check_svn(tgt, name):
    code, body = _curl(tgt, f"/tools/{name}/svn/")
    ok = code == "200" and ("subversion" in body.lower() or "Revision 0" in body)
    log(f"svn:         GET /tools/{name}/svn/ -> HTTP {code}{'  (mod_dav_svn)' if ok else ''}")
    return ok


def _check_git(tgt, svc, name):
    p = f"/tools/{name}/{svc}/{name}/info/refs?service=git-upload-pack"
    code, body = _curl(tgt, p)
    ok = code == "200" and "service=git-upload-pack" in body
    log(f"{svc + ':':<12} GET /tools/{name}/{svc}/{name}/info/refs -> HTTP {code}"
        f"{'  (git-http-backend)' if ok else ''}")
    return ok


def cmd_test():
    """Create throwaway, uniquely-named backing resources for each requested service,
    verify each actually serves over HTTP, then remove them.  Self-contained: needs no
    MySQL/forge provisioning.  mod_wsgi trac is served by hzforge's generic WSGI route, so
    it needs no config change; svn, git, and mod_python trac need a per-resource route, so
    a temporary self-test drop-in is added and removed around the checks (graceful reload)."""
    configured = detect_configured_services()
    for s in (ARGS.services or []):
        if s not in configured:
            die(f"'{s}' is not configured here -- run 'install {s}' first")
    targets = [s for s in (ARGS.services or [t for t in TESTABLE if t in configured])
               if s in TESTABLE]
    if not targets:
        die(f"nothing testable here (configured: {','.join(configured) or 'none'}) "
            "-- install a service first")
    tgt = _vhost_target()
    if not tgt:
        die(f"could not detect the hub vhost (/etc/httpd/sites.d/{ARGS.hub}-ssl.conf)")
    handler = detect_handler() or ARGS.trac_handler

    name = f"hzforge-selftest-{os.getpid()}"
    step(f"Self-test: {','.join(targets)} -- throwaway name '{name}' "
         f"via {tgt['scheme']}://{tgt['host']}")
    if "trac" in targets:
        log(f"trac handler: {handler}")
    if CTX.dry:
        log(f"[dry-run] would create throwaway {','.join(targets)}, curl each, then remove")
        return

    conf = _selftest_conf_path()
    if os.path.exists(conf):
        die(f"stale self-test conf present: {conf} (remove it and retry)")
    created, results = [], {}
    route_lines = ["# hzforge self-test routes -- throwaway, removed automatically",
                   "RewriteEngine On", ""]
    routed = False                              # do svn/git/mod_python-trac need a temp route?
    try:
        if "trac" in targets:
            env = os.path.join(OPT["trac_tools"][0], name)
            if os.path.exists(env):
                die(f"test path already exists: {env}")
            created.append(env)   # register before creating so cleanup covers a partial create
            run(["trac-admin", env, "initenv", name, "sqlite:db/trac.db"], capture=True)
            run(["chown", "-R", "apache:apache", env])
            # mod_wsgi is served by hzforge's generic route; mod_python needs a per-env <Location>
            if handler == "mod_python":
                route_lines += _trac_modpython_route(name, env)
                routed = True
        if "svn" in targets:
            repo = os.path.join(OPT["svn_tools"][0], name)
            if os.path.exists(repo):
                die(f"test path already exists: {repo}")
            created.append(repo)   # register before creating so cleanup covers a partial create
            run(["svnadmin", "create", repo], capture=True)
            run(["chown", "-R", "apache:apache", repo])
            route_lines += _svn_route(name, repo)
            routed = True
        for gsvc, key in (("git", "git_tools"), ("gitExternal", "gext_tools")):
            if gsvc in targets:
                root = OPT[key][0]
                # on-disk layout matches the forge convention: git -> <name>.git,
                # gitExternal -> <name> (no suffix)
                reponame = name + ".git" if gsvc == "git" else name
                repo = os.path.join(root, reponame)
                if os.path.exists(repo):
                    die(f"test path already exists: {repo}")
                created.append(repo)   # register before creating so cleanup covers a partial create
                run(["git", "init", "--bare", "-q", repo], capture=True)
                run(["chown", "-R", "apache:apache", repo])
                route_lines += _git_route(gsvc, name, root, reponame)
                routed = True
        if routed:
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
        die(f"self-test FAILED: {', '.join(failed)}")
    step(f"Self-test PASSED: {', '.join(targets)}")


# ---------------------------------------------------------------------------- #
# Introspection / uninstall / doctor / repair
# ---------------------------------------------------------------------------- #
def _remove_file(path):
    if not os.path.exists(path):
        return
    if CTX.dry:
        log("[dry-run] rm " + path)
    else:
        os.remove(path)
        log("rm " + path)
    _mark()


def detect_configured_services():
    """Which services are wired now: one 00-forge-<svc>.conf each (+ a legacy trac.conf)."""
    found = [s for s in ALL_SERVICES if os.path.exists(dropin_path(s))]
    if "trac" not in found and os.path.exists(os.path.join(ARGS.include_dir, "trac.conf")):
        found.append("trac")
    return found


def detect_handler():
    """Trac handler currently wired (from the trac drop-in or a legacy trac.conf), else None.
    Match markers that are present even before any env exists: the mod_python drop-in always
    carries `PythonOption` (egg cache), the mod_wsgi one always `WSGIScriptAliasMatch`.
    `modpython_frontend` only appears once there are per-env <Location> blocks."""
    for p in (dropin_path("trac"), os.path.join(ARGS.include_dir, "trac.conf")):
        if os.path.exists(p):
            with open(p) as fh:
                t = fh.read()
            if "WSGIScriptAliasMatch" in t:                      return "mod_wsgi"
            if "modpython_frontend" in t or "PythonOption" in t: return "mod_python"
    return None


def _disable_legacy_trac():
    """Move a standalone <hub>.conf.d/trac.conf aside (replaced by the trac drop-in)."""
    legacy = os.path.join(ARGS.include_dir, "trac.conf")
    if os.path.exists(legacy):
        warn(f"disabling standalone {legacy} (replaced by {dropin_path('trac')})")
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
    # verify the just-installed services actually serve, unless suppressed
    # (both Trac handlers are self-testable).
    testable = [x for x in s if x in TESTABLE]
    if testable and not ARGS.no_test and not ARGS.no_restart and not CTX.dry:
        cmd_test()


def uninstall(remove):
    if not remove:
        die("uninstall needs at least one service (e.g. 'hzforge uninstall git')")
    configured = detect_configured_services()
    handler = detect_handler() or ARGS.trac_handler
    ARGS.trac_handler = handler
    actual = [s for s in remove if s in configured]      # only what's actually wired
    skipped = [s for s in remove if s not in configured]
    if not actual:
        if configured:
            step(f"Uninstall: {','.join(remove)} not configured here "
                 f"(configured: {','.join(configured)}) -- nothing to do")
        else:
            step("Uninstall: no hzforge services configured here -- nothing to do "
                 f"(requested: {','.join(remove)})")
        return                              # leave the running server untouched
    remaining = [x for x in configured if x not in actual]
    skip = f"  [skipping {','.join(skipped)}: not configured]" if skipped else ""
    step(f"Uninstall {','.join(actual)}   "
         f"(configured={','.join(configured) or '-'} -> "
         f"remaining={','.join(remaining) or '-'}){skip}")
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


_CHK_LABELS = {"INFO": "[info]", "OK": "[ ok ]", "WARN": "[warn]", "FAIL": "[FAIL]"}


def _chk(results, level, msg):
    results.append((level, msg))
    print(f"    {_CHK_LABELS[level]:<7} {msg}")


def doctor():
    step(f"Doctor -- hub={ARGS.hub}")
    r = []
    inc = ARGS.include_dir
    services = detect_configured_services()
    target = ARGS.services or services           # positional services = explicit scope
    handler = detect_handler()
    legacy = os.path.join(inc, "trac.conf")
    if os.path.exists(legacy):
        _chk(r, "WARN", f"standalone {legacy} present -> consolidate via 'install' "
                        "(avoid duplicate WSGI)")
    _chk(r, "INFO", f"configured services: {','.join(services) or '(none)'}")
    if ARGS.services:
        _chk(r, "INFO", f"scope: {' '.join(target)}")
        for s in ARGS.services:
            if s not in services:
                _chk(r, "WARN", f"'{s}' requested but not currently configured")
    for svc in target:
        p = dropin_path(svc)
        present = os.path.exists(p) or (svc == "trac" and os.path.exists(legacy))
        state = "present" if present else f"{p} ABSENT (run install)"
        _chk(r, "OK" if present else "WARN", f"{svc} drop-in {state}")
    if "trac" in target:
        _chk(r, "INFO", f"trac handler: {handler or 'n/a'}")

    wsgi_disk, py_disk = _ondisk_module("wsgi_module"), _ondisk_module("python_module")
    wsgi_run, py_run = running_has_so("mod_wsgi"), running_has_so("mod_python")

    def ld(b):
        return "loaded" if b else "not loaded"

    if wsgi_disk and py_disk:
        _chk(r, "FAIL", "both mod_wsgi and mod_python enabled on disk -> httpd won't start (repair)")
    for name, running, disk in (("mod_wsgi", wsgi_run, wsgi_disk),
                                ("mod_python", py_run, py_disk)):
        if running == disk:
            _chk(r, "OK", f"{name}: {ld(disk)}")
        else:
            _chk(r, "WARN", f"{name}: on-disk {ld(disk)} but running {ld(running)} "
                            "-- restart pending (repair)")

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
            shim = os.path.exists(SHIM_PATH)
            _chk(r, "OK" if shim else "FAIL",
                 f"shim {'present' if shim else 'MISSING (repair)'}")
        ti = subprocess.run(["runuser", "-u", "apache", "--", _py(), "-c", "import trac"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, universal_newlines=True)
        if ti.returncode == 0:
            _chk(r, "OK", "apache can import Trac")
        else:
            perm = "Permission denied" in (ti.stderr or "")
            _chk(r, "FAIL", f"apache cannot import Trac{' [umask perms]' if perm else ''} "
                            f"(repair: {'fix-perms' if perm else 'reinstall Trac'})")
        pinned = _trac_spec_exact_version()
        have = _trac_installed_version()
        if pinned:
            if have is None:
                _chk(r, "WARN", f"Trac version: not pip-installed (pin: {pinned})")
            elif have == pinned:
                _chk(r, "OK", f"Trac version: {have} (matches pin)")
            else:
                _chk(r, "FAIL", f"Trac version: {have} != pin {pinned} (repair to reinstall)")
        elif have:
            _chk(r, "INFO", f"Trac version: {have} (spec: {ARGS.trac_spec})")
        # Trac's repo browser is optional -- only relevant when the svn service is
        # configured (it pulls subversion-python). Trac-without-svn is a valid config.
        if "svn" in services:
            # subversion-python is Py2-only on Rocky 8 (RHEL8 dropped Py3 SWIG
            # bindings).  This probe stays Py2 regardless of --python: if the
            # daemon is py36 and svn.core isn't available, the repo browser
            # degrades to an error -- same as Trac-without-svn.
            sc = subprocess.run(["runuser", "-u", "apache", "--", "python2", "-c", "import svn.core"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            _chk(r, "OK" if sc.returncode == 0 else "WARN",
                 "Trac repo browser (svn.core) " + ("available" if sc.returncode == 0
                 else "unavailable -> install subversion-python"))
    if "svn" in target:
        dav = rpm_installed("mod_dav_svn")
        _chk(r, "OK" if dav else "FAIL", "mod_dav_svn " + ("installed" if dav else "MISSING"))
        f = os.path.join(inc, "svn", "svn.conf")
        have = os.path.exists(f)
        state = "present" if have else "absent (generated externally)"
        _chk(r, "OK" if have else "WARN", f"per-tool {f} {state}")
    if "git" in target or "gitExternal" in target:
        have_git = rpm_installed("git")
        _chk(r, "OK" if have_git else "FAIL", "git " + ("installed" if have_git else "MISSING"))
        for svc, fn in (("git", "git.conf"), ("gitExternal", "gitExternal.conf")):
            if svc in target:
                f = os.path.join(inc, "git", fn)
                have = os.path.exists(f)
                state = "present" if have else "absent (generated externally)"
                _chk(r, "OK" if have else "WARN", f"per-tool {f} {state}")
    for key, svc in (("trac_tools", "trac"), ("svn_tools", "svn"),
                     ("git_tools", "git"), ("gext_tools", "gitExternal")):
        if svc in target:
            path = OPT[key][0]
            have = os.path.isdir(path)
            state = "exists" if have else "MISSING (repair)"
            _chk(r, "OK" if have else "WARN", f"{path} {state}")

    fails = sum(1 for lvl, _ in r if lvl == "FAIL")
    warns = sum(1 for lvl, _ in r if lvl == "WARN")
    oks = sum(1 for lvl, _ in r if lvl == "OK")
    step(f"Doctor: {fails} FAIL, {warns} WARN, {oks} OK")
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
    step(f"Repair -- scope={','.join(scope) or 'all'}, "
         f"re-assert={','.join(target) or 'none'} "
         f"(configured: {','.join(configured) or 'none'})")
    for s in requested:
        if s not in configured:
            warn(f"'{s}' is not configured -> nothing to repair (use 'install {s}')")
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


def _macros_universal_installed():
    """True iff the universal `hubzero_macros` plugin is importable by some
    Python available on this host (Py2 today; Py3 after Stage 2).  When yes,
    per-env legacy `image.py`/`link.py` copies are safe to disable because
    the universal plugin keeps serving the same `[[image …]]` / `[[link …]]`
    macros."""
    for py in ("python2", "python3"):
        if _ok([py, "-c", "import hubzero_macros"]):
            return True
    return False


def _ensure_components_enabled(trac_ini, key, value="enabled"):
    """Ensure `[components] <key> = <value>` is present in trac.ini.

    Idempotent text-level surgery: if the exact `key = value` line is
    already in any section, no-op; else insert it right after the
    `[components]` header (or append a new `[components]` section at EOF
    if none exists).  Preserves comments, ordering, whitespace, and the
    file's existing mode/owner.  Honors --dry-run.  Returns True iff
    modified.

    Trac's `Environment.is_component_enabled` returns None for non-`trac.*`
    components -- which the framework interprets as "enabled only if
    located in <env>/plugins/" -- so entry-point plugins like
    hubzero_macros need an explicit `[components]` enable to activate."""
    if not os.path.isfile(trac_ini):
        return False
    with open(trac_ini) as fh:
        text = fh.read()
    # Already enabled?  Match the exact `key = value` line anywhere in the file.
    if re.search(r'^\s*' + re.escape(key) + r'\s*=\s*' + re.escape(value) + r'\s*$',
                 text, re.M):
        return False
    sec = re.search(r'^\[components\]\s*$', text, re.M)
    if sec:
        end = sec.end()
        new_text = text[:end] + "\n" + key + " = " + value + text[end:]
    else:
        if not text.endswith("\n"):
            text += "\n"
        new_text = text + "\n[components]\n" + key + " = " + value + "\n"
    if CTX.dry:
        log(f"[dry-run] enable [components] {key} = {value} in {trac_ini}")
        _mark()
        return True
    st = os.stat(trac_ini)
    d = os.path.dirname(trac_ini)
    fd, tmp = tempfile.mkstemp(dir=d)
    with os.fdopen(fd, "w") as fh:
        fh.write(new_text)
    os.replace(tmp, trac_ini)
    os.chmod(trac_ini, stat.S_IMODE(st.st_mode))
    try:
        os.chown(trac_ini, st.st_uid, st.st_gid)
    except PermissionError:
        pass   # tests may run as a non-root user with no chown rights
    log(f"enabled [components] {key} = {value} in {trac_ini}")
    _mark()
    return True


# Match the LDAP <LocationMatch ...> directive in 00-forge-trac.conf,
# capturing the negative-lookahead env-name list (if any) so we can
# extend it.  Three observed shapes:
#
#   <LocationMatch "^/tools/[^/]+/login">                 (no carve-out yet)
#   <LocationMatch "^/tools/(?!FOO/)[^/]+/login">         (single env)
#   <LocationMatch "^/tools/(?!(?:FOO|BAR)/)[^/]+/login"> (multiple envs)
#
# Group 1 = the prefix up to (and including) `^/tools/`.
# Group 2 = the carve-out env-list "FOO" or "FOO|BAR" (empty/None if absent).
# Group 3 = the suffix `[^/]+/login"`.
_LDAP_LOC_RE = re.compile(
    r'(<LocationMatch\s+"\^/tools/)'
    r'(?:\(\?!(?:\(\?:)?([\w\-]+(?:\|[\w\-]+)*)\)?/\))?'
    r'(\[\^/\]\+/login")'
)


def _ldap_carveout_ensure(httpd_conf, env_name):
    """Ensure `env_name` is in the negative-lookahead skip list of the LDAP
    `<LocationMatch>` block in `httpd_conf`.  Returns True iff modified.

    Idempotent text-level surgery -- preserves the rest of the file byte for
    byte (mode/owner/whitespace).  If the directive isn't found at all, no-op
    and warn (the conf may have been hand-edited or the LDAP block already
    removed; either way nothing left to carve out)."""
    if not os.path.isfile(httpd_conf):
        warn(f"{httpd_conf} not found; skipping LDAP carve-out")
        return False
    with open(httpd_conf) as fh:
        text = fh.read()
    m = _LDAP_LOC_RE.search(text)
    if not m:
        warn(f"no LDAP <LocationMatch> directive found in {httpd_conf} "
             f"(removed or hand-edited?); skipping carve-out for {env_name}")
        return False
    existing = set(m.group(2).split("|")) if m.group(2) else set()
    if env_name in existing:
        return False
    existing.add(env_name)
    envs_sorted = sorted(existing)
    if len(envs_sorted) == 1:
        carveout = "(?!" + envs_sorted[0] + "/)"
    else:
        carveout = "(?!(?:" + "|".join(envs_sorted) + ")/)"
    new_text = (text[:m.start()] + m.group(1) + carveout + m.group(3)
                + text[m.end():])
    if CTX.dry:
        log(f"[dry-run] extend LDAP <LocationMatch> carve-out in "
            f"{httpd_conf}: + {env_name}")
        _mark()
        return True
    st = os.stat(httpd_conf)
    d = os.path.dirname(httpd_conf)
    fd, tmp = tempfile.mkstemp(dir=d)
    with os.fdopen(fd, "w") as fh:
        fh.write(new_text)
    os.replace(tmp, httpd_conf)
    os.chmod(httpd_conf, stat.S_IMODE(st.st_mode))
    try:
        os.chown(httpd_conf, st.st_uid, st.st_gid)
    except PermissionError:
        pass
    log(f"extended LDAP <LocationMatch> carve-out in {httpd_conf}: "
        f"+ {env_name}  (now skipping: {', '.join(envs_sorted)})")
    _mark()
    return True


def _upgrade_trac_env(env_path):
    """Per-env Trac upgrade: legacy macro cleanup + trac.ini sanity.

    Today (items 1 & 2 of the original scope):
      * If any `<env>/plugins/{image,link}.py` is present AND the universal
        `hubzero_macros` plugin is installed (a Python on the host can
        `import hubzero_macros`), first ensure `[components] hubzero_macros.*
        = enabled` in `<env>/conf/trac.ini` (Trac would otherwise treat the
        system-wide entry-point plugin as disabled-by-default and the env
        would render `[[image …]]`/`[[link …]]` as "missing wiki" links once
        the per-env files are out of the way), then rename each legacy file
        to `<file>.disabled`.  If hubzero_macros isn't installed, just warn
        and leave the per-env files in place.
      * Read `<env>/conf/trac.ini` and warn on any `[components]` entry
        starting with `image.` or `link.` -- those mask the universal
        plugin's `hubzero_macros.image`/`.link` modules and want cleanup.

    Stage 2 (DB schema walk db29 -> db45, Py2-plugin quarantine across the
    walk) is still pending; it gates on a Py3.11 + Trac 1.6 install."""
    name = os.path.basename(env_path)
    step(f"Upgrade trac env: {name}")
    if not os.path.isdir(env_path):
        warn(f"  {env_path}: not a directory")
        return

    plugins_dir = os.path.join(env_path, "plugins")
    trac_ini    = os.path.join(env_path, "conf", "trac.ini")

    legacy_present = [
        legacy for legacy in ("image.py", "link.py")
        if os.path.isdir(plugins_dir)
           and os.path.isfile(os.path.join(plugins_dir, legacy))
    ]
    if legacy_present:
        if _macros_universal_installed():
            # Enable the system-wide plugin BEFORE disabling the per-env files,
            # so the env stays continuously functional through the transition
            # (no window of "missing wiki" rendering).
            _ensure_components_enabled(trac_ini, "hubzero_macros.*")
            for legacy in legacy_present:
                src = os.path.join(plugins_dir, legacy)
                _rename(src, src + ".disabled")
        else:
            for legacy in legacy_present:
                warn(f"  legacy {plugins_dir}/{legacy} present; install "
                     "hubzero-trac-macros first, then re-run upgrade-trac")

    if os.path.isfile(trac_ini):
        try:
            cp = configparser.RawConfigParser()
            cp.optionxform = str   # `[components]` keys are case-sensitive
            cp.read(trac_ini)
            if cp.has_section("components"):
                for opt, val in cp.items("components"):
                    if opt.startswith(("image.", "link.")):
                        warn(f"  {trac_ini} [components] {opt} = {val}  "
                             "(legacy per-env macro reference; safe to remove)")
        except (configparser.Error, OSError) as e:
            warn(f"  could not read {trac_ini}: {e}")
    log("  done")


def cmd_enable_cmsauth():
    """`hzforge enable-cmsauth ENV [ENV ...] [--install-wheel PATH]`

    Switch one or more Trac envs from Apache LDAP-Basic auth to HUBzero CMS
    SSO via the `hubzero-trac-cmsauth` plugin.  Per env:
      * `[components] hubzero_cmsauth.* = enabled` (+ `LoginModule = disabled`)
        added to `<env>/conf/trac.ini`
      * the LDAP `<LocationMatch>` directive in the trac drop-in extended to
        skip this env via the negative-lookahead carve-out

    `--install-wheel PATH` (optional, recommended for the first env on a
    fresh host) ALSO installs the cmsauth wheel into every available
    interpreter's site-packages before the per-env work.  Without it, the
    plugin is assumed to be installed system-wide already; an import check
    runs first and the subcommand dies with instructions if not.

    Calls `apply_changes()` at the end -- the LDAP carve-out is a vhost-
    scope conf edit, so Apache needs `apachectl configtest` + graceful
    reload to pick it up.  (A `touch /opt/trac/wsgi/hubtrac.wsgi` is not
    sufficient because it only triggers mod_wsgi daemon reloads, not
    vhost-conf re-parse.)"""
    tools_dir = OPT["trac_tools"][0]
    if not os.path.isdir(tools_dir):
        die(f"trac not configured (no {tools_dir})")
    all_envs = sorted(
        d for d in os.listdir(tools_dir)
        if os.path.isdir(os.path.join(tools_dir, d))
        and os.path.isfile(os.path.join(tools_dir, d, "conf", "trac.ini"))
    )
    requested = list(getattr(ARGS, "envs", None) or [])
    if not requested:
        die("usage: hzforge enable-cmsauth ENV [ENV ...]  "
            f"(envs available: {', '.join(all_envs) or '(none)'})")
    bad = [e for e in requested if e not in all_envs]
    if bad:
        die(f"unknown env(s): {', '.join(bad)}  "
            f"(have: {', '.join(all_envs) or '(none)'})")

    # 1. Optional wheel install.  Always before the import check, since the
    #    point is to make the import work.
    wheel = getattr(ARGS, "install_wheel", None)
    if wheel:
        if not os.path.isfile(wheel):
            die(f"--install-wheel: file not found: {wheel}")
        step(f"Install cmsauth wheel: {wheel}")
        pip_install_plugin_wheel(wheel)

    # 2. Import check (best-effort: try apache user if runuser is available,
    #    fall back to root-import which still verifies on-disk presence).
    step("Verify hubzero_cmsauth is importable")
    pythons = [py for py in ("python2", "python3") if _ok([py, "--version"])]
    if not pythons:
        die("no python2 or python3 found on the host")
    import_failures = []
    for py in pythons:
        # Try `runuser -u apache` first (covers the umask/perms issue the
        # plugin install must avoid); fall back to root import if runuser
        # isn't available or the apache user doesn't exist.
        probe = ([py, "-c", "import hubzero_cmsauth.api"]
                 if not _ok(["runuser", "-u", "apache", "--", py, "-c",
                             "import hubzero_cmsauth.api"])
                 else None)
        if probe is None:
            log(f"  {py}: apache can import hubzero_cmsauth.api")
            continue
        if _ok(probe):
            log(f"  {py}: root can import (but apache may not "
                "-- check umask/perms)")
            continue
        import_failures.append(py)
    if import_failures:
        die("hubzero_cmsauth not importable on "
            f"{', '.join(import_failures)}; "
            "install the wheel first (--install-wheel PATH or "
            "`sudo sh -c 'umask 022 && pipN install --no-deps "
            "/path/to/wheel'`)")

    # 3. Per-env trac.ini surgery.
    step(f"Enable cmsauth on {len(requested)} env(s): {','.join(requested)}")
    for env_name in requested:
        env_path = os.path.join(tools_dir, env_name)
        trac_ini = os.path.join(env_path, "conf", "trac.ini")
        log(f"  {env_name}")
        _ensure_components_enabled(trac_ini, "hubzero_cmsauth.*", "enabled")
        _ensure_components_enabled(trac_ini, "trac.web.auth.LoginModule", "disabled")

    # 4. LDAP carve-out (one block per host; extend it once per env).
    httpd_conf = dropin_path("trac")
    step(f"Extend LDAP <LocationMatch> carve-out in {httpd_conf}")
    for env_name in requested:
        _ldap_carveout_ensure(httpd_conf, env_name)

    # 5. apachectl configtest + graceful reload (CTX.config_changed was set
    #    by _ensure_components_enabled and _ldap_carveout_ensure).
    apply_changes()


def cmd_upgrade_trac():
    """`hzforge upgrade-trac [env ...]` -- run _upgrade_trac_env() per env.
    With no positional ENV args, runs across every env under /opt/trac/tools/
    (a subdir containing conf/trac.ini)."""
    tools_dir = OPT["trac_tools"][0]
    if not os.path.isdir(tools_dir):
        die(f"trac not configured (no {tools_dir})")
    all_envs = sorted(
        d for d in os.listdir(tools_dir)
        if os.path.isdir(os.path.join(tools_dir, d))
        and os.path.isfile(os.path.join(tools_dir, d, "conf", "trac.ini"))
    )
    requested = list(getattr(ARGS, "envs", None) or [])
    if requested:
        bad = [e for e in requested if e not in all_envs]
        if bad:
            die(f"unknown env(s): {', '.join(bad)}  "
                f"(have: {', '.join(all_envs) or '(none)'})")
        targets = requested
    else:
        targets = all_envs
    step(f"Upgrade {len(targets)} trac env(s): {','.join(targets) or '(none)'}")
    for env_name in targets:
        _upgrade_trac_env(os.path.join(tools_dir, env_name))
    # Anything _rename()d set CTX.config_changed; apply_changes() does the
    # graceful httpd reload so the mod_wsgi daemon picks up the per-env
    # plugin changes.  (No-op if nothing was actually moved.)
    apply_changes()


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
    pi.add_argument("--python", choices=list(PY.keys()),
                    default=INSTALL_DEFAULTS["python"], dest="python",
                    help="python+Trac matrix: py27 (Trac 1.0.x, pip mod_wsgi) "
                         "or py36 (Trac 1.6.x, dnf python3-mod_wsgi).  "
                         "py36 requires --trac-handler=mod_wsgi.")
    pi.add_argument("--trac-handler", choices=["mod_wsgi", "mod_python"],
                    default=INSTALL_DEFAULTS["trac_handler"], dest="trac_handler")
    pi.add_argument("--svn-source", choices=["wandisco", "appstream"],
                    default=INSTALL_DEFAULTS["svn_source"], dest="svn_source")
    # --trac-spec / --modwsgi-spec defaults are resolved in main() from
    # PY[args.python] AFTER argparsing (so an explicit value wins; otherwise
    # the python-version-appropriate default takes over).
    pi.add_argument("--trac-spec", default=None, dest="trac_spec",
                    help="override Trac pip spec (default: per --python -- "
                         f"py27={PY['py27']['trac_spec']!r}, "
                         f"py36={PY['py36']['trac_spec']!r})")
    pi.add_argument("--modwsgi-spec", default=None, dest="modwsgi_spec",
                    help="override mod_wsgi pip spec (py27 only; py36 uses the "
                         "python3-mod_wsgi RPM)")
    pi.add_argument("--ldap-url", dest="ldap_url")
    pi.add_argument("--ldap-binddn", dest="ldap_binddn")
    pi.add_argument("--ldap-bindpw", dest="ldap_bindpw",
                    help="LDAP bind password (exposed in the process list; "
                         "prefer --ldap-bindpw-file)")
    pi.add_argument("--ldap-bindpw-file", dest="ldap_bindpw_file",
                    help="read the LDAP bind password from this root-only file")
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
    pug = sub.add_parser("upgrade-trac", parents=[common],
                         help="per-env Trac upgrade (legacy macro cleanup + trac.ini sanity)")
    pug.add_argument("envs", nargs="*", metavar="ENV",
                     help="trac envs to upgrade (under /opt/trac/tools); default: all configured")

    pec = sub.add_parser("enable-cmsauth", parents=[common],
                         help="switch trac envs from LDAP to HUBzero CMS SSO")
    pec.add_argument("envs", nargs="*", metavar="ENV",
                     help="trac envs to switch (under /opt/trac/tools); REQUIRED, no default")
    pec.add_argument("--install-wheel", metavar="PATH", dest="install_wheel",
                     help="path to the hubzero_trac_cmsauth-*.whl to install "
                          "system-wide before enabling (recommended for the "
                          "first env on a fresh host; idempotent)")
    return p


def main():
    global CTX, ARGS
    parser = build_parser()
    args = parser.parse_args()
    if not getattr(args, "command", None):
        parser.print_help()                # bare `hzforge` or `--help` => top-level help
        sys.exit(0)

    # install-only options live on one subparser; backfill their defaults so the
    # other subcommands (which read them) have the attrs.
    for attr, default in INSTALL_DEFAULTS.items():
        if not hasattr(args, attr):
            setattr(args, attr, default)

    # Resolve python-version-dependent defaults AFTER argparsing -- an explicit
    # --trac-spec / --modwsgi-spec on the command line wins, otherwise pick the
    # appropriate default from the PY matrix.
    if args.trac_spec is None:
        args.trac_spec = PY[args.python]["trac_spec"]
    if args.modwsgi_spec is None:
        args.modwsgi_spec = PY[args.python]["modwsgi_pip_spec"]

    # Matrix validation: py36 + mod_python is rejected -- mod_python upstream
    # is Py2-only (last release 3.5.0, supports Py2.7 only).  py36 + mod_wsgi
    # uses the python3-mod_wsgi RPM; py27 + either handler is the legacy path.
    if args.python == "py36" and args.trac_handler == "mod_python":
        die("--python=py36 + --trac-handler=mod_python is not a valid combination "
            "(mod_python upstream is Python 2 only).  Use --trac-handler=mod_wsgi.")

    CTX = Ctx(args.dry)
    if os.geteuid() != 0:
        die("must run as root (sudo).")
    args.hub = args.hub or detect_hub() or "help"
    args.include_dir = f"/etc/httpd/{args.hub}.conf.d"
    # services come in as bare positional args (a list); allow comma-joined too
    services = []
    for tok in (getattr(args, "services", None) or []):   # positional on install/uninstall/doctor/repair/test
        services += [s for s in tok.split(",") if s]
    bad = [s for s in services if s not in ALL_SERVICES]
    if bad:
        die(f"unknown service(s): {', '.join(bad)} (valid: {', '.join(ALL_SERVICES)})")
    if args.command == "install" and not services:
        services = list(ALL_SERVICES)                  # install with no args = all
    if args.command == "uninstall" and not services:
        die("uninstall needs at least one service: " + " ".join(ALL_SERVICES))
    args.services = services
    # envs (positional on upgrade-trac); allow comma-joined like services.
    # Not validated against ALL_SERVICES -- envs are arbitrary subdirs of
    # /opt/trac/tools/ and the existence check happens in cmd_upgrade_trac().
    envs = []
    for tok in (getattr(args, "envs", None) or []):
        envs += [e for e in tok.split(",") if e]
    args.envs = envs
    ARGS = args

    print("=" * 72)
    dry = "  DRY-RUN" if args.dry else ""
    print(f" HUBzero Forge services [{args.command}]  hub={args.hub}{dry}")
    print("=" * 72)
    if not os.path.isdir(args.include_dir):
        die(f"include dir {args.include_dir} missing (right --hub?)")

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
    elif args.command == "upgrade-trac":
        cmd_upgrade_trac()
    elif args.command == "enable-cmsauth":
        cmd_enable_cmsauth()

    step("Done")
    for n in CTX.notes:
        print("[!] " + n)


if __name__ == "__main__":
    main()
