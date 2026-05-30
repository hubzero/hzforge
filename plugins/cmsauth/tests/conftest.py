"""Test fixtures for hubzero-trac-cmsauth.

Tests cover the parts WE wrote -- cookie extraction, the CMS API call,
the redirect URL construction.  We deliberately do NOT test the parent
class behavior (auth_cookie INSERT/DELETE, trac_auth cookie attributes,
the post-action redirect logic) -- those belong to Trac's
trac.web.auth.LoginModule and are covered by the canary integration
tests on the help host.

This means our stubs for Trac are intentionally minimal -- just enough
to let `from trac.web.auth import LoginModule` and `from trac.config
import Option, IntOption` succeed at import time.
"""
from __future__ import absolute_import, division, print_function, unicode_literals

import os
import sys
import types


# --- src/ on sys.path (so the plugin source is importable regardless of
# pytest's rootdir).  Use os.path rather than pathlib so this conftest stays
# Py2-compatible (pathlib is Py3.4+ stdlib).
# ---
sys.path.insert(0, os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "src")))


# --- Minimal Trac stubs (only what's needed at api.py import time + for
# the override methods to instantiate without real Trac) ---

for _mod in ("trac", "trac.core", "trac.config",
             "trac.web", "trac.web.api", "trac.web.auth",
             "trac.web.chrome", "trac.util", "trac.util.html"):
    sys.modules.setdefault(str(_mod), types.ModuleType(str(_mod)))


# trac.config Option descriptors: return the default when accessed on an
# instance, so `self.api_path` evaluates to "/api/v1.1/members/currentuser"
# without a real Trac component framework.
class _ConfigOption(object):
    def __init__(self, section, key, default, doc=""):
        self.section, self.key, self.default, self.doc = section, key, default, doc
    def __get__(self, instance, owner=None):
        return self if instance is None else self.default


sys.modules["trac.config"].Option     = _ConfigOption
sys.modules["trac.config"].BoolOption = _ConfigOption
sys.modules["trac.config"].IntOption  = _ConfigOption


# trac.web.auth.LoginModule: real Trac's version is a complex Component with
# its own IAuthenticator / IRequestHandler / INavigationContributor surface.
# In tests we only care that our override methods can call super() without
# blowing up.  This stub records which super-method was called.
class _LoginModuleStub(object):
    """Minimal stub for trac.web.auth.LoginModule.  Tests can inspect
    `req._super_calls` to assert that our override delegated to the parent."""

    def __init__(self, *a, **kw):
        pass

    @staticmethod
    def _record(req, name):
        if not hasattr(req, "_super_calls"):
            req._super_calls = []
        req._super_calls.append(name)

    def authenticate(self, req):
        _LoginModuleStub._record(req, "authenticate")
        # Real Trac's behavior: if REMOTE_USER set -> return it; elif
        # trac_auth cookie -> look up DB.  In tests, just honor REMOTE_USER.
        return (req.environ.get("REMOTE_USER") if req.environ else None)

    def _do_login(self, req):
        _LoginModuleStub._record(req, "_do_login")
        # Pretend to INSERT auth_cookie + set trac_auth (real Trac does)

    def _do_logout(self, req):
        _LoginModuleStub._record(req, "_do_logout")

    def _expire_cookie(self, req):
        _LoginModuleStub._record(req, "_expire_cookie")

    def _redirect_back(self, req):
        _LoginModuleStub._record(req, "_redirect_back")


sys.modules["trac.web.auth"].LoginModule = _LoginModuleStub


import pytest   # noqa: E402  (must follow the trac stubs above)


# --- fake Trac request -------------------------------------------------- #

class _FakeHref(object):
    def __init__(self, base):
        self._base = base.rstrip("/")
    def wiki(self, *parts):
        return self._base + "/wiki" + ("/" + "/".join(parts) if parts else "")
    def login(self):
        return self._base + "/login"
    def logout(self):
        return self._base + "/logout"
    def __call__(self):
        return self._base


class _RedirectDone(Exception):
    """Mimics Trac's RequestDone raised by req.redirect()."""
    def __init__(self, url):
        super(_RedirectDone, self).__init__(url)
        self.url = url


class _FakeReq(object):
    def __init__(self, path_info="/", environ=None, incookie=None,
                 args=None, remote_user=None, authname="anonymous",
                 abs_base="https://help.hubzero.org/tools/hzforgetest"):
        self.path_info = path_info
        self.environ   = environ or {}
        self.incookie  = incookie or {}
        # Real Trac: req.outcookie is a Cookie.SimpleCookie / http.cookies
        # SimpleCookie -- assigning `c["name"] = value` creates a Morsel
        # whose attributes (path, secure, httponly, max-age, ...) can be
        # set via `c["name"]["path"] = "/x"`.  Use the real SimpleCookie so
        # tests exercise the same morsel API the production code expects.
        try:                                          # Py3
            from http.cookies import SimpleCookie
        except ImportError:                           # Py2
            from Cookie import SimpleCookie
        self.outcookie = SimpleCookie()
        self.args      = args or {}
        self.authname  = authname
        self.abs_href  = _FakeHref(abs_base)
        # path-only href (mimics Trac's req.href: relative paths)
        idx = abs_base.find("/", abs_base.find("//") + 2)
        self.href      = _FakeHref(abs_base[idx:]) if idx >= 0 else _FakeHref("/")
        self.redirected_to = None
    @property
    def remote_user(self):
        # Real Trac: req.remote_user is an alias for environ['REMOTE_USER'].
        # The fake mirrors that so writes via `req.environ['REMOTE_USER'] = ...`
        # become readable via `req.remote_user` (which is how our _do_login
        # checks whether the user is now authed after the CMS-API path).
        return self.environ.get("REMOTE_USER")
    def redirect(self, url):
        self.redirected_to = url
        raise _RedirectDone(url)


@pytest.fixture
def make_req():
    """Factory: make_req(path_info='/', cookie='', remote_user=None, ...)"""
    def _make(path_info="/", cookie="", remote_user=None,
              abs_base="https://help.hubzero.org/tools/hzforgetest",
              authname=None, incookie=None, args=None):
        environ = {
            "wsgi.url_scheme": "https",
            "HTTP_HOST":       "help.hubzero.org",
            "REMOTE_ADDR":     "203.0.113.5",
        }
        if cookie:
            environ["HTTP_COOKIE"] = cookie
        if remote_user:
            environ["REMOTE_USER"] = remote_user
        # Default authname: nkissebe if remote_user, else 'anonymous'
        if authname is None:
            authname = remote_user or "anonymous"
        return _FakeReq(path_info=path_info, environ=environ,
                        incookie=incookie or {}, args=args or {},
                        remote_user=remote_user, authname=authname,
                        abs_base=abs_base)
    return _make


@pytest.fixture
def RedirectDone():
    return _RedirectDone


@pytest.fixture
def env():
    """A minimal env stand-in.  Records DB calls without pretending to run
    SQL.  Tests stage rows for db_query via `env.db_query_responses`
    (FIFO list of result-sets); tests inspect db_transaction calls via
    `env.db_transactions`."""
    class _Log(object):
        def __init__(self): self.records = []
        def debug(self,   *a): self.records.append(('debug',   a))
        def info(self,    *a): self.records.append(('info',    a))
        def warning(self, *a): self.records.append(('warning', a))
    class _Env(object):
        def __init__(self):
            self.log = _Log()
            self.db_transactions = []        # list of (sql, params)
            self.db_queries      = []        # list of (sql, params)
            self.db_query_responses = []     # FIFO: next call returns first list
        def db_transaction(self, sql, params=None):
            self.db_transactions.append((sql, params))
        def db_query(self, sql, params=None):
            self.db_queries.append((sql, params))
            if self.db_query_responses:
                return self.db_query_responses.pop(0)
            return []
    return _Env()


@pytest.fixture
def sso(env):
    """A HubzeroSSOLoginModule with .env preset."""
    from hubzero_cmsauth.api import HubzeroSSOLoginModule
    m = HubzeroSSOLoginModule.__new__(HubzeroSSOLoginModule)
    m.env = env
    return m


# --- HTTP transport stub (re-used from previous shape) ----------------- #

class _FakeResponse(object):
    def __init__(self, status, body=b""):
        self.status = status
        self._body  = body
    def read(self):
        return self._body


class _FakeHTTPConn(object):
    def __init__(self, host, port, **kw):
        self.host = host
        self.port = port
        self.kw   = kw
        self.calls = []
        self._next_response  = None
        self._next_exception = None
    def stage_response(self, status, body):
        self._next_response = _FakeResponse(status, body)
        self._next_exception = None
    def stage_exception(self, exc):
        self._next_exception = exc
        self._next_response  = None
    def request(self, method, url, headers=None, body=None):
        self.calls.append({"method": method, "url": url,
                           "headers": dict(headers or {}), "body": body})
        if self._next_exception:
            raise self._next_exception
    def getresponse(self):
        return self._next_response
    def close(self):
        pass


@pytest.fixture
def http_stub(monkeypatch):
    """Stage CMS API responses BEFORE the plugin makes the call:

        http_stub.stage_response(200, b'{"profile":{...}}')
        http_stub.stage_exception(socket.timeout())
        sso.authenticate(req)
        http_stub.last_conn.calls    # inspect the request
    """
    from hubzero_cmsauth import api as plugin_api

    default_body = (b'{"profile":{"id":1,"username":"jdoe",'
                    b'"name":"Jane Doe","email":"j@x"}}')
    pending = {"status": 200, "body": default_body, "exception": None}
    conns = []

    def _make(*a, **kw):
        c = _FakeHTTPConn(*a, **kw)
        if pending["exception"] is not None:
            c.stage_exception(pending["exception"])
        else:
            c.stage_response(pending["status"], pending["body"])
        conns.append(c)
        return c

    monkeypatch.setattr(plugin_api, "HTTPSConnection", _make)
    monkeypatch.setattr(plugin_api, "HTTPConnection",  _make)

    class _Stub(object):
        @property
        def last_conn(self):
            return conns[-1] if conns else None
        @property
        def all_conns(self):
            return list(conns)
        def stage_response(self, status, body):
            pending["status"], pending["body"], pending["exception"] = status, body, None
        def stage_exception(self, exc):
            pending["exception"] = exc
            pending["status"], pending["body"] = None, None

    return _Stub()
