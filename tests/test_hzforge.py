"""Unit tests for hzforge's pure logic (no root / Apache / packages needed).

hzforge.py is imported as a module; only its side-effect-free helpers are
exercised here. Anything that would touch the system (dnf, systemctl, apachectl,
the filesystem under /etc or /opt) is either monkeypatched out or avoided.
"""
import importlib.util
import os
import pathlib
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent


def load_hz():
    spec = importlib.util.spec_from_file_location("hzforge", ROOT / "hzforge.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


hz = load_hz()


def make_args(**kw):
    a = types.SimpleNamespace(
        include_dir="/etc/httpd/help.conf.d",
        trac_handler="mod_wsgi",
        python="py27",                          # default matrix: py27
        ldap_url=None, ldap_binddn=None, ldap_bindpw=None, ldap_bindpw_file=None,
        services=[],
    )
    for k, v in kw.items():
        setattr(a, k, v)
    return a


def render(monkeypatch, svc, **kw):
    """Render one service drop-in, capturing the written content."""
    cap = {}
    monkeypatch.setattr(hz, "ARGS", make_args(**kw), raising=False)
    monkeypatch.setattr(hz, "CTX", hz.Ctx(False), raising=False)
    monkeypatch.setattr(hz, "write_file",
                        lambda p, c, mode, owner="root", group="root": cap.__setitem__("c", c) or True)
    monkeypatch.setattr(hz, "makedir", lambda *a, **k: None)
    monkeypatch.setattr(hz, "_trac_envs", lambda: ["histogram"])
    hz.write_service_conf(svc)
    return cap["c"]


def test_all_services():
    assert hz.ALL_SERVICES == ["svn", "git", "gitExternal", "trac"]


def test_dropin_prefix_is_forge():
    assert hz.DROPIN_PREFIX == "00-forge-"


def test_dropin_path(monkeypatch):
    monkeypatch.setattr(hz, "ARGS", make_args(include_dir="/etc/httpd/help.conf.d"), raising=False)
    assert hz.dropin_path("trac") == "/etc/httpd/help.conf.d/00-forge-trac.conf"


def test_svn_dropin(monkeypatch):
    c = render(monkeypatch, "svn")
    assert 'RewriteRule "^/tools/[^/]+/svn(/|$)" - [END]' in c   # <Location> shield
    assert "IncludeOptional /etc/httpd/help.conf.d/svn/svn.conf" in c


def test_git_dropin(monkeypatch):
    c = render(monkeypatch, "git")
    assert "IncludeOptional /etc/httpd/help.conf.d/git/git.conf" in c
    assert "gitExternal" not in c          # git and gitExternal are separate files


def test_gitexternal_dropin(monkeypatch):
    c = render(monkeypatch, "gitExternal")
    assert "IncludeOptional /etc/httpd/help.conf.d/git/gitExternal.conf" in c


def test_trac_modwsgi_dropin(monkeypatch):
    c = render(monkeypatch, "trac", trac_handler="mod_wsgi")
    assert "WSGIDaemonProcess trac" in c
    assert "WSGIScriptAliasMatch" in c
    assert "/opt/trac/wsgi/hubtrac.wsgi" in c
    assert "modpython_frontend" not in c
    # alias self-diverts -> no [END] carve-out for trac under mod_wsgi
    assert "[END]" not in c
    for verb in ("wiki", "timeline", "browser", "ticket", "newticket", "query"):
        assert verb in c


def test_trac_modpython_dropin(monkeypatch):
    c = render(monkeypatch, "trac", trac_handler="mod_python")
    assert "trac.web.modpython_frontend" in c
    assert "<Location /tools/histogram>" in c
    assert "TracEnv /opt/trac/tools/histogram" in c
    assert "[END]" in c                    # <Location> needs the verb shield
    assert "WSGIScriptAliasMatch" not in c


def test_detect_services_and_handler(tmp_path, monkeypatch):
    monkeypatch.setattr(hz, "ARGS", make_args(include_dir=str(tmp_path)), raising=False)
    assert hz.detect_configured_services() == []
    assert hz.detect_handler() is None
    (tmp_path / "00-forge-svn.conf").write_text("# svn\nIncludeOptional x/svn/svn.conf\n")
    (tmp_path / "00-forge-trac.conf").write_text("WSGIScriptAliasMatch ... hubtrac.wsgi\n")
    assert set(hz.detect_configured_services()) == {"svn", "trac"}
    assert hz.detect_handler() == "mod_wsgi"


def test_detect_handler_modpython(tmp_path, monkeypatch):
    monkeypatch.setattr(hz, "ARGS", make_args(include_dir=str(tmp_path)), raising=False)
    (tmp_path / "00-forge-trac.conf").write_text("PythonHandler trac.web.modpython_frontend\n")
    assert hz.detect_handler() == "mod_python"


def test_detect_legacy_trac_conf(tmp_path, monkeypatch):
    monkeypatch.setattr(hz, "ARGS", make_args(include_dir=str(tmp_path)), raising=False)
    (tmp_path / "trac.conf").write_text("WSGIScriptAliasMatch ...\n")   # hand-made, no 00-forge-
    assert "trac" in hz.detect_configured_services()


@pytest.mark.parametrize("mode,expected", [(0o600, False), (0o640, False), (0o644, True), (0o755, True)])
def test_other_can_read_file(tmp_path, mode, expected):
    f = tmp_path / "f"
    f.write_text("x")
    os.chmod(str(f), mode)
    assert hz._other_can_read(str(f)) is expected


def test_other_can_read_dir(tmp_path):
    d = tmp_path / "d"
    d.mkdir()
    os.chmod(str(d), 0o700)
    assert hz._other_can_read(str(d)) is False
    os.chmod(str(d), 0o755)
    assert hz._other_can_read(str(d)) is True


def test_ldap_bindpw_file_wins_and_strips(tmp_path, monkeypatch):
    monkeypatch.setattr(hz, "CTX", hz.Ctx(False), raising=False)
    f = tmp_path / "pw"
    f.write_text("s3cret\n")
    os.chmod(str(f), 0o600)
    # inline also set: the file must take precedence
    monkeypatch.setattr(hz, "ARGS",
                        make_args(ldap_bindpw="inline", ldap_bindpw_file=str(f)), raising=False)
    assert hz._ldap_bindpw() == "s3cret"      # trailing newline stripped, file wins
    assert hz.CTX.notes == []                 # 0600 -> no perms warning


def test_ldap_bindpw_file_loose_perms_warns(tmp_path, monkeypatch):
    monkeypatch.setattr(hz, "CTX", hz.Ctx(False), raising=False)
    f = tmp_path / "pw"
    f.write_text("s3cret")
    os.chmod(str(f), 0o644)
    monkeypatch.setattr(hz, "ARGS", make_args(ldap_bindpw_file=str(f)), raising=False)
    hz._ldap_bindpw()
    assert any("chmod 600" in n for n in hz.CTX.notes)


def test_ldap_bindpw_inline_warns(monkeypatch):
    monkeypatch.setattr(hz, "CTX", hz.Ctx(False), raising=False)
    monkeypatch.setattr(hz, "ARGS", make_args(ldap_bindpw="pw"), raising=False)
    assert hz._ldap_bindpw() == "pw"
    assert any("process list" in n for n in hz.CTX.notes)


@pytest.mark.parametrize("spec,expected", [
    ("Trac==1.0.14",          "1.0.14"),
    ("  Trac == 1.0.14  ",    "1.0.14"),    # whitespace tolerated
    ("trac==1.0.14",          "1.0.14"),    # case-insensitive
    ("Trac>=1.0,<1.1",        None),         # range -> no exact version
    ("Trac",                  None),         # bare name
    ("Trac==1.0.14,<1.1",     None),         # not a pure pin
])
def test_trac_spec_exact_version(spec, expected, monkeypatch):
    monkeypatch.setattr(hz, "ARGS", make_args(trac_spec=spec), raising=False)
    assert hz._trac_spec_exact_version() == expected


# --- upgrade-trac: _macros_universal_installed + _upgrade_trac_env ---

def test_macros_universal_installed_true_if_any_python_can_import(monkeypatch):
    monkeypatch.setattr(hz, "_ok", lambda cmd: cmd[0] == "python2")
    assert hz._macros_universal_installed() is True


def test_macros_universal_installed_false_when_neither_python_can_import(monkeypatch):
    monkeypatch.setattr(hz, "_ok", lambda cmd: False)
    assert hz._macros_universal_installed() is False


def test_upgrade_trac_env_warns_about_legacy_macros_when_universal_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(hz, "CTX", hz.Ctx(False), raising=False)
    monkeypatch.setattr(hz, "_macros_universal_installed", lambda: False)
    env = tmp_path / "histogram"
    plugins = env / "plugins"
    plugins.mkdir(parents=True)
    (plugins / "image.py").write_text("# legacy image macro")
    (plugins / "link.py").write_text("# legacy link macro")
    hz._upgrade_trac_env(str(env))
    # universal missing -> files NOT moved
    assert (plugins / "image.py").is_file()
    assert (plugins / "link.py").is_file()
    # ... but warnings name both, and reference the hubzero-trac-macros install:
    notes_text = "\n".join(hz.CTX.notes)
    assert "image.py" in notes_text
    assert "link.py"  in notes_text
    assert "install hubzero-trac-macros" in notes_text


def test_upgrade_trac_env_disables_legacy_macros_when_universal_present(tmp_path, monkeypatch):
    monkeypatch.setattr(hz, "CTX", hz.Ctx(False), raising=False)
    monkeypatch.setattr(hz, "_macros_universal_installed", lambda: True)
    env = tmp_path / "wiki"
    plugins = env / "plugins"
    plugins.mkdir(parents=True)
    (plugins / "image.py").write_text("# legacy image macro")
    (plugins / "link.py").write_text("# legacy link macro")
    hz._upgrade_trac_env(str(env))
    # originals moved aside -> .disabled siblings remain
    assert not (plugins / "image.py").exists()
    assert     (plugins / "image.py.disabled").is_file()
    assert not (plugins / "link.py").exists()
    assert     (plugins / "link.py.disabled").is_file()


def test_upgrade_trac_env_dry_run_does_not_move_files(tmp_path, monkeypatch):
    monkeypatch.setattr(hz, "CTX", hz.Ctx(True), raising=False)   # dry=True
    monkeypatch.setattr(hz, "_macros_universal_installed", lambda: True)
    env = tmp_path / "x"
    plugins = env / "plugins"
    plugins.mkdir(parents=True)
    (plugins / "image.py").write_text("# legacy")
    hz._upgrade_trac_env(str(env))
    assert (plugins / "image.py").is_file()                 # still here
    assert not (plugins / "image.py.disabled").exists()     # not moved


def test_upgrade_trac_env_warns_about_stale_components_in_trac_ini(tmp_path, monkeypatch):
    monkeypatch.setattr(hz, "CTX", hz.Ctx(False), raising=False)
    monkeypatch.setattr(hz, "_macros_universal_installed", lambda: True)
    env = tmp_path / "histogram"
    conf = env / "conf"
    conf.mkdir(parents=True)
    (conf / "trac.ini").write_text(
        "[components]\n"
        "image.* = enabled\n"
        "link.linkMacro = enabled\n"
        "trac.* = enabled\n"
    )
    hz._upgrade_trac_env(str(env))
    notes_text = "\n".join(hz.CTX.notes)
    assert "image.*"        in notes_text
    assert "link.linkMacro" in notes_text
    assert "trac.*"     not in notes_text    # non-legacy entry not flagged


def test_upgrade_trac_env_handles_missing_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(hz, "CTX", hz.Ctx(False), raising=False)
    monkeypatch.setattr(hz, "_macros_universal_installed", lambda: False)
    hz._upgrade_trac_env(str(tmp_path / "nonexistent"))
    assert any("not a directory" in n for n in hz.CTX.notes)


# --- _ensure_components_enabled (text-surgery patcher for trac.ini) ---

def test_ensure_components_enabled_inserts_into_existing_section(tmp_path, monkeypatch):
    monkeypatch.setattr(hz, "CTX", hz.Ctx(False), raising=False)
    ini = tmp_path / "trac.ini"
    ini.write_text(
        "[trac]\nsecure_cookies = True\n\n"
        "[components]\nhubzeroplugin.* = enabled\n\n"
        "[wiki]\n"
    )
    changed = hz._ensure_components_enabled(str(ini), "hubzero_macros.*")
    assert changed is True
    body = ini.read_text()
    assert "hubzero_macros.* = enabled" in body
    # inserted right after [components] header -- before the existing entry
    assert body.index("hubzero_macros.* = enabled") < body.index("hubzeroplugin.* = enabled")
    # other sections untouched, comments-or-whitespace preserved
    assert "[trac]"          in body
    assert "secure_cookies"  in body
    assert "[wiki]"          in body


def test_ensure_components_enabled_appends_new_section_when_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(hz, "CTX", hz.Ctx(False), raising=False)
    ini = tmp_path / "trac.ini"
    ini.write_text("[trac]\nsecure_cookies = True\n")
    changed = hz._ensure_components_enabled(str(ini), "hubzero_macros.*")
    assert changed is True
    body = ini.read_text()
    assert "[components]"               in body
    assert "hubzero_macros.* = enabled" in body
    # original content preserved
    assert body.startswith("[trac]\nsecure_cookies = True\n")


def test_ensure_components_enabled_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(hz, "CTX", hz.Ctx(False), raising=False)
    ini = tmp_path / "trac.ini"
    ini.write_text("[components]\nhubzero_macros.* = enabled\n")
    changed = hz._ensure_components_enabled(str(ini), "hubzero_macros.*")
    assert changed is False
    # exactly one occurrence -- no duplicate inserted
    assert ini.read_text().count("hubzero_macros.* = enabled") == 1


def test_ensure_components_enabled_dry_run(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(hz, "CTX", hz.Ctx(True), raising=False)   # dry=True
    ini = tmp_path / "trac.ini"
    original = "[trac]\nsecure_cookies = True\n"
    ini.write_text(original)
    changed = hz._ensure_components_enabled(str(ini), "hubzero_macros.*")
    assert changed is True
    assert ini.read_text() == original            # unwritten under --dry-run
    assert "[dry-run]" in capsys.readouterr().out  # log() goes to stdout


def test_ensure_components_enabled_returns_false_for_missing_file(tmp_path, monkeypatch):
    monkeypatch.setattr(hz, "CTX", hz.Ctx(False), raising=False)
    assert hz._ensure_components_enabled(str(tmp_path / "no-such.ini"),
                                         "hubzero_macros.*") is False


# --- _ldap_carveout_ensure (extends the negative lookahead in 00-forge-trac.conf) ---

_LDAP_CONF_NO_CARVEOUT = (
    '# managed by hzforge\n'
    'WSGIDaemonProcess trac processes=1 threads=30\n'
    '<LocationMatch "^/tools/[^/]+/login">\n'
    '    AuthType Basic\n'
    '    AuthBasicProvider ldap\n'
    '</LocationMatch>\n'
)

_LDAP_CONF_SINGLE = (
    '<LocationMatch "^/tools/(?!hzforgetest/)[^/]+/login">\n'
    '    AuthType Basic\n'
    '</LocationMatch>\n'
)

_LDAP_CONF_MULTI = (
    '<LocationMatch "^/tools/(?!(?:bio3d|hzforgetest)/)[^/]+/login">\n'
    '    AuthType Basic\n'
    '</LocationMatch>\n'
)


def test_ldap_carveout_promotes_no_carveout_to_single(tmp_path, monkeypatch):
    """First env added: rewrite the block from `^/tools/[^/]+/login` to
    `^/tools/(?!hzforgetest/)[^/]+/login`.  Uses the single-form syntax
    (one env, no `(?:...)` group)."""
    monkeypatch.setattr(hz, "CTX", hz.Ctx(False), raising=False)
    conf = tmp_path / "00-forge-trac.conf"
    conf.write_text(_LDAP_CONF_NO_CARVEOUT)
    changed = hz._ldap_carveout_ensure(str(conf), "hzforgetest")
    assert changed is True
    body = conf.read_text()
    assert '<LocationMatch "^/tools/(?!hzforgetest/)[^/]+/login">' in body
    # other lines preserved verbatim
    assert "WSGIDaemonProcess trac processes=1 threads=30" in body
    assert "AuthBasicProvider ldap" in body


def test_ldap_carveout_promotes_single_to_multiple(tmp_path, monkeypatch):
    """Adding a 2nd env: rewrite from `(?!FOO/)` to `(?!(?:BAR|FOO)/)`
    (with the env names sorted alphabetically for a deterministic diff)."""
    monkeypatch.setattr(hz, "CTX", hz.Ctx(False), raising=False)
    conf = tmp_path / "00-forge-trac.conf"
    conf.write_text(_LDAP_CONF_SINGLE)
    changed = hz._ldap_carveout_ensure(str(conf), "bio3d")
    assert changed is True
    body = conf.read_text()
    # sorted alphabetically: bio3d before hzforgetest
    assert '<LocationMatch "^/tools/(?!(?:bio3d|hzforgetest)/)[^/]+/login">' in body


def test_ldap_carveout_extends_multiple(tmp_path, monkeypatch):
    """Nth env added: insert into the existing `(?:...)` group, re-sort."""
    monkeypatch.setattr(hz, "CTX", hz.Ctx(False), raising=False)
    conf = tmp_path / "00-forge-trac.conf"
    conf.write_text(_LDAP_CONF_MULTI)
    changed = hz._ldap_carveout_ensure(str(conf), "calc")
    assert changed is True
    assert ('<LocationMatch "^/tools/(?!(?:bio3d|calc|hzforgetest)/)'
            '[^/]+/login">') in conf.read_text()


def test_ldap_carveout_is_idempotent_for_already_carved_env(tmp_path, monkeypatch):
    """Re-running with the same env name: no-op, file untouched."""
    monkeypatch.setattr(hz, "CTX", hz.Ctx(False), raising=False)
    conf = tmp_path / "00-forge-trac.conf"
    conf.write_text(_LDAP_CONF_SINGLE)
    before = conf.read_text()
    changed = hz._ldap_carveout_ensure(str(conf), "hzforgetest")
    assert changed is False
    assert conf.read_text() == before     # byte-for-byte unchanged


def test_ldap_carveout_dry_run(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(hz, "CTX", hz.Ctx(True), raising=False)
    conf = tmp_path / "00-forge-trac.conf"
    conf.write_text(_LDAP_CONF_NO_CARVEOUT)
    changed = hz._ldap_carveout_ensure(str(conf), "hzforgetest")
    assert changed is True
    assert conf.read_text() == _LDAP_CONF_NO_CARVEOUT    # unwritten
    assert "[dry-run]" in capsys.readouterr().out


def test_ldap_carveout_skips_when_locationmatch_absent(tmp_path, monkeypatch, capsys):
    """The trac drop-in may have had the LDAP block removed entirely
    (final-state cutover).  No-op + warn, no error."""
    monkeypatch.setattr(hz, "CTX", hz.Ctx(False), raising=False)
    conf = tmp_path / "00-forge-trac.conf"
    conf.write_text("# no LocationMatch here\nWSGIDaemonProcess trac\n")
    changed = hz._ldap_carveout_ensure(str(conf), "hzforgetest")
    assert changed is False
    assert "no LDAP <LocationMatch>" in capsys.readouterr().out


def test_ldap_carveout_skips_missing_file(tmp_path, monkeypatch):
    """No conf file -> no-op + warn (don't blow up; just nothing to do)."""
    monkeypatch.setattr(hz, "CTX", hz.Ctx(False), raising=False)
    assert hz._ldap_carveout_ensure(str(tmp_path / "no-such.conf"),
                                    "hzforgetest") is False


def test_upgrade_trac_env_enables_hubzero_macros_before_disabling_legacy(tmp_path, monkeypatch):
    """The whole reason _ensure_components_enabled exists: without the enable,
    Trac would render `[[image …]]` as a "missing wiki" link once the per-env
    image.py is renamed to .disabled.  This test locks in that the enable lands
    before the rename, so the env stays continuously functional."""
    monkeypatch.setattr(hz, "CTX", hz.Ctx(False), raising=False)
    monkeypatch.setattr(hz, "_macros_universal_installed", lambda: True)
    env = tmp_path / "histogram"
    (env / "plugins").mkdir(parents=True)
    (env / "conf").mkdir()
    (env / "plugins" / "image.py").write_text("# legacy image macro")
    (env / "plugins" / "link.py").write_text("# legacy link macro")
    (env / "conf" / "trac.ini").write_text("[trac]\nsecure_cookies = True\n")
    hz._upgrade_trac_env(str(env))
    body = (env / "conf" / "trac.ini").read_text()
    assert "[components]"               in body
    assert "hubzero_macros.* = enabled" in body
    # legacy disabled too -- both halves of the transition land
    assert not (env / "plugins" / "image.py").exists()
    assert     (env / "plugins" / "image.py.disabled").is_file()


def test_upgrade_trac_env_no_legacy_no_trac_ini_change(tmp_path, monkeypatch):
    """If there's no per-env legacy to disable, don't touch trac.ini -- the
    env never asked for the system-wide macros and we shouldn't surprise it
    by activating them out of the blue."""
    monkeypatch.setattr(hz, "CTX", hz.Ctx(False), raising=False)
    monkeypatch.setattr(hz, "_macros_universal_installed", lambda: True)
    env = tmp_path / "quiet"
    (env / "plugins").mkdir(parents=True)
    (env / "conf").mkdir()
    original_ini = "[trac]\nsecure_cookies = True\n"
    (env / "conf" / "trac.ini").write_text(original_ini)
    hz._upgrade_trac_env(str(env))
    assert (env / "conf" / "trac.ini").read_text() == original_ini


def test_upgrade_trac_env_universal_missing_does_not_touch_trac_ini(tmp_path, monkeypatch):
    """Legacy present + universal missing -> warn only; don't enable the
    system-wide plugin (it isn't installed) and don't disable the per-env
    files (they're still the env's only source of macros)."""
    monkeypatch.setattr(hz, "CTX", hz.Ctx(False), raising=False)
    monkeypatch.setattr(hz, "_macros_universal_installed", lambda: False)
    env = tmp_path / "legacy_only"
    (env / "plugins").mkdir(parents=True)
    (env / "conf").mkdir()
    (env / "plugins" / "image.py").write_text("# legacy")
    original_ini = "[trac]\nsecure_cookies = True\n"
    (env / "conf" / "trac.ini").write_text(original_ini)
    hz._upgrade_trac_env(str(env))
    assert (env / "plugins" / "image.py").is_file()              # not disabled
    assert (env / "conf" / "trac.ini").read_text() == original_ini  # untouched


# --- python/handler matrix (PY dict + _py/_pip/_modwsgi_conf_path helpers) ---

def test_py_dict_has_three_valid_matrices():
    """Three valid (python, handler) combos:
    py27+mod_python (legacy), py27+mod_wsgi (current), py36+mod_wsgi (stage 2).
    The 4th (py36+mod_python) is rejected at argparse time, not in PY."""
    assert set(hz.PY.keys()) == {"py27", "py36"}
    # py27: pip-install mod_wsgi (last Py2-capable: 4.9.4)
    assert hz.PY["py27"]["py"]  == "python2"
    assert hz.PY["py27"]["pip"] == "pip2"
    assert hz.PY["py27"]["trac_spec"]      == "Trac==1.0.14"
    assert hz.PY["py27"]["modwsgi_source"] == "pip"
    assert hz.PY["py27"]["modwsgi_pip_spec"].startswith("mod_wsgi==")
    assert hz.PY["py27"]["modwsgi_rpm"]    is None
    # py36: dnf install python3-mod_wsgi (Rocky 8 AppStream; ships its own conf)
    assert hz.PY["py36"]["py"]  == "python3"
    assert hz.PY["py36"]["pip"] == "pip3"
    assert hz.PY["py36"]["trac_spec"]      == "Trac>=1.6,<1.7"
    assert hz.PY["py36"]["modwsgi_source"] == "rpm"
    assert hz.PY["py36"]["modwsgi_pip_spec"] is None
    assert hz.PY["py36"]["modwsgi_rpm"]    == "python3-mod_wsgi"
    # py27's mod_wsgi RPM conf is OUR file; py36's is the AppStream RPM's file.
    assert hz.PY["py27"]["modwsgi_conf"].endswith("/10-wsgi.conf")
    assert hz.PY["py36"]["modwsgi_conf"].endswith("/10-wsgi-python3.conf")
    # py27 needs the C toolchain to build mod_wsgi from source; py36 doesn't.
    assert "python2-devel" in hz.PY["py27"]["build_deps"]
    assert hz.PY["py36"]["build_deps"] == []


def test_py_helper_returns_correct_interpreter(monkeypatch):
    """_py() reads ARGS.python and returns the right binary name."""
    monkeypatch.setattr(hz, "ARGS", make_args(python="py27"), raising=False)
    assert hz._py() == "python2"
    assert hz._pip() == "pip2"
    monkeypatch.setattr(hz, "ARGS", make_args(python="py36"), raising=False)
    assert hz._py() == "python3"
    assert hz._pip() == "pip3"


def test_modwsgi_conf_path_is_per_python(monkeypatch):
    """py27 -> hzforge-written 10-wsgi.conf.  py36 -> AppStream RPM's
    10-wsgi-python3.conf (we read from it but never write to it)."""
    monkeypatch.setattr(hz, "ARGS", make_args(python="py27"), raising=False)
    assert hz._modwsgi_conf_path().endswith("/10-wsgi.conf")
    monkeypatch.setattr(hz, "ARGS", make_args(python="py36"), raising=False)
    assert hz._modwsgi_conf_path().endswith("/10-wsgi-python3.conf")


def test_install_defaults_trac_spec_unset(monkeypatch):
    """INSTALL_DEFAULTS deliberately stores trac_spec/modwsgi_spec as None so
    main() can resolve them from PY[args.python] AFTER parsing.  This locks in
    the contract: argparse default is None, not a hardcoded value."""
    assert hz.INSTALL_DEFAULTS["trac_spec"]    is None
    assert hz.INSTALL_DEFAULTS["modwsgi_spec"] is None
    assert hz.INSTALL_DEFAULTS["python"]       == "py27"   # back-compat default


def test_argparse_resolves_trac_spec_from_python_choice():
    """End-to-end via build_parser(): when --trac-spec isn't passed,
    main()'s post-parse resolution would pick the python-appropriate
    default from PY.  Verify the argparse layer leaves trac_spec=None for
    main() to fill in."""
    parser = hz.build_parser()
    # --python py27 (no explicit trac-spec)
    args = parser.parse_args(["install", "--python", "py27", "trac"])
    assert args.python == "py27"
    assert args.trac_spec    is None
    assert args.modwsgi_spec is None
    # --python py36
    args = parser.parse_args(["install", "--python", "py36", "trac"])
    assert args.python == "py36"
    assert args.trac_spec    is None
    # --trac-spec explicit always wins
    args = parser.parse_args(["install", "--python", "py36",
                              "--trac-spec", "Trac==1.4.4", "trac"])
    assert args.trac_spec == "Trac==1.4.4"


def test_pip_install_uses_chosen_pip(monkeypatch):
    """pip_install() shells out to ARGS.pip (pip2 for py27, pip3 for py36)."""
    monkeypatch.setattr(hz, "ARGS", make_args(python="py27"), raising=False)
    captured = []
    monkeypatch.setattr(hz, "run", lambda cmd, **kw: captured.append(list(cmd)))
    hz.pip_install("Trac==1.0.14")
    # The shell command is ["sh", "-c", "<script>", "sh", pip, spec]
    assert captured[0][:3] == ["sh", "-c", 'umask 022; exec "$1" install "$2"']
    assert captured[0][4] == "pip2"
    assert captured[0][5] == "Trac==1.0.14"
    # Switch to py36 and re-check
    captured.clear()
    monkeypatch.setattr(hz, "ARGS", make_args(python="py36"), raising=False)
    hz.pip_install("Trac>=1.6,<1.7")
    assert captured[0][4] == "pip3"
    assert captured[0][5] == "Trac>=1.6,<1.7"


def test_py_can_import_defaults_to_chosen_python(monkeypatch):
    """py_can_import(mod) uses _py() unless explicitly overridden -- so
    `import trac` is probed via python3 under --python=py36."""
    monkeypatch.setattr(hz, "ARGS", make_args(python="py36"), raising=False)
    captured = []
    monkeypatch.setattr(hz, "_ok", lambda cmd: captured.append(list(cmd)) or True)
    hz.py_can_import("trac")
    assert captured[0] == ["python3", "-c", "import trac"]
    # Explicit override still works (used by cmd_test which probes both):
    captured.clear()
    hz.py_can_import("trac", py="python2")
    assert captured[0] == ["python2", "-c", "import trac"]
