# -*- coding: utf-8 -*-
#
# @package      hubzero-trac-mysqlauthz
# @file         hubzeroplugin/api.py
# @copyright    Copyright (c) 2010-2020 The Regents of the University of California.
# @license      http://opensource.org/licenses/MIT MIT
#
# Copyright (c) 2010-2020 The Regents of the University of California.
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.
#
# HUBzero is a registered trademark of The Regents of the University of California.
#

"""Management of permissions."""

from __future__ import absolute_import, division, print_function, unicode_literals

import contextlib
import re
import time

# Py3 stdlib or Py2 PyPI backport (declared in pyproject.toml).
from configparser import RawConfigParser

# PyMySQL is a drop-in replacement for MySQLdb at the surface we use
# (host=/user=/passwd=/db=/.cursor()/.insert_id()).  The `as MySQLdb` alias
# keeps the existing call sites (MySQLdb.connect(...)) untouched -- the only
# import-level work in this port is at the top of the file.
import pymysql as MySQLdb
from pymysql.err import ProgrammingError, OperationalError

from trac.config import BoolOption
from trac.core import *
from trac.perm import IPermissionGroupProvider, IPermissionStore
from trac.util.html import tag                # cross-version Trac tag builder
from trac.web.chrome import INavigationContributor

__all__ = ['IPermissionStore', 'IPermissionGroupProvider']


# `[hubzero] fail_closed` is a SHARED knob between Store + Provider (and any
# future HUBzero component).  Both Components declare the same BoolOption so
# either instance can read it; Trac de-dups them on the same section/key.
_FAIL_CLOSED_DOC = (
    "If true, raise TracError on a CMS DB exception (or on an unresolved "
    "project_id at startup) instead of silently degrading to empty "
    "permissions.  Silent degrade hides TRAC_ADMIN from the actual admins "
    "during a CMS DB outage -- pages still render, but admin functions "
    "vanish with no signal except in trac.log (which is often disabled on "
    "production envs).  Default false for backward compat; flip to true on "
    "envs where you'd rather see a 500 than a security-blurred page."
)


# Sticky DB-error timestamp window: how long after a DB exception the
# Store's metanav warning stays visible.  Reset to 0 after a successful
# query so the banner clears when the DB comes back.  Lives on the Store
# instance (one per env -- Trac instantiates Components per Environment),
# so envs that share a CMS DB but don't talk to it at the same moment
# don't bleed banners across each other.
_DB_ERROR_BANNER_WINDOW_SECONDS = 300


def _check_project_id(component):
    """Helper used at the top of every public method on Store and Provider.

    Returns True when the caller should continue (`project_id` is resolved).
    Returns False when the caller should bail with its method-specific empty
    return (`project_id is None` and `fail_closed = False` -- the silent-
    degrade default).
    Raises `TracError` when `project_id is None` and `fail_closed = True` --
    in fail-closed mode, a startup DB outage (where `_resolve_project_id`
    swallowed the exception and left project_id=None) should propagate to
    the request, not silently render an empty-permissions page.

    Module-level instead of a method on the two Components so both can
    call it without a shared base class.  Py2's unbound-method check
    blocked the alternative `_check_project_id = Store._check_project_id`
    aliasing pattern.

    Lazy recovery: project_id is resolved once at Component __init__.  If
    the CMS DB was down at that moment, project_id is None for the life of
    the worker process -- silently stripping every authenticated user's
    @group memberships even after the DB recovers (the availability bug
    flagged in the 2026-05-30 review).  So when project_id is None we
    attempt ONE read-only re-resolve here (the row was created by the
    Store on an earlier successful request, or by another worker); on
    success the Component recovers without a restart."""
    if component.project_id is None:
        recovered = _resolve_project_id(component.env, component.project)
        if recovered is not None:
            component.project_id = recovered
            component.env.log.info(
                "%s: recovered project_id=%s on lazy re-resolve "
                "(CMS DB was likely down at Component init)",
                type(component).__name__, recovered)
    if component.project_id is not None:
        return True
    if component.fail_closed:
        raise TracError(
            "HUBzero project_id unresolved; CMS DB may be unreachable "
            "(set [hubzero] fail_closed = false to degrade silently "
            "instead of failing closed).")
    return False

HUBZERO_MODULE_CONFIG = [ 'enable', 'project', 'hzdb_host', 'hzdb_user', 'hzdb_password', 'hzdb_db' ]

def _open_cms_connection(env):
    """Open a fresh MySQL connection to the HUBzero CMS DB.  Caller owns the
    returned connection and must close() it -- use `with contextlib.closing(...)`
    or the `_cms_cursor()` context manager below.

    Connection params come from /etc/hubzero.conf (which points at the hub's
    docroot) + <docroot>/configuration.php (PHP source, regex-parsed for
    `var/public $name = value;` lines, then poked into env.config['hubzero']).
    """
    config = RawConfigParser()
    config.read('/etc/hubzero.conf')
    section = config.get('DEFAULT', 'site')
    docroot = config.get(section, 'DocumentRoot')
    with open(docroot + '/configuration.php', 'r') as fp:
        contents = fp.read()
    for m in re.finditer(r"\s*(?:var|public)\s+\$([a-zA-Z-_0-9]+)\s*=\s*(.+)\s*;", contents):
        env.config.set('hubzero', m.group(1), m.group(2).strip(" \'\"\t"))
    return MySQLdb.connect(
        host       = env.config.get('hubzero', 'hzdb_host',     env.config.get('hubzero', 'host',     'localhost')),
        user       = env.config.get('hubzero', 'hzdb_user',     env.config.get('hubzero', 'user',     'hubzero')),
        passwd     = env.config.get('hubzero', 'hzdb_password', env.config.get('hubzero', 'password', '')),
        db         = env.config.get('hubzero', 'hzdb_db',       env.config.get('hubzero', 'db',       'hubzero')),
        autocommit = True,
    )


def _resolve_project_id(env, project_name):
    """Read-only lookup of jos_trac_project.id by name.  Returns the id as a
    string, or None if no row matches.  Does NOT create the row (that's
    HubzeroPermissionStore.__init__'s job -- the Store owns project-row
    lifecycle; other Components are read-only consumers).
    """
    try:
        with _cms_cursor(env) as (cursor, _db):
            cursor.execute('SELECT id FROM jos_trac_project WHERE name=%s',
                           (project_name,))
            row = cursor.fetchone()
            return str(row[0]) if row else None
    except Exception:
        env.log.exception('_resolve_project_id(%r) failed', project_name)
        return None


@contextlib.contextmanager
def _cms_cursor(env):
    """Yield (cursor, connection) for a fresh CMS DB connection, closing both
    on exit -- normal completion, exception, or generator close.  Use as::

        with _cms_cursor(self.env) as (cursor, db):
            cursor.execute(...)

    Each call opens its OWN connection, so multiple threads (the mod_wsgi
    daemon runs `processes=2 threads=15`) cannot race on shared state.  This
    replaces the previous HubzeroDatabaseConnection class, whose `db` and
    `dbcursor` were class attributes (every instance, every thread, all
    sharing one connection) and whose `disconnect()` only nulled the
    references without actually calling `.close()` (leaked connections).
    """
    db = _open_cms_connection(env)
    try:
        cursor = db.cursor()
        try:
            yield cursor, db
        finally:
            cursor.close()
    finally:
        db.close()

class HubzeroPermissionStore(Component):
    """HUBzero implementation of permission storage and limited group management.

    This component uses the group and user and trac permission tables in HUBzero
    to store permissions and groups.
    """
    implements(IPermissionStore, INavigationContributor)

    group_providers = ExtensionPoint(IPermissionGroupProvider)

    fail_closed = BoolOption("hubzero", "fail_closed", False, _FAIL_CLOSED_DOC)

    def __init__(self):
        self.env.log.debug('HubzeroPermissionStore::__init__() start')
        self.project = self.env.config.get('project', 'name')
        self.project_id = None
        # Sticky DB-error marker: set by _mark_db_error() on any exception
        # in a method body; cleared by _mark_db_success() after a query
        # completes cleanly.  get_navigation_items() reads it to decide
        # whether to surface the [!] banner in the metanav.
        self._last_db_error_at = 0
        try:
            with _cms_cursor(self.env) as (cursor, db):
                cursor.execute('SELECT id FROM jos_trac_project WHERE name=%s', (self.project,))
                row = cursor.fetchone()
                if row:
                    self.project_id = str(row[0])
                else:
                    cursor.execute('INSERT IGNORE INTO jos_trac_project (name) VALUE (%s);', (self.project,))
                    # str() so the project_id type is consistent with the
                    # row-fetched branch above (it was an int before -- which
                    # crashed the `'...' + self.project_id + '...'` log below).
                    self.project_id = str(db.insert_id())
            self._mark_db_success()
        except Exception:
            self._mark_db_error()
            self.env.log.exception('HubzeroPermissionStore::__init__() failed')
        self.env.log.debug('HubzeroPermissionStore::__init__(): project_id=%s end', self.project_id)

    # -- DB-error sticky marker (review #8) --------------------------------- #

    def _mark_db_error(self):
        """Stamp the sticky DB-error marker so the next render of the
        metanav surfaces the [!] banner.  Called from every method's
        `except Exception:` block."""
        self._last_db_error_at = time.time()

    def _mark_db_success(self):
        """Clear the sticky DB-error marker.  Called from every method
        immediately after the `with _cms_cursor(...)` block completes
        without exception -- so the banner clears as soon as the CMS DB
        is reachable again."""
        self._last_db_error_at = 0

    # -- INavigationContributor (review #8) --------------------------------- #
    # Surface a [!] banner in the metanav (top-right chrome slot, next to
    # the user / login link) when this env's permission_store hit a CMS DB
    # exception within the last _DB_ERROR_BANNER_WINDOW_SECONDS.  Without
    # this, a CMS DB outage degrades silently: pages render, permissions
    # are empty, admins look anonymous and have no visible signal that
    # anything is wrong unless they happen to be reading trac.log.
    #
    # The banner is shown ONLY to logged-in users -- random anonymous
    # visitors don't see "HUBzero DB unreachable" advertised in their
    # chrome (it's internal infrastructure state).  An operator who's
    # logged in (the audience for this signal) sees it.
    #
    # Cross-Trac-version compatible: INavigationContributor exists in
    # both Trac 1.0/1.4 (Genshi templates) and 1.6 (Jinja2).  Stock
    # `tag.span(...)` from trac.util.html builds correctly in both.

    def get_active_navigation_item(self, req):
        # We don't claim any active item -- the banner is a passive
        # status display, not a navigation target.
        return ""

    def get_navigation_items(self, req):
        if getattr(req, "authname", "anonymous") == "anonymous":
            return
        age = time.time() - self._last_db_error_at
        if 0 < age < _DB_ERROR_BANNER_WINDOW_SECONDS:
            yield (
                "metanav",
                "hubzero_db_status",
                tag.span(
                    " [!] HUBzero DB unreachable - permissions may be incomplete ",
                    class_="hubzero-db-warning",
                    title=("hubzero-trac-mysqlauthz hit a CMS DB exception "
                           "within the last %d seconds; check the env's "
                           "trac.log for details.  This warning auto-clears "
                           "the next time the DB query succeeds."
                           % _DB_ERROR_BANNER_WINDOW_SECONDS),
                ),
            )

    # No __del__ -- nothing to clean up.  Each method opens & closes its own
    # CMS connection via _cms_cursor(); there is no instance-held DB state.

    def get_user_permissions(self, username):
        """Retrieve the permissions for the given user and return them in a
        dictionary.

        The permissions are stored in the HUBzero database is #__trac_user_permission and
        #__trac_group_permission, group membership is stored in #__xgroups_members
        """

        self.env.log.debug('HubzeroPermissionStore::get_user_permission(%s) start', username)
        actions = set()
        if not _check_project_id(self):
            return list(actions)
        try:
            with _cms_cursor(self.env) as (cursor, _db):
                # permissions for anonymous users
                cursor.execute('SELECT DISTINCT(p.action) FROM jos_trac_user_permission AS p '
                               'WHERE p.user_id=0 AND p.trac_project_id=%s',
                               (self.project_id,))
                for action, in cursor.fetchall():
                    actions.add(action)

                if username != 'anonymous':
                    # permissions for the authenticated user
                    cursor.execute('SELECT DISTINCT(p.action) FROM jos_users AS j, jos_trac_user_permission AS p '
                                   'WHERE j.id = p.user_id AND j.username = %s AND p.trac_project_id=%s',
                                   (username, self.project_id))
                    for action, in cursor.fetchall():
                        actions.add(action)

                    # permissions from group memberships
                    cursor.execute('SELECT DISTINCT(p.action) FROM jos_xgroups_members AS xgm, '
                                   'jos_trac_group_permission AS p, jos_users AS j '
                                   'WHERE xgm.uidNumber = j.id AND j.username = %s '
                                   'AND xgm.gidNumber = p.group_id AND p.trac_project_id=%s',
                                   (username, self.project_id))
                    for action, in cursor.fetchall():
                        actions.add(action)

                    # permissions from the special 'authenticated' group
                    cursor.execute('SELECT DISTINCT(p.action) FROM jos_trac_group_permission AS p '
                                   'WHERE p.group_id = 0 AND p.trac_project_id=%s',
                                   (self.project_id,))
                    for action, in cursor.fetchall():
                        actions.add(action)
            self._mark_db_success()
        except Exception:
            self._mark_db_error()
            self.env.log.exception('HubzeroPermissionStore::get_user_permission(%r) failed', username)
            if self.fail_closed:
                raise
        self.env.log.debug('HubzeroPermissionStore::get_user_permission(%s) end', username)
        return list(actions)

    def get_users_with_permissions(self, permissions):
        """Retrieve a list of users that have any of the specified permissions
        
        Users are returned as a list of usernames.
        """
      
        self.env.log.debug('HubzeroPermissionStore::get_users_with_permission() start')
        result = set()
        if not _check_project_id(self):
            return list(result)
        # Dynamic IN-clause: one %s per permission, then the project_id
        # appended.  Passed as a single parameter tuple so the driver does
        # the quoting.
        permissions = list(permissions)
        if not permissions:
            # An empty set would build `... action IN () ...`, a MySQL syntax
            # error (harmless degrade-to-empty when fail_closed=False, but a
            # confusing 500 when fail_closed=True, and a needless DB hit
            # either way).  No permissions requested -> no users match.
            return list(result)
        placeholders = ','.join(['%s'] * len(permissions))
        params = tuple(permissions) + (self.project_id,)
        try:
            with _cms_cursor(self.env) as (cursor, _db):
                # user records with these permissions
                sql_user = ('SELECT DISTINCT(j.username) FROM jos_users AS j, jos_trac_user_permission AS p '
                            'WHERE j.id=p.user_id AND p.action IN ({}) AND p.trac_project_id=%s'
                            .format(placeholders))
                cursor.execute(sql_user, params)
                for username, in cursor.fetchall():
                    result.add(username)

                # user records in groups with any of these permissions
                sql_group = ('SELECT DISTINCT(u.username) FROM jos_users AS u,jos_xgroups_members AS xgm, '
                             'jos_xgroups AS xg, jos_trac_group_permission AS p '
                             'WHERE xgm.gidNumber=p.group_id AND xg.gidNumber = p.group_id '
                             'AND u.id=xgm.uidNumber AND p.action IN ({}) AND p.trac_project_id = %s'
                             .format(placeholders))
                cursor.execute(sql_group, params)
                for username, in cursor.fetchall():
                    result.add(username)
            self._mark_db_success()
        except Exception:
            self._mark_db_error()
            self.env.log.exception('HubzeroPermissionStore::get_users_with_permission(%r) failed', permissions)
            if self.fail_closed:
                raise
        self.env.log.debug('HubzeroPermissionStore::get_users_with_permission() end')
        return list(result)

    def get_all_permissions(self):
        """Return all permissions for all users.

        The permissions are returned as a list of (subject, action)
        formatted tuples."""

        self.env.log.debug('HubzeroPermissionStore::get_all_permissions() for project %s start', self.project)
        perms = []
        if not _check_project_id(self):
            return perms
        try:
            with _cms_cursor(self.env) as (cursor, _db):
                # one row per (user, action) -- explicit users
                cursor.execute('SELECT j.username,p.action FROM jos_users AS j, jos_trac_user_permission AS p '
                               'WHERE j.id = p.user_id AND p.trac_project_id = %s',
                               (self.project_id,))
                perms.extend((u, a) for u, a in cursor.fetchall())

                # ... plus the special 'anonymous' user (user_id=0)
                cursor.execute("SELECT 'anonymous',p.action FROM jos_trac_user_permission AS p "
                               "WHERE p.user_id = '0' AND p.trac_project_id = %s",
                               (self.project_id,))
                perms.extend((u, a) for u, a in cursor.fetchall())

                # one row per (@group, action)
                cursor.execute('SELECT xg.cn,p.action FROM jos_xgroups AS xg, jos_trac_group_permission AS p '
                               'WHERE xg.gidNumber = p.group_id AND p.trac_project_id = %s',
                               (self.project_id,))
                perms.extend(('@' + g, a) for g, a in cursor.fetchall())

                # one row per (user, @group) -- group memberships, expressed
                # as if the @group itself were a "permission"
                cursor.execute('SELECT g.cn,u.username FROM jos_xgroups AS g, jos_xgroups_members AS gm, '
                               'jos_trac_group_permission AS p, jos_users AS u '
                               'WHERE u.id=gm.uidNumber AND g.gidNumber = p.group_id '
                               'AND g.gidNumber=gm.gidNumber AND p.trac_project_id = %s',
                               (self.project_id,))
                perms.extend((u, '@' + g) for g, u in cursor.fetchall())

                # ... plus the special 'authenticated' group (group_id=0)
                cursor.execute("SELECT 'authenticated',p.action FROM jos_trac_group_permission AS p "
                               "WHERE p.group_id = '0' AND p.trac_project_id = %s",
                               (self.project_id,))
                perms.extend((g, a) for g, a in cursor.fetchall())
            self._mark_db_success()
        except Exception:
            self._mark_db_error()
            self.env.log.exception('HubzeroPermissionStore::get_all_permissions() failed')
            if self.fail_closed:
                raise
        self.env.log.debug('HubzeroPermissionStore::get_all_permissions() for project %s end', self.project)
        return perms

    def grant_permission(self, username, action):
        """Grants a user the permission to perform the specified action."""
        self.env.log.debug('HubzeroPermissionStore::grant_permission(%s, %s) start', username, action)
        if not _check_project_id(self):
            return
        try:
            with _cms_cursor(self.env) as (cursor, _db):
                uidNumber = None
                gidNumber = None
                if action.isupper():
                    if username == 'anonymous':
                        uidNumber = '0'
                    elif username == 'authenticated':
                        gidNumber = '0'
                    elif username.startswith('@'):
                        username = username[1:]
                        cursor.execute('SELECT gidNumber from jos_xgroups WHERE cn=%s', (username,))
                        row = cursor.fetchone()
                        if row:
                            gidNumber = str(row[0])
                        else:
                            # review #9: silent no-op was indistinguishable from
                            # success.  Log a warning so `trac-admin permission
                            # add @group X` doesn't quietly do nothing when the
                            # group doesn't exist in jos_xgroups.
                            self.env.log.warning(
                                "HubzeroPermissionStore::grant_permission: "
                                "group @%s not found in jos_xgroups; "
                                "grant of %s is a no-op", username, action)
                    else:
                        cursor.execute('SELECT id FROM jos_users WHERE username=%s', (username,))
                        row = cursor.fetchone()
                        if row:
                            uidNumber = str(row[0])
                        else:
                            # review #9, same reason -- unknown username.
                            self.env.log.warning(
                                "HubzeroPermissionStore::grant_permission: "
                                "user %r not found in jos_users; grant of %s "
                                "is a no-op (check the CMS account exists "
                                "and was synced)", username, action)

                    if uidNumber is not None:
                        cursor.execute('INSERT IGNORE INTO jos_trac_user_permission (user_id,action,trac_project_id) VALUE (%s,%s,%s);',
                                       (uidNumber, action, self.project_id))
                    if gidNumber is not None:
                        cursor.execute('INSERT IGNORE INTO jos_trac_group_permission (group_id,action,trac_project_id) VALUE (%s,%s,%s);',
                                       (gidNumber, action, self.project_id))
                else:
                    # A non-uppercase "action" is a group-membership pseudo-
                    # action (the upstream convention).  This plugin NEVER
                    # writes group membership (jos_xgroups_members) -- that's
                    # managed exclusively by the HUBzero CMS so LDAP sync stays
                    # consistent.  So there is nothing to do here but say so.
                    # (Pre-2.4.5 this branch ran 1-2 SELECTs to compute
                    # uidNumber/gidNumber and then discarded them -- pure dead
                    # code; dropped.)
                    self.env.log.info('Group membership must be managed through HUBzero in order to maintain LDAP sync')
            self._mark_db_success()
        except Exception:
            self._mark_db_error()
            self.env.log.exception('HubzeroPermissionStore::grant_permission(%r, %r) failed', username, action)
            if self.fail_closed:
                raise
        self.env.log.debug('HubzeroPermissionStore::grant_permission() end')

    def revoke_permission(self, username, action):
        """Revokes a users' permission to perform the specified action."""
        self.env.log.debug('HubzeroPermissionStore::revoke_permission(%s, %s) start', username, action)
        if not _check_project_id(self):
            return
        try:
            with _cms_cursor(self.env) as (cursor, _db):
                uidNumber = None
                gidNumber = None
                if action.isupper():
                    if username == 'anonymous':
                        uidNumber = '0'
                    elif username == 'authenticated':
                        gidNumber = '0'
                    elif username.startswith('@'):
                        username = username[1:]
                        cursor.execute('SELECT gidNumber from jos_xgroups WHERE cn=%s', (username,))
                        row = cursor.fetchone()
                        if row:
                            gidNumber = str(row[0])
                        else:
                            # review #9: silent no-op was indistinguishable
                            # from success.
                            self.env.log.warning(
                                "HubzeroPermissionStore::revoke_permission: "
                                "group @%s not found in jos_xgroups; "
                                "revoke of %s is a no-op", username, action)
                    else:
                        cursor.execute('SELECT id FROM jos_users WHERE username=%s', (username,))
                        row = cursor.fetchone()
                        if row:
                            uidNumber = str(row[0])
                        else:
                            self.env.log.warning(
                                "HubzeroPermissionStore::revoke_permission: "
                                "user %r not found in jos_users; revoke of %s "
                                "is a no-op", username, action)

                    if uidNumber is not None:
                        cursor.execute('DELETE FROM jos_trac_user_permission '
                                       'WHERE trac_project_id=%s AND user_id=%s AND action=%s;',
                                       (self.project_id, uidNumber, action))
                    if gidNumber is not None:
                        cursor.execute('DELETE FROM jos_trac_group_permission '
                                       'WHERE trac_project_id=%s AND group_id=%s AND action=%s;',
                                       (self.project_id, gidNumber, action))
                else:
                    # Symmetric with grant_permission: a non-uppercase "action" is a
                    # group-membership pseudo-action this plugin never writes
                    # (CMS-managed for LDAP-sync).  Pre-2.4.5 this branch ran SELECTs
                    # and discarded the results -- dead code, and the @group SELECT
                    # here ran unconditionally (an asymmetry vs grant); dropped.
                    self.env.log.info('Group membership must be managed through HUBzero in order to maintain LDAP sync')
            self._mark_db_success()
        except Exception:
            self._mark_db_error()
            self.env.log.exception('HubzeroPermissionStore::revoke_permission(%r, %r) failed', username, action)
            if self.fail_closed:
                raise
        self.env.log.debug('HubzeroPermissionStore::revoke_permission() end')

class HubzeroPermissionGroupProvider(Component):
    """Provides the basic builtin permission groups 'anonymous' and 'authenticated' and HUBzero groups."""

    implements(IPermissionGroupProvider)

    fail_closed = BoolOption("hubzero", "fail_closed", False, _FAIL_CLOSED_DOC)

    def __init__(self):
        self.env.log.debug('HubzeroPermissionGroupProvider::__init__()')
        self.project = self.env.config.get('project', 'name')
        # Resolve project name -> project_id once at Component construction.
        # get_permission_groups() filters on p.trac_project_id and needs this.
        # Read-only: this Component is a consumer, not the owner of the
        # project row (HubzeroPermissionStore.__init__ creates it on first
        # load).  If the row hasn't been created yet -- e.g. Provider is
        # instantiated before Store on a brand-new env -- project_id stays
        # None and get_permission_groups() returns the builtin groups only;
        # the next env load (after Store has run) resolves it.
        self.project_id = _resolve_project_id(self.env, self.project)
        self.env.log.debug(
            'HubzeroPermissionGroupProvider::__init__(): project_id=%s',
            self.project_id)

    # No __del__ -- nothing to clean up.

    def get_permission_groups(self, username):
        self.env.log.debug('HubzeroPermissionGroupProvider::get_permission_groups(%s) start', username)
        groups = ['anonymous']
        if not (username and username != 'anonymous'):
            return groups
        groups.append('authenticated')
        if not _check_project_id(self):
            return groups
        try:
            with _cms_cursor(self.env) as (cursor, _db):
                # The previous query referenced `proj.name`/`proj.id` in WHERE
                # without ever declaring `proj` in FROM (broken since at least
                # 2011).  Fix: filter directly on `p.trac_project_id` -- we
                # already resolved the project name to project_id in __init__
                # and store it as self.project_id, so the join is unnecessary
                # (matches the pattern of every other query in this plugin).
                # Also: drop the unused `p.action` column, and DISTINCT so a
                # user with multiple permissions via the same group doesn't
                # produce duplicate `@group` entries in the return list.
                cursor.execute('SELECT DISTINCT g.cn FROM jos_xgroups AS g, jos_xgroups_members AS m, '
                               'jos_trac_group_permission AS p, jos_users AS u '
                               'WHERE p.trac_project_id = %s AND m.uidNumber = u.id '
                               'AND u.username = %s AND m.gidNumber = g.gidNumber '
                               'AND g.gidNumber = p.group_id',
                               (self.project_id, username))
                for (groupname,) in cursor.fetchall():
                    groups.append('@' + groupname)
        except Exception:
            self.env.log.exception('HubzeroPermissionGroupProvider::get_permission_groups(%r) failed', username)
            if self.fail_closed:
                raise
        self.env.log.debug('HubzeroPermissionGroupProvider::get_permission_groups(%s) end', username)
        return groups
