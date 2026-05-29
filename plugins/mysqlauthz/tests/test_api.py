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
