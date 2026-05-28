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

# Py3 stdlib or Py2 PyPI backport (declared in pyproject.toml).
from configparser import RawConfigParser

# PyMySQL is a drop-in replacement for MySQLdb at the surface we use
# (host=/user=/passwd=/db=/.cursor()/.insert_id()).  The `as MySQLdb` alias
# keeps the existing call sites (MySQLdb.connect(...)) untouched -- the only
# import-level work in this port is at the top of the file.
import pymysql as MySQLdb
from pymysql.err import ProgrammingError, OperationalError

from trac.core import *
from trac.perm import IPermissionGroupProvider, IPermissionStore

__all__ = ['IPermissionStore', 'IPermissionGroupProvider']

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
    implements(IPermissionStore)

    group_providers = ExtensionPoint(IPermissionGroupProvider)

    def __init__(self):
        self.env.log.debug('HubzeroPermissionStore::__init__() start')
        self.project = self.env.config.get('project', 'name')
        self.project_id = None
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
        except Exception:
            self.env.log.exception('HubzeroPermissionStore::__init__() failed')
        self.env.log.debug('HubzeroPermissionStore::__init__(): project_id=%s end', self.project_id)

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
        if self.project_id is None:
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
        except Exception:
            self.env.log.exception('HubzeroPermissionStore::get_user_permission(%r) failed', username)
        self.env.log.debug('HubzeroPermissionStore::get_user_permission(%s) end', username)
        return list(actions)

    def get_users_with_permissions(self, permissions):
        """Retrieve a list of users that have any of the specified permissions
        
        Users are returned as a list of usernames.
        """
      
        self.env.log.debug('HubzeroPermissionStore::get_users_with_permission() start')
        result = set()
        if self.project_id is None:
            return list(result)
        # Dynamic IN-clause: one %s per permission, then the project_id
        # appended.  Passed as a single parameter tuple so the driver does
        # the quoting.
        permissions = list(permissions)
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
        except Exception:
            self.env.log.exception('HubzeroPermissionStore::get_users_with_permission(%r) failed', permissions)
        self.env.log.debug('HubzeroPermissionStore::get_users_with_permission() end')
        return list(result)

    def get_all_permissions(self):
        """Return all permissions for all users.

        The permissions are returned as a list of (subject, action)
        formatted tuples."""

        self.env.log.debug('HubzeroPermissionStore::get_all_permissions() for project %s start', self.project)
        perms = []
        if self.project_id is None:
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
        except Exception:
            self.env.log.exception('HubzeroPermissionStore::get_all_permissions() failed')
        self.env.log.debug('HubzeroPermissionStore::get_all_permissions() for project %s end', self.project)
        return perms

    def grant_permission(self, username, action):
        """Grants a user the permission to perform the specified action."""
        self.env.log.debug('HubzeroPermissionStore::grant_permission(%s, %s) start', username, action)
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
                        cursor.execute('SELECT id FROM jos_users WHERE username=%s', (username,))
                        row = cursor.fetchone()
                        if row:
                            uidNumber = str(row[0])

                    if uidNumber is not None:
                        cursor.execute('INSERT IGNORE INTO jos_trac_user_permission (user_id,action,trac_project_id) VALUE (%s,%s,%s);',
                                       (uidNumber, action, self.project_id))
                    if gidNumber is not None:
                        cursor.execute('INSERT IGNORE INTO jos_trac_group_permission (group_id,action,trac_project_id) VALUE (%s,%s,%s);',
                                       (gidNumber, action, self.project_id))
                else:
                    if username and username != 'anonymous' and username != 'authenticated':
                        cursor.execute('SELECT id FROM jos_users WHERE username=%s', (username,))
                        row = cursor.fetchone()
                        if row:
                            uidNumber = str(row[0])

                        if action.startswith('@'):
                            action = action[1:]
                            cursor.execute('SELECT g.gidNumber from jos_xgroups AS g, jos_tool_groups AS tg, '
                                           'jos_trac_project AS proj, jos_tool AS t '
                                           "WHERE tg.role=1 AND g.cn=tg.cn AND t.id=tg.toolid "
                                           "AND CONCAT('app:',t.toolname)=proj.name "
                                           'AND proj.id=%s AND g.cn=%s',
                                           (self.project_id, action))
                            row = cursor.fetchone()
                            if row:
                                gidNumber = str(row[0])

                        # Group membership is intentionally managed only via the
                        # HUBzero CMS (so LDAP sync stays consistent); this plugin
                        # never INSERTs into jos_xgroups_members.  The previous
                        # implementation reached this point with `if True:` and a
                        # dead-code `elif gidNumber is not None and uidNumber is
                        # not None: cursor.execute(INSERT IGNORE INTO
                        # jos_xgroups_members ...)` -- dropped.
                        self.env.log.info('Group membership must be managed through HUBzero in order to maintain LDAP sync')
        except Exception:
            self.env.log.exception('HubzeroPermissionStore::grant_permission(%r, %r) failed', username, action)
        self.env.log.debug('HubzeroPermissionStore::grant_permission() end')

    def revoke_permission(self, username, action):
        """Revokes a users' permission to perform the specified action."""
        self.env.log.debug('HubzeroPermissionStore::revoke_permission(%s, %s) start', username, action)
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
                        cursor.execute('SELECT id FROM jos_users WHERE username=%s', (username,))
                        row = cursor.fetchone()
                        if row:
                            uidNumber = str(row[0])

                    if uidNumber is not None:
                        cursor.execute('DELETE FROM jos_trac_user_permission '
                                       'WHERE trac_project_id=%s AND user_id=%s AND action=%s;',
                                       (self.project_id, uidNumber, action))
                    if gidNumber is not None:
                        cursor.execute('DELETE FROM jos_trac_group_permission '
                                       'WHERE trac_project_id=%s AND group_id=%s AND action=%s;',
                                       (self.project_id, gidNumber, action))
                else:
                    if username and username != 'anonymous' and username != 'authenticated':
                        cursor.execute('SELECT id FROM jos_users WHERE username=%s', (username,))
                        row = cursor.fetchone()
                        if row:
                            uidNumber = str(row[0])

                        if action.startswith('@'):
                            action = action[1:]
                        cursor.execute('SELECT g.gidNumber from jos_xgroups AS g, jos_tool_groups AS tg, '
                                       'jos_trac_project AS proj, jos_tool AS t '
                                       "WHERE tg.role=1 AND g.cn=tg.cn AND t.id=tg.toolid "
                                       "AND CONCAT('app:',t.toolname)=proj.name "
                                       'AND proj.id=%s AND g.cn=%s',
                                       (self.project_id, action))
                        row = cursor.fetchone()
                        if row:
                            gidNumber = str(row[0])

                        # Same policy as grant_permission: group membership is
                        # managed only via the HUBzero CMS for LDAP-sync
                        # reasons.  Dropping the dead `elif gidNumber is not
                        # None and uidNumber is not None:` block that would
                        # have DELETEd from jos_xgroups_members / _managers.
                        self.env.log.info('Group membership must be managed through HUBzero in order to maintain LDAP sync')
        except Exception:
            self.env.log.exception('HubzeroPermissionStore::revoke_permission(%r, %r) failed', username, action)
        self.env.log.debug('HubzeroPermissionStore::revoke_permission() end')

class HubzeroPermissionGroupProvider(Component):
    """Provides the basic builtin permission groups 'anonymous' and 'authenticated' and HUBzero groups."""

    implements(IPermissionGroupProvider)

    def __init__(self):
        self.env.log.debug('HubzeroPermissionGroupProvider::__init__()')
        self.project = self.env.config.get('project', 'name')
        # No DB state held on the instance -- each get_permission_groups()
        # call opens its own connection via _cms_cursor().  (The previous
        # __init__ also accidentally constructed HubzeroDatabaseConnection
        # twice, leaking the first one.)

    # No __del__ -- nothing to clean up.

    def get_permission_groups(self, username):
        self.env.log.debug('HubzeroPermissionGroupProvider::get_permission_groups(%s) start', username)
        groups = ['anonymous']
        if not (username and username != 'anonymous'):
            return groups
        groups.append('authenticated')
        if self.project_id is None:
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
        self.env.log.debug('HubzeroPermissionGroupProvider::get_permission_groups(%s) end', username)
        return groups
