"""Test fixtures for hubzero-trac-cmsauth.

The plugin imports trac.core / trac.config / trac.web.api / trac.web.chrome
at module load.  Py3 hubs don't yet have Trac installed (that's a Stage 2
install), so we stub the handful of names the plugin touches; this lets
the test suite run on Py3.6 + Py2.7 without depending on a Trac install.
"""
from __future__ import absolute_import, division, print_function, unicode_literals

import os
import sys
import types


# --- src/ on sys.path (so the plugin source is importable regardless of
# pytest's rootdir).  Use os.path rather than pathlib so this conftest stays
# Py2-compatible (pathlib is Py3.4+ stdlib; Py2 doesn't have it).
# ---
sys.path.insert(0, os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "src")))


# --- Trac stubs (must run before any test imports hubzero_cmsauth.api) ---

for _mod in ("trac", "trac.core", "trac.config", "trac.web", "trac.web.api",
             "trac.web.chrome", "trac.util", "trac.util.html"):
    # str(_mod): on Py2 with `from __future__ import unicode_literals` the
    # literals are `unicode`, but types.ModuleType() requires a native str.
    sys.modules.setdefault(str(_mod), types.ModuleType(str(_mod)))


# trac.core: minimal Component + implements + ExtensionPoint
def _component_init(self, *a, **kw):
    pass


sys.modules["trac.core"].Component       = type(str("Component"), (object,),
                                                {"__init__": _component_init})
sys.modules["trac.core"].implements      = lambda *a, **kw: None
sys.modules["trac.core"].ExtensionPoint  = lambda *a, **kw: None


# trac.config: descriptors that just return the default value (the plugin
# reads them via `self.option_name`; in tests, returning the default is fine).
class _ConfigOption(object):
    def __init__(self, section, key, default, doc=""):
        self.section = section
        self.key = key
        self.default = default
        self.doc = doc
    def __get__(self, instance, owner=None):
        if instance is None:
            return self
        return self.default


sys.modules["trac.config"].Option     = _ConfigOption
sys.modules["trac.config"].BoolOption = _ConfigOption
sys.modules["trac.config"].IntOption  = _ConfigOption


# trac.web.api: empty interfaces (plugins implement them but never call
# methods on the interface objects themselves)
sys.modules["trac.web.api"].IAuthenticator = object
sys.modules["trac.web.api"].IRequestHandler = object

# trac.web.chrome: ditto
sys.modules["trac.web.chrome"].INavigationContributor = object


# trac.util.html: minimal `tag` namespace -- the plugin uses tag.a/.b/.span
# in INavigationContributor.get_navigation_items.  Most tests don't touch
# those paths, but the stub keeps imports working.
class _TagBuilder(object):
    def __getattr__(self, name):
        def make(*children, **attrs):
            return ("tag." + name, children, attrs)
        return make


sys.modules["trac.util.html"].tag = _TagBuilder()


import pytest   # noqa: E402  (must follow the trac stubs above)


# --- fake Trac env + req ----------------------------------------------- #

class _FakeLog(object):
    def __init__(self):
        self.records = []
    def _add(self, level, args):
        self.records.append((level, args))
    def debug(self, *a):     self._add("debug",     a)
    def info(self, *a):      self._add("info",      a)
    def warning(self, *a):   self._add("warning",   a)
    def error(self, *a):     self._add("error",     a)
    def exception(self, *a): self._add("exception", a)


class _FakeDb(object):
    """Stand-in for the cursor-like callable yielded by env.db_query and
    env.db_transaction.  Records every (sql, params) pair and serves staged
    result-sets (a list of row tuples per execute()) in FIFO order."""

    def __init__(self, results):
        self.executions = []           # list of (sql, params)
        self._results   = list(results)
    def __call__(self, sql, params=None):
        self.executions.append((sql, params))
        return list(self._results.pop(0)) if self._results else []


class _DbCtx(object):
    """Context manager that mimics env.db_query / env.db_transaction:
    `with self.env.db_query as db: db("SELECT ...", (a, b))` ."""

    def __init__(self, results, raise_on_enter=None):
        self.db = _FakeDb(results)
        self._raise_on_enter = raise_on_enter
    def __enter__(self):
        if self._raise_on_enter:
            raise self._raise_on_enter
        return self.db
    def __exit__(self, *a):
        return False


class _FakeEnv(object):
    def __init__(self):
        self.log = _FakeLog()
        # Both db_query and db_transaction yield a new _FakeDb each time;
        # tests typically stage results via env.stage_db(...).  The
        # `_results` and `_raise` queues are FIFO across the two managers
        # combined, so a test that stages one read-result will see it on
        # the first `with env.db_query/db_transaction` block whichever runs.
        self._db_results = []
        self._db_raise   = []
        self.db_dbs      = []    # all _FakeDb instances created (for assertions)
    def stage_db(self, results):
        """Queue a result-set list (list of row tuples) for the next
        db_query/db_transaction block.  Stage one per `with` block."""
        self._db_results.append(results)
    def stage_db_error(self, exc):
        self._db_raise.append(exc)
    def _next_ctx(self):
        results = self._db_results.pop(0) if self._db_results else []
        raise_on_enter = self._db_raise.pop(0) if self._db_raise else None
        ctx = _DbCtx([results], raise_on_enter=raise_on_enter)
        self.db_dbs.append(ctx.db)
        return ctx
    @property
    def db_query(self):
        return self._next_ctx()
    @property
    def db_transaction(self):
        return self._next_ctx()


class _FakeHref(object):
    """Trac's req.href / req.abs_href -- callable AND attribute-driven.
    We only need .wiki() / .login() / .logout() and the (str) ones for tests."""

    def __init__(self, base):
        self._base = base.rstrip("/")
    def wiki(self, *parts):
        return self._base + "/wiki" + ("/" + "/".join(parts) if parts else "")
    def login(self):
        return self._base + "/login"
    def logout(self):
        return self._base + "/logout"


class _FakeReq(object):
    """Tiny stand-in for Trac's Request.  Tests poke .environ, .path_info,
    .authname, .incookie; the rest goes through methods we mock."""

    def __init__(self, path_info="/", authname="anonymous", environ=None,
                 abs_base="https://help.hubzero.org/tools/hzforgetest"):
        self.path_info = path_info
        self.authname  = authname
        self.environ   = environ or {}
        self.incookie  = _FakeSimpleCookie()
        self.outcookie = _OutCookie()
        self.abs_href  = _FakeHref(abs_base)
        self.href      = _FakeHref(abs_base[abs_base.find("/", 8):])  # path-only
        self.redirected_to = None
    def get_header(self, name):
        return self.environ.get("HTTP_" + name.upper().replace("-", "_"))
    def redirect(self, url):
        # Trac's req.redirect() raises RequestDone after issuing the redirect
        # in real code; we capture the URL and raise a sentinel exception so
        # tests can assert on both the URL and the "execution stopped here"
        # contract.
        self.redirected_to = url
        raise _RedirectDone(url)


class _FakeSimpleCookie(dict):
    """Stand-in for Cookie.SimpleCookie -- supports .output(header, sep)."""
    def output(self, header="", sep="\r\n"):
        # Approximation: render as "k=v; k=v"; we don't need full RFC.
        return sep.join("%s=%s" % (k, v) for k, v in sorted(self.items()))


class _OutMorsel(object):
    """Mimics http.cookies.Morsel for the trac_auth cookie attributes the
    plugin sets: value (assigned at construction), plus path/secure/httponly/
    expires set via dict-style mutation."""

    def __init__(self, value=""):
        self.value = value
        self._attrs = {"path": "", "secure": False, "httponly": False,
                       "expires": "", "max-age": ""}
    def __setitem__(self, k, v):
        self._attrs[k.lower()] = v
    def __getitem__(self, k):
        return self._attrs.get(k.lower(), "")


class _OutCookie(dict):
    """Stand-in for http.cookies.SimpleCookie on the outgoing side.
    Assigning a string value auto-wraps it in an _OutMorsel (matching
    SimpleCookie's `c['name'] = 'value'` syntax)."""

    def __setitem__(self, name, value):
        if isinstance(value, _OutMorsel):
            dict.__setitem__(self, name, value)
        else:
            dict.__setitem__(self, name, _OutMorsel(value))


class _RedirectDone(Exception):
    """Raised by FakeReq.redirect() to mimic Trac's RequestDone."""
    def __init__(self, url):
        super(_RedirectDone, self).__init__(url)
        self.url = url


@pytest.fixture
def env():
    return _FakeEnv()


@pytest.fixture
def make_req():
    """Factory: make_req(path_info='/', authname='anonymous', cookie='...').
    Returns a _FakeReq with the cookie header pre-installed in environ."""
    def _make(path_info="/", authname="anonymous", cookie="",
              abs_base="https://help.hubzero.org/tools/hzforgetest"):
        environ = {}
        if cookie:
            environ["HTTP_COOKIE"] = cookie
        # Match Trac's typical environ -- helps the cms_base_url derivation
        environ.setdefault("wsgi.url_scheme", "https")
        environ.setdefault("HTTP_HOST", "help.hubzero.org")
        return _FakeReq(path_info=path_info, authname=authname,
                        environ=environ, abs_base=abs_base)
    return _make


@pytest.fixture
def RedirectDone():
    """Expose _RedirectDone so tests can `with pytest.raises(RedirectDone):`."""
    return _RedirectDone


# --- Authenticator instantiation helpers ------------------------------- #
#
# Trac's Component framework normally instantiates components inside a
# ComponentManager (which __init__'s them with the env).  Our tests bypass
# that and manually __new__ the class, set .env, then call the methods
# we're testing.

@pytest.fixture
def authenticator(env):
    """A HubzeroSessionAuthenticator with .env preset."""
    from hubzero_cmsauth.api import HubzeroSessionAuthenticator
    a = HubzeroSessionAuthenticator.__new__(HubzeroSessionAuthenticator)
    a.env = env
    return a


@pytest.fixture
def login_module(env):
    """A HubzeroLoginModule with .env preset."""
    from hubzero_cmsauth.api import HubzeroLoginModule
    m = HubzeroLoginModule.__new__(HubzeroLoginModule)
    m.env = env
    return m


# --- HTTP transport monkey-patches ------------------------------------- #

class _FakeResponse(object):
    """Stand-in for http.client.HTTPResponse -- status + read()."""
    def __init__(self, status, body=b""):
        self.status = status
        self._body  = body
    def read(self):
        return self._body


class _FakeHTTPConn(object):
    """Stand-in for http.client.HTTPSConnection / HTTPConnection.

    Records every request() and serves a pre-staged response (or raises a
    pre-staged exception).  Use the `http_stub` fixture to install."""

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
    """Install a controllable HTTPConnection/HTTPSConnection in the
    plugin's api module.

    Usage:
        http_stub.stage_response(403, b'{...}')           # before plugin call
        http_stub.stage_exception(socket.timeout())       # before plugin call
        username = authenticator.authenticate(req)        # triggers _call_api
        http_stub.last_conn.calls                         # inspect the request
    """
    from hubzero_cmsauth import api as plugin_api

    # The factory holds the pending response/exception so the test can stage
    # outcomes BEFORE the plugin opens the connection.  Default outcome is
    # "200 OK + valid profile JSON", which makes the happy path one-liner-y.
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
