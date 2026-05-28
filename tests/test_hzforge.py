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
