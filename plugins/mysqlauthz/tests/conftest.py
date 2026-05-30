"""Test fixtures for hubzero-trac-mysqlauthz.

The plugin imports `trac.core` and `trac.perm` at module load.  Py3 hubs
don't yet have Trac installed (that's a Stage 2 install), so we stub the
handful of names the plugin touches; this lets the test suite run on
Py3.11 without depending on a Trac install.  The stubs are deliberately
no-ops -- the tests exercise the plugin's own logic, not Trac internals.

Connection isolation: every test that touches the CMS DB monkeypatches
`hubzeroplugin.api._open_cms_connection` (via the `fake_db` fixture) to
return an in-memory `FakeConn` whose cursor records every `.execute()`
call and serves staged result rows in order.  No real MySQL is required.
"""
from __future__ import absolute_import, division, print_function, unicode_literals

import os
import sys
import types


# --- src/ on sys.path (so the plugin source is importable regardless of
# pytest's rootdir -- works whether pytest is invoked from this plugin's
# directory or from the repo root with multiple test paths).  Use os.path
# rather than pathlib so this conftest stays Py2-compatible (pathlib is
# Py3.4+ stdlib; Py2 doesn't have it).
# ---

sys.path.insert(0, os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, 'src')))


# --- Trac stubs (must run before any test imports hubzeroplugin.api) ---

for _mod in ('trac', 'trac.core', 'trac.config', 'trac.perm'):
    # str(_mod): on Py2 with `from __future__ import unicode_literals` the
    # literals above are `unicode`, but types.ModuleType() requires a native
    # str.  str() is a no-op on Py3 and a unicode->bytes coerce on Py2.
    sys.modules.setdefault(str(_mod), types.ModuleType(str(_mod)))
sys.modules['trac.core'].Component       = type(str('Component'), (), {})
sys.modules['trac.core'].ExtensionPoint  = lambda *a, **kw: None
sys.modules['trac.core'].implements      = lambda *a, **kw: None
sys.modules['trac.core'].TracError       = type(str('TracError'), (Exception,), {})
sys.modules['trac.perm'].IPermissionStore         = object
sys.modules['trac.perm'].IPermissionGroupProvider = object

# trac.config.BoolOption descriptor: returns the default when accessed on an
# instance (the test fixtures may override per-instance by assigning a value).
class _BoolOption(object):
    def __init__(self, section, key, default, doc=""):
        self.section, self.key, self.default, self.doc = section, key, default, doc
    def __get__(self, instance, owner=None):
        return self if instance is None else self.default


sys.modules['trac.config'].BoolOption = _BoolOption


import pytest   # noqa: E402  (must follow the trac stubs above)


# --- fake trac env ---

class _FakeLog(object):
    """Records every log call so tests can assert on them if they want to."""
    def __init__(self):
        self.records = []
    def _add(self, level, args):
        self.records.append((level, args))
    def debug(self, *a):     self._add('debug', a)
    def info(self, *a):      self._add('info', a)
    def warning(self, *a):   self._add('warning', a)
    def error(self, *a):     self._add('error', a)
    def exception(self, *a): self._add('exception', a)


class _FakeConfig(object):
    """Mimics trac.config.Configuration for `env.config.get(section, key, default)`."""
    def __init__(self):
        self._data = {}
    def get(self, section, option, default=None):
        return self._data.get((section, option), default)
    def set(self, section, option, value):
        self._data[(section, option)] = value


class _FakeEnv(object):
    def __init__(self, project='tooltest'):
        self.log = _FakeLog()
        self.config = _FakeConfig()
        self.config.set('project', 'name', project)


@pytest.fixture
def env():
    """A minimal stand-in for a Trac env: .log records calls, .config.get/set."""
    return _FakeEnv()


# --- fake CMS connection ---

class FakeCursor(object):
    """Records every (sql, params) tuple from .execute(); serves the next
    result-set in `staged_results` from .fetchall()/.fetchone()."""
    def __init__(self, staged_results):
        self.calls = []                # list of (sql, params) tuples
        self._queue = list(staged_results)
        self._current = []
    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        self._current = list(self._queue.pop(0)) if self._queue else []
    def fetchall(self):
        rows, self._current = self._current, []
        return rows
    def fetchone(self):
        rows, self._current = self._current, []
        return rows[0] if rows else None
    def close(self):
        pass


class FakeConn(object):
    """Mimics pymysql.Connection: cursor()/insert_id()/close()."""
    def __init__(self, staged_results, insert_id_value=1):
        self.cursor_obj = FakeCursor(staged_results)
        self.insert_id_value = insert_id_value
        self.closed = False
    def cursor(self):
        return self.cursor_obj
    def insert_id(self):
        return self.insert_id_value
    def close(self):
        self.closed = True


@pytest.fixture
def fake_conn():
    """A factory: `fake_conn(staged_results=[], insert_id_value=1)` -> FakeConn.
    For tests that drive the connection directly (e.g. the _cms_cursor
    context-manager tests).  Most tests use `fake_db` instead, which also
    monkeypatches `_open_cms_connection` to return a FakeConn."""
    def make(staged_results=(), insert_id_value=1):
        return FakeConn(list(staged_results), insert_id_value)
    return make


@pytest.fixture
def fake_db(monkeypatch):
    """Returns a callable.  Use as:

        conn = fake_db(staged_results, insert_id_value=42)

    -- monkeypatches `_open_cms_connection` to return a `FakeConn` whose
    cursor will serve the staged results in order.  `staged_results` is a
    list-of-lists: the Nth inner list is what the Nth cursor.execute() call's
    fetchall()/fetchone() will return.

    `fake_db.current()` returns the FakeConn that was actually handed out --
    or None if the plugin never opened the connection (so tests can assert
    "this code path didn't touch the DB" by checking for None).
    """
    from hubzeroplugin import api
    box = {'conn': None, 'opened': False}
    def install(staged_results, insert_id_value=1):
        conn = FakeConn(staged_results, insert_id_value)
        def opener(_env):
            box['opened'] = True
            box['conn'] = conn
            return conn
        monkeypatch.setattr(api, '_open_cms_connection', opener)
        return conn
    install.current = lambda: box['conn'] if box['opened'] else None
    return install


# --- fixtures for instantiating Trac Components without Trac's framework ---

@pytest.fixture
def store_factory(env):
    """A factory that builds a HubzeroPermissionStore with project_id pre-set,
    bypassing __init__'s own DB lookup so tests of the other methods don't
    have to seed it.  Tests that need to override the default project_id use
    this fixture; tests that just want the common case use `store` below."""
    def make(project_id='42'):
        from hubzeroplugin import api
        s = api.HubzeroPermissionStore.__new__(api.HubzeroPermissionStore)
        s.env = env
        s.project = env.config.get('project', 'name')
        s.project_id = project_id
        return s
    return make


@pytest.fixture
def store(store_factory):
    """The common case: HubzeroPermissionStore with project_id='42'."""
    return store_factory()


@pytest.fixture
def group_provider_factory(env):
    """Factory: build a HubzeroPermissionGroupProvider with project_id
    pre-set, bypassing __init__'s own DB lookup so tests of
    get_permission_groups() don't have to seed the SELECT.  Use this when
    you want to override the default project_id; use `group_provider` for
    the common case.

    NB: this fixture mirrors `store_factory` -- it intentionally bypasses
    __init__ so the test author CHOOSES whether to exercise the resolution
    logic.  Tests that DO want to exercise __init__ should construct the
    Component directly (see test_group_provider_init_resolves_project_id)."""
    def make(project_id='42'):
        from hubzeroplugin import api
        p = api.HubzeroPermissionGroupProvider.__new__(api.HubzeroPermissionGroupProvider)
        p.env = env
        p.project = env.config.get('project', 'name')
        p.project_id = project_id
        return p
    return make


@pytest.fixture
def group_provider(group_provider_factory):
    """The common case: HubzeroPermissionGroupProvider with project_id='42'."""
    return group_provider_factory()
