"""Tests for hubzeroplugin.api (HubzeroPermissionStore / HubzeroPermissionGroupProvider).

The Trac framework + the CMS MySQL DB are stubbed (see conftest.py).  Tests
exercise the plugin's own logic: which queries fire for which inputs, the
parameter shape, the result merge, and the security/regression behaviors
fixed across the hzforge.1..hzforge.4 iterations.
"""
from __future__ import absolute_import, division, print_function, unicode_literals

import pytest

from hubzeroplugin import api


# -----------------------------------------------------------------------------
# _cms_cursor context manager: closes the connection on exit (including on
# exception) -- the bug the hzforge.3 rewrite fixed (old disconnect() never
# called .close()).
# -----------------------------------------------------------------------------

def test_cms_cursor_closes_connection_on_normal_exit(env, monkeypatch, fake_conn):
    conn = fake_conn()
    monkeypatch.setattr(api, '_open_cms_connection', lambda _: conn)
    with api._cms_cursor(env) as (cursor, db):
        assert db is conn
        cursor.execute('SELECT 1', ())
    assert conn.closed is True


def test_cms_cursor_closes_connection_on_exception(env, monkeypatch, fake_conn):
    conn = fake_conn()
    monkeypatch.setattr(api, '_open_cms_connection', lambda _: conn)
    with pytest.raises(RuntimeError, match="boom"):
        with api._cms_cursor(env) as (cursor, _):
            cursor.execute('SELECT 1', ())
            raise RuntimeError("boom")
    assert conn.closed is True


# -----------------------------------------------------------------------------
# HubzeroPermissionStore.__init__: resolves project name -> project_id (or
# INSERTs a new row if absent), preserves str type for project_id (hzforge.3
# fixed the previous int+str log concat that crashed on the create path).
# -----------------------------------------------------------------------------

def test_init_picks_up_existing_project_id(env, fake_db):
    fake_db(staged_results=[[(7,)]])      # SELECT returns one row, id=7
    store = api.HubzeroPermissionStore.__new__(api.HubzeroPermissionStore)
    store.env = env
    api.HubzeroPermissionStore.__init__(store)
    assert store.project_id == '7'        # str(), not int
    assert isinstance(store.project_id, str)


def test_init_creates_project_when_missing(env, fake_db):
    # SELECT returns no row; INSERT runs; db.insert_id() -> 42
    fake_db(staged_results=[[], []], insert_id_value=42)
    store = api.HubzeroPermissionStore.__new__(api.HubzeroPermissionStore)
    store.env = env
    api.HubzeroPermissionStore.__init__(store)
    assert store.project_id == '42'       # str(int) -- no longer crashes the log
    # Two executes: SELECT + INSERT
    calls = fake_db.current().cursor_obj.calls
    assert len(calls) == 2
    assert calls[0][0].startswith('SELECT id FROM jos_trac_project')
    assert calls[0][1] == ('tooltest',)
    assert calls[1][0].startswith('INSERT IGNORE INTO jos_trac_project')
    assert calls[1][1] == ('tooltest',)


# -----------------------------------------------------------------------------
# get_user_permissions: anonymous runs 1 query, authenticated runs 4; all
# results are merged into a single set.
# -----------------------------------------------------------------------------

def test_get_user_permissions_anonymous_runs_only_the_anonymous_query(fake_db, store):
    fake_db(staged_results=[[('WIKI_VIEW',), ('LOG_VIEW',)]])
    perms = set(store.get_user_permissions('anonymous'))
    assert perms == {'WIKI_VIEW', 'LOG_VIEW'}
    calls = fake_db.current().cursor_obj.calls
    assert len(calls) == 1                 # only the user_id=0 query
    sql, params = calls[0]
    assert 'p.user_id=0' in sql
    assert params == ('42',)               # just project_id


def test_get_user_permissions_authenticated_merges_all_four_sources(fake_db, store):
    fake_db(staged_results=[
        [('WIKI_VIEW',)],                  # anonymous
        [('TICKET_CREATE',)],              # user-direct
        [('REPORT_VIEW',)],                # via group membership
        [('TIMELINE_VIEW',)],              # via 'authenticated' group
    ])
    perms = set(store.get_user_permissions('alice'))
    assert perms == {'WIKI_VIEW', 'TICKET_CREATE', 'REPORT_VIEW', 'TIMELINE_VIEW'}
    calls = fake_db.current().cursor_obj.calls
    assert len(calls) == 4                 # all four sub-queries fired


def test_get_user_permissions_returns_empty_when_no_project_id(fake_db, store_factory):
    fake_db(staged_results=[[('SHOULD_NOT_BE_FETCHED',)]])
    store = store_factory(project_id=None)
    assert store.get_user_permissions('alice') == []
    # The connection was never opened -- nothing called .execute()
    assert fake_db.current() is None


# -----------------------------------------------------------------------------
# get_users_with_permissions: dynamic IN clause; N permissions -> N
# placeholders + 1 for project_id.  (hzforge.2 closed the SQL-injection class
# by building the placeholder list from len(permissions) instead of quoting
# values into the SQL string.)
# -----------------------------------------------------------------------------

@pytest.mark.parametrize("n", [1, 2, 5])
def test_get_users_with_permissions_in_clause_has_n_placeholders(fake_db, store, n):
    perms = ['PERM_{}'.format(i) for i in range(n)]
    fake_db(staged_results=[[('alice',)], [('bob',)]])
    store.get_users_with_permissions(perms)
    calls = fake_db.current().cursor_obj.calls
    assert len(calls) == 2                 # user-direct, then via-group
    for sql, params in calls:
        # IN clause has exactly n %s placeholders, plus a trailing %s for project_id
        assert sql.count('%s') == n + 1
        assert params == tuple(perms) + ('42',)


# -----------------------------------------------------------------------------
# grant_permission / revoke_permission: the special-case routing for
# anonymous/authenticated, @group, and regular usernames.
# -----------------------------------------------------------------------------

def test_grant_permission_anonymous_uppercase_action(fake_db, store):
    """anonymous -> uidNumber='0', INSERTs into jos_trac_user_permission."""
    fake_db(staged_results=[[]])           # the INSERT returns no rowset
    store.grant_permission('anonymous', 'WIKI_VIEW')
    calls = fake_db.current().cursor_obj.calls
    # No SELECT for 'anonymous' -- uidNumber is hardcoded to '0'.
    assert len(calls) == 1
    sql, params = calls[0]
    assert sql.startswith('INSERT IGNORE INTO jos_trac_user_permission')
    assert params == ('0', 'WIKI_VIEW', '42')


def test_grant_permission_normal_user_uppercase_action(fake_db, store):
    """SELECTs the user id, then INSERTs into jos_trac_user_permission."""
    fake_db(staged_results=[[(101,)], []])
    store.grant_permission('alice', 'WIKI_VIEW')
    calls = fake_db.current().cursor_obj.calls
    assert len(calls) == 2
    sel_sql, sel_params = calls[0]
    assert sel_sql == 'SELECT id FROM jos_users WHERE username=%s'
    assert sel_params == ('alice',)
    ins_sql, ins_params = calls[1]
    assert ins_sql.startswith('INSERT IGNORE INTO jos_trac_user_permission')
    assert ins_params == ('101', 'WIKI_VIEW', '42')


def test_revoke_permission_authenticated_uppercase_action(fake_db, store):
    """authenticated -> gidNumber='0', DELETEs from jos_trac_group_permission."""
    fake_db(staged_results=[[]])
    store.revoke_permission('authenticated', 'WIKI_VIEW')
    calls = fake_db.current().cursor_obj.calls
    assert len(calls) == 1
    sql, params = calls[0]
    assert sql.startswith('DELETE FROM jos_trac_group_permission')
    # WHERE order in this method: trac_project_id, group_id, action
    assert params == ('42', '0', 'WIKI_VIEW')


# -----------------------------------------------------------------------------
# HubzeroPermissionGroupProvider.__init__: resolves project name -> project_id
# via the same jos_trac_project lookup as HubzeroPermissionStore (read-only --
# the Provider is a consumer, not the owner of the project row).
#
# Regression: 2.4.0 (commit 8396faf, "hzforge.4") referenced self.project_id in
# get_permission_groups but never set it in __init__.  The first authenticated
# request raised AttributeError, which Trac caught upstream -- silently
# dropping every @group membership for every authenticated user in production.
# Fixed in 2.4.1: __init__ now calls _resolve_project_id().
# -----------------------------------------------------------------------------

def test_group_provider_init_resolves_project_id_from_db(env, fake_db):
    """The fix: __init__ runs the SELECT and stores the result on the
    instance.  This test deliberately constructs the Component WITHOUT the
    `group_provider` fixture (which seeds project_id manually) so the real
    __init__ path runs."""
    fake_db(staged_results=[[(7,)]])      # SELECT id FROM jos_trac_project -> 7
    p = api.HubzeroPermissionGroupProvider.__new__(api.HubzeroPermissionGroupProvider)
    p.env = env
    api.HubzeroPermissionGroupProvider.__init__(p)
    assert p.project_id == '7'             # str, not int (matches Store's contract)
    assert isinstance(p.project_id, str)


def test_group_provider_init_leaves_project_id_none_when_row_missing(env, fake_db):
    """Read-only behavior: if the project row doesn't exist yet (e.g. the
    Provider was instantiated before the Store on a brand-new env), we leave
    project_id=None instead of INSERTing.  The Store owns row creation."""
    fake_db(staged_results=[[]])           # SELECT returns no row
    p = api.HubzeroPermissionGroupProvider.__new__(api.HubzeroPermissionGroupProvider)
    p.env = env
    api.HubzeroPermissionGroupProvider.__init__(p)
    assert p.project_id is None
    # Confirm we did NOT do an INSERT on the read-only path
    calls = fake_db.current().cursor_obj.calls
    assert len(calls) == 1
    assert calls[0][0].startswith('SELECT id FROM jos_trac_project')


def test_group_provider_init_swallows_db_error_and_leaves_project_id_none(env, monkeypatch):
    """DB outage at Component-load time -> project_id=None, but the
    Component still loads (get_permission_groups returns builtin groups)."""
    def boom(_env):
        raise RuntimeError('CMS DB unavailable')
    monkeypatch.setattr(api, '_open_cms_connection', boom)
    p = api.HubzeroPermissionGroupProvider.__new__(api.HubzeroPermissionGroupProvider)
    p.env = env
    api.HubzeroPermissionGroupProvider.__init__(p)
    assert p.project_id is None
    # And the get_permission_groups fast-path returns builtins (no crash)
    assert p.get_permission_groups('alice') == ['anonymous', 'authenticated']


def test_group_provider_get_permission_groups_returns_builtins_when_project_id_none(
        fake_db, group_provider_factory):
    """No project_id -> skip the DB call, return just the builtin groups.
    Matches HubzeroPermissionStore.get_user_permissions's guard pattern."""
    fake_db(staged_results=[[('@should_not_appear',)]])
    p = group_provider_factory(project_id=None)
    assert p.get_permission_groups('alice') == ['anonymous', 'authenticated']
    # Connection was never opened
    assert fake_db.current() is None


# -----------------------------------------------------------------------------
# HubzeroPermissionGroupProvider.get_permission_groups
# -----------------------------------------------------------------------------

def test_get_permission_groups_anonymous_returns_only_anonymous(fake_db, group_provider):
    fake_db(staged_results=[[('@should_not_appear',)]])
    assert group_provider.get_permission_groups('anonymous') == ['anonymous']
    assert fake_db.current() is None       # no DB call for anonymous


def test_get_permission_groups_authenticated_returns_groups(fake_db, group_provider):
    fake_db(staged_results=[[('staff',), ('admins',)]])
    assert group_provider.get_permission_groups('alice') == [
        'anonymous', 'authenticated', '@staff', '@admins',
    ]


# -----------------------------------------------------------------------------
# Regression: the hzforge.4 fix for the broken-since-2011 query in
# get_permission_groups, which referenced an undefined `proj.name` / `proj.id`
# alias.  Lock it in by asserting the fixed SQL does NOT reference `proj.*`.
# -----------------------------------------------------------------------------

def test_get_permission_groups_query_does_not_reference_undefined_proj_alias(fake_db, group_provider):
    fake_db(staged_results=[[]])
    group_provider.get_permission_groups('alice')
    sql, params = fake_db.current().cursor_obj.calls[0]
    assert 'proj.name' not in sql          # the 2011 bug
    assert 'proj.id'   not in sql
    assert 'p.trac_project_id = %s' in sql # the fix: filter on project_id directly
    assert params == ('42', 'alice')


# -----------------------------------------------------------------------------
# Security regression: no execute() ever sees the value interpolated into
# the SQL string.  We feed a username with an apostrophe that would break a
# concatenated query and assert the value comes through as a parameter.
# -----------------------------------------------------------------------------

def test_user_lookup_passes_value_as_parameter_not_concatenated(fake_db, store):
    """If concatenation regressed, the apostrophe would either crash MySQL or
    open an injection vector.  Parameterized: the value lands in params."""
    evil = "x'; DROP TABLE jos_users; --"
    fake_db(staged_results=[[]])           # SELECT returns no row
    store.grant_permission(evil, 'WIKI_VIEW')
    sel_sql, sel_params = fake_db.current().cursor_obj.calls[0]
    # SQL has a placeholder, not the value:
    assert evil not in sel_sql
    assert '%s' in sel_sql
    # Value arrives as a parameter, where the driver will quote it safely:
    assert sel_params == (evil,)


# -----------------------------------------------------------------------------
# [hubzero] fail_closed (review #8): if true, raise TracError on a CMS DB
# exception (or an unresolved project_id) instead of silently degrading to
# empty permissions.  Default false preserves the existing degrade behavior.
# -----------------------------------------------------------------------------

def test_fail_closed_default_is_false():
    """The knob defaults to False -- existing operators get the silent-
    degrade behavior unless they explicitly opt in.  Locks in the
    back-compat contract."""
    assert api.HubzeroPermissionStore.fail_closed.default is False
    assert api.HubzeroPermissionGroupProvider.fail_closed.default is False


def test_fail_closed_raises_when_project_id_unresolved(fake_db, store_factory):
    """fail_closed=True + project_id=None -> TracError (admin sees a 500
    immediately, not a security-blurred empty-permissions page)."""
    fake_db(staged_results=[])                            # no DB activity expected
    store = store_factory(project_id=None)
    store.fail_closed = True
    with pytest.raises(api.TracError):
        store.get_user_permissions('alice')


def test_fail_closed_false_returns_empty_when_project_id_unresolved(fake_db, store_factory):
    """fail_closed=False (default) + project_id=None -> empty list (the
    silent-degrade behavior that triggered review #8 in the first place;
    documented + preserved for back-compat)."""
    fake_db(staged_results=[])
    store = store_factory(project_id=None)
    store.fail_closed = False
    assert store.get_user_permissions('alice') == []


def test_fail_closed_reraises_on_db_exception(fake_db, store, monkeypatch):
    """fail_closed=True + DB exception mid-query -> the exception propagates
    (vs being swallowed by the except block and returning a partial result)."""
    def boom(_env):
        raise RuntimeError("CMS DB unavailable")
    monkeypatch.setattr(api, '_open_cms_connection', boom)
    store.fail_closed = True
    with pytest.raises(RuntimeError, match="CMS DB unavailable"):
        store.get_user_permissions('alice')


def test_fail_closed_false_swallows_db_exception(fake_db, store, monkeypatch):
    """fail_closed=False + DB exception -> caught + logged + empty return
    (the original behavior; we lock it in to prove the knob is opt-in)."""
    def boom(_env):
        raise RuntimeError("CMS DB unavailable")
    monkeypatch.setattr(api, '_open_cms_connection', boom)
    store.fail_closed = False
    assert store.get_user_permissions('alice') == []
    # The exception WAS logged (visible in env.log)
    assert any(rec[0] == 'exception' for rec in store.env.log.records)


def test_fail_closed_propagates_to_group_provider(fake_db, group_provider_factory):
    """The Provider declares its own BoolOption with the same section/key,
    so trac.ini's `[hubzero] fail_closed = true` flips both sides at once."""
    fake_db(staged_results=[])
    p = group_provider_factory(project_id=None)
    p.fail_closed = True
    with pytest.raises(api.TracError):
        p.get_permission_groups('alice')


def test_fail_closed_reraises_for_grant_and_revoke(fake_db, store, monkeypatch):
    """Admin commands (`trac-admin permission add/remove`) should ALSO
    fail loudly under fail_closed=True -- silently no-op'ing a grant when
    the DB is down would leave the operator thinking they'd granted
    something they hadn't."""
    def boom(_env):
        raise RuntimeError("CMS DB unavailable")
    monkeypatch.setattr(api, '_open_cms_connection', boom)
    store.fail_closed = True
    with pytest.raises(RuntimeError):
        store.grant_permission('alice', 'WIKI_VIEW')
    with pytest.raises(RuntimeError):
        store.revoke_permission('alice', 'WIKI_VIEW')


# -----------------------------------------------------------------------------
# INavigationContributor banner (review #8 telemetry): when a recent DB
# exception was logged, expose a metanav warning to logged-in users so they
# see the degraded state without having to read trac.log.
# -----------------------------------------------------------------------------

import time as _time

class _FakeReq(object):
    def __init__(self, authname="alice"):
        self.authname = authname


def test_navigation_item_absent_when_no_recent_db_error(store):
    """Default state -- _last_db_error_at == 0 -- no banner."""
    store._last_db_error_at = 0
    assert list(store.get_navigation_items(_FakeReq())) == []


def test_navigation_item_shown_when_recent_db_error(store):
    """Recent DB error within the sticky window -> metanav banner."""
    store._last_db_error_at = _time.time() - 5             # 5 seconds ago
    items = list(store.get_navigation_items(_FakeReq()))
    assert len(items) == 1
    region, name, element = items[0]
    assert region == "metanav"
    assert name   == "hubzero_db_status"
    # The element is our stubbed tag.span() dict; verify the text +
    # CSS class are useful.
    assert element["tag"] == "span"
    assert "HUBzero DB unreachable" in element["children"][0]
    assert element["attrs"]["class_"] == "hubzero-db-warning"


def test_navigation_item_clears_after_sticky_window(store):
    """Old error (> _DB_ERROR_BANNER_WINDOW_SECONDS) -> no banner."""
    store._last_db_error_at = _time.time() - api._DB_ERROR_BANNER_WINDOW_SECONDS - 1
    assert list(store.get_navigation_items(_FakeReq())) == []


def test_navigation_item_hidden_for_anonymous(store):
    """Anonymous visitors don't get told about internal infrastructure
    state.  Banner is only for logged-in users (operators)."""
    store._last_db_error_at = _time.time() - 5
    assert list(store.get_navigation_items(_FakeReq(authname="anonymous"))) == []


def test_mark_db_success_clears_the_banner(store):
    """A successful query clears the sticky marker so the banner stops
    showing on subsequent renders."""
    store._last_db_error_at = _time.time() - 5
    assert list(store.get_navigation_items(_FakeReq())) != []
    store._mark_db_success()
    assert list(store.get_navigation_items(_FakeReq())) == []


def test_get_user_permissions_marks_db_success_on_clean_query(fake_db, store):
    """Each public method should stamp _mark_db_success() at the end of a
    successful try block, so a previous error gets cleared as soon as the
    DB is reachable again."""
    store._last_db_error_at = _time.time() - 5     # simulate a prior error
    fake_db(staged_results=[[]])
    store.get_user_permissions('anonymous')
    assert store._last_db_error_at == 0            # cleared


def test_get_user_permissions_marks_db_error_on_exception(fake_db, store, monkeypatch):
    """And the converse: an exception during a method stamps the marker."""
    store._last_db_error_at = 0                     # no prior error
    monkeypatch.setattr(api, '_open_cms_connection',
                        lambda _: (_ for _ in ()).throw(RuntimeError("CMS down")))
    store.get_user_permissions('anonymous')         # swallowed (fail_closed=False)
    assert store._last_db_error_at > 0              # marker stamped


# -----------------------------------------------------------------------------
# Review #9: grant_permission / revoke_permission must NOT be silent no-ops
# when the user/group doesn't exist in jos_users/jos_xgroups.  Without a log
# warning, `trac-admin <env> permission add bob TICKET_VIEW` returns success
# even when `bob` doesn't exist -- operators think the perm landed when it
# didn't.
# -----------------------------------------------------------------------------

def test_grant_permission_warns_on_unknown_user(fake_db, store):
    """SELECT id FROM jos_users WHERE username=%s returns no row -> log a
    warning identifying the user that wasn't found."""
    fake_db(staged_results=[[]])                    # SELECT returns no row
    store.get_user_permissions = lambda *a: None    # avoid the navigation banner check
    store.env.log.records = []                      # reset
    store.grant_permission('ghost', 'TICKET_VIEW')
    warnings = [r for r in store.env.log.records if r[0] == 'warning']
    assert warnings, "expected a log.warning when user not in jos_users"
    msg = warnings[0][1][0]
    assert "not found" in msg
    assert "ghost" in str(warnings[0][1])


def test_grant_permission_warns_on_unknown_group(fake_db, store):
    """SELECT gidNumber FROM jos_xgroups WHERE cn=%s returns no row -> warn."""
    fake_db(staged_results=[[]])
    store.env.log.records = []
    store.grant_permission('@nonexistent', 'TICKET_VIEW')
    warnings = [r for r in store.env.log.records if r[0] == 'warning']
    assert warnings, "expected a log.warning when group not in jos_xgroups"
    msg = warnings[0][1][0]
    assert "nonexistent" in str(warnings[0][1])


def test_revoke_permission_warns_on_unknown_user(fake_db, store):
    """Same contract for revoke -- silent no-op is just as misleading on
    the way out."""
    fake_db(staged_results=[[]])
    store.env.log.records = []
    store.revoke_permission('ghost', 'TICKET_VIEW')
    warnings = [r for r in store.env.log.records if r[0] == 'warning']
    assert warnings
    assert "ghost" in str(warnings[0][1])


def test_grant_permission_does_not_warn_on_known_user(fake_db, store):
    """Sanity: a successful user lookup followed by the INSERT shouldn't
    log a warning -- the new warnings must be precisely scoped to the
    'no row found' branch."""
    fake_db(staged_results=[[(42,)], []])           # SELECT user -> id 42, then INSERT
    store.env.log.records = []
    store.grant_permission('alice', 'TICKET_VIEW')
    warnings = [r for r in store.env.log.records if r[0] == 'warning']
    assert warnings == []


# --- 2.4.4: empty-permissions guard (avoid `IN ()` MySQL syntax error) --- #

def test_get_users_with_permissions_empty_returns_empty_no_db(fake_db, store):
    """An empty permission set must NOT build `... action IN () ...` (a MySQL
    syntax error).  Short-circuit to [] without touching the DB."""
    fake_db(staged_results=[])
    assert store.get_users_with_permissions([]) == []
    # No connection opened -> the guard returned before any query
    assert fake_db.current() is None


# --- 2.4.4: lazy project_id re-resolve (recover after startup-time CMS outage) --- #

def test_check_project_id_lazy_reresolves_when_none(env, monkeypatch):
    """If project_id was None (CMS DB down at Component init), _check_project_id
    attempts ONE read-only re-resolve so the worker recovers without a
    restart once the DB is back."""
    from hubzeroplugin import api
    store = api.HubzeroPermissionStore.__new__(api.HubzeroPermissionStore)
    store.env = env
    store.project = 'tooltest'
    store.project_id = None
    store.fail_closed = False
    monkeypatch.setattr(api, '_resolve_project_id', lambda e, n: '99')
    assert api._check_project_id(store) is True
    assert store.project_id == '99'            # recovered + cached on the instance


def test_check_project_id_lazy_reresolve_still_none_returns_false(env, monkeypatch):
    """Re-resolve still fails (DB still down) + fail_closed off -> False
    (degrade), project_id stays None."""
    from hubzeroplugin import api
    store = api.HubzeroPermissionStore.__new__(api.HubzeroPermissionStore)
    store.env = env
    store.project = 'tooltest'
    store.project_id = None
    store.fail_closed = False
    monkeypatch.setattr(api, '_resolve_project_id', lambda e, n: None)
    assert api._check_project_id(store) is False
    assert store.project_id is None


def test_check_project_id_lazy_reresolve_still_none_fail_closed_raises(env, monkeypatch):
    """Re-resolve still fails + fail_closed on -> TracError (don't silently
    serve an empty-permissions page)."""
    from hubzeroplugin import api
    store = api.HubzeroPermissionStore.__new__(api.HubzeroPermissionStore)
    store.env = env
    store.project = 'tooltest'
    store.project_id = None
    store.fail_closed = True
    monkeypatch.setattr(api, '_resolve_project_id', lambda e, n: None)
    with pytest.raises(api.TracError):
        api._check_project_id(store)


def test_check_project_id_no_reresolve_when_already_set(env, monkeypatch):
    """project_id already resolved -> True immediately, NO re-resolve query."""
    from hubzeroplugin import api
    store = api.HubzeroPermissionStore.__new__(api.HubzeroPermissionStore)
    store.env = env
    store.project = 'tooltest'
    store.project_id = '42'
    store.fail_closed = False
    called = []
    monkeypatch.setattr(api, '_resolve_project_id',
                        lambda e, n: called.append(1) or '99')
    assert api._check_project_id(store) is True
    assert store.project_id == '42'            # unchanged
    assert called == []                        # no re-resolve attempted
