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

class HubzeroDatabaseConnection(object):
    """Hubzero Database Connection"""

    db = False
    dbcursor = False

    def __init__(self,env):
      env.log.debug('HubzeroDatabaseConnection::__init__() start')
      self.env = env
      self.connect()
      env.log.debug('HubzeroDatabaseConnection::__init__() end')

    def __del__(self):
      self.env.log.debug('HubzeroDatabaseConnection::__del__() start')
      self.disconnect()
      self.env.log.debug('HubzeroDatabaseConnection::__del__() end')

    def connect(self):
      self.env.log.debug('HubzeroDatabaseConnection::connect() start')
      if (self.__class__.db):
        self.env.log.debug('HubzeroDatabaseConnection::connect() already connected. end')
        return 1

      config = RawConfigParser()
      config.read('/etc/hubzero.conf')
      section = config.get('DEFAULT','site')
      docroot = config.get(section, 'DocumentRoot')
      file_read = open(docroot + '/configuration.php',"r")
      contents = file_read.read()
      file_read.close() 
      for m in re.finditer("\s*(?:var|public)\s+\$([a-zA-Z-_0-9]+)\s*=\s*(.+)\s*;", contents):
        self.env.config.set('hubzero',m.group(1),m.group(2).strip(" \'\"\t"))

      mysql_host = self.env.config.get('hubzero', 'hzdb_host', self.env.config.get('hubzero', 'host', 'localhost'))
      mysql_user = self.env.config.get('hubzero', 'hzdb_user', self.env.config.get('hubzero', 'user', 'hubzero'))
      mysql_password = self.env.config.get('hubzero', 'hzdb_password', self.env.config.get('hubzero', 'password', ''))
      mysql_db = self.env.config.get('hubzero', 'hzdb_db', self.env.config.get('hubzero','db', 'hubzero'))
        
      try:
        self.__class__.db = MySQLdb.connect(host=mysql_host, user=mysql_user, passwd=mysql_password, db=mysql_db)
      except:
        self.env.log.error('HubzeroDatabaseConnection::connect() Failed: host %s user %s password XXXXX db %s' % (mysql_host, mysql_user, mysql_db))
        self.env.log.debug('HubzeroDatabaseConnection::connect() end')
        return 0

      self.env.log.debug('HubzeroDatabaseConnection::connect() end')
      return 1

    def cursor(self):
      self.env.log.debug('HubzeroDatabaseConnection::cursor() start')
      if not self.__class__.db:
        if not self.connect():
          self.env.log.debug('HubzeroDatabaseConnection::cursor() connection failed. end')
          return None

      if self.__class__.dbcursor:
        self.env.log.debug('HubzeroDatabaseConnection::cursor() already have cursor')
      else:
        self.env.log.debug('HubzeroDatabaseConnection::cursor() getting cursor from db')
        self.__class__.dbcursor = self.__class__.db.cursor()

        if not self.__class__.dbcursor:
          self.env.log.error('HubzeroDatabaseConnection::cursor() Failed to create datanase cursor. end')
          self.__class__.dbcursor = False

      self.env.log.debug('HubzeroDatabaseConnection::cursor() end')
      return self.__class__.dbcursor

    def disconnect(self):
      self.env.log.debug('HubzeroDatabaseConnection::disconnect() start')
      self.__class__.dbcursor = None
      self.__class__.db = None
      self.env.log.debug('HubzeroDatabaseConnection::disconnect() end')

    def insert_id(self):
      return self.__class__.db.insert_id()

class HubzeroPermissionStore(Component):
    """HUBzero implementation of permission storage and limited group management.
    
    This component uses the group and user and trac permission tables in HUBzero 
    to store permissions and groups.
    """
    implements(IPermissionStore)

    group_providers = ExtensionPoint(IPermissionGroupProvider)

    def __init__(self):
        self.env.log.debug('HubzeroPermissionStore::__init__() start')
        self.db = HubzeroDatabaseConnection(self.env)
        self.project = self.env.config.get('project', 'name')

        cursor = self.db.cursor()

        if not cursor:
          self.project_id = None
          self.db = None
          self.env.log.debug('HubzeroPermissionStore::__init__() no database cursor.')
        else:
          self.env.log.debug('HubzeroPermissionStore::__init__(): SELECT id FROM jos_trac_project WHERE name=%s', self.project)
          cursor.execute('SELECT id FROM jos_trac_project WHERE name=%s', (self.project,))
          row = cursor.fetchone()

          if row:
            self.project_id = str(row[0])
          else:
            self.env.log.debug('HubzeroPermissionStore::__init__(): INSERT IGNORE INTO jos_trac_project (name) VALUE (%s);', self.project)
            cursor.execute('INSERT IGNORE INTO jos_trac_project (name) VALUE (%s);', (self.project,))
            self.project_id = self.db.insert_id()

        self.env.log.debug('HubzeroPermissionStore::__init__(): ' + self.project_id + ' : end')

    def __del__(self):
      self.env.log.debug('HubzeroPermissionStore::__del__() start')
      self.db = None
      self.env.log.debug('HubzeroPermissionStore::__del__() end')

    def get_user_permissions(self, username):
        """Retrieve the permissions for the given user and return them in a
        dictionary.

        The permissions are stored in the HUBzero database is #__trac_user_permission and
        #__trac_group_permission, group membership is stored in #__xgroups_members
        """

        self.env.log.debug('HubzeroPermissionStore::get_user_permission(' + username + ') start')
        
        actions = set([])

        cursor = self.db.cursor()

        if not cursor:
          self.env.log.debug('HubzeroPermissionStore::get_user_permission(' + username + ') no database cursor. end')
          self.db.disconnect()
          return list(actions)

        if self.project_id == None:
          self.env.log.debug('HubzeroPermissionStore::get_user_permission(' + username + ') no project_id. end')
          self.db.disconnect()
          return list(actions)

        # permissions for anonymous users
        self.env.log.debug('SELECT DISTINCT(p.action) FROM jos_trac_user_permission AS p WHERE p.user_id=0 AND p.trac_project_id=%s', self.project_id)
        cursor.execute('SELECT DISTINCT(p.action) FROM jos_trac_user_permission AS p WHERE p.user_id=0 AND p.trac_project_id=%s', (self.project_id,))
        rows = cursor.fetchall()
        for action, in rows:
          self.env.log.debug('HubzeroPermissionStore::get_user_permission(): adding [anonymous] permission ' + action)
          actions.add(action)

        # permissions for authenticated users
        if username != 'anonymous':
          self.env.log.debug('SELECT DISTINCT(p.action) FROM jos_users AS j, jos_trac_user_permission AS p WHERE j.id = p.user_id AND j.username = %s AND p.trac_project_id=%s', username, self.project_id)
          cursor.execute('SELECT DISTINCT(p.action) FROM jos_users AS j, jos_trac_user_permission AS p WHERE j.id = p.user_id AND j.username = %s AND p.trac_project_id=%s', (username, self.project_id))
          rows = cursor.fetchall()
          for action, in rows:
            self.env.log.debug('HubzeroPermissionStore::get_user_permission(): adding [' + username + '] permission ' + action)
            actions.add(action)

          # permissions from group memberships
          self.env.log.debug('SELECT DISTINCT(p.action) FROM jos_xgroups_members AS xgm, jos_trac_group_permission AS p, jos_users AS j WHERE xgm.uidNumber = j.id AND j.username = %s AND xgm.gidNumber = p.group_id AND p.trac_project_id=%s', username, self.project_id);
          cursor.execute('SELECT DISTINCT(p.action) FROM jos_xgroups_members AS xgm, jos_trac_group_permission AS p, jos_users AS j WHERE xgm.uidNumber = j.id AND j.username = %s AND xgm.gidNumber = p.group_id AND p.trac_project_id=%s', (username, self.project_id));
          rows = cursor.fetchall()
          for action, in rows:
            self.env.log.debug('HubzeroPermissionStore::get_user_permission(): adding [group granted] permission ' + action)
            actions.add(action)

          # permissions from special 'authenticated' group
          self.env.log.debug('SELECT DISTINCT(p.action) FROM jos_trac_group_permission AS p WHERE p.group_id = 0 AND p.trac_project_id=%s', self.project_id);
          cursor.execute('SELECT DISTINCT(p.action) FROM jos_trac_group_permission AS p WHERE p.group_id = 0 AND p.trac_project_id=%s', (self.project_id,));
          rows = cursor.fetchall()
          for action, in rows:
            self.env.log.debug('HubzeroPermissionStore::get_user_permission(): adding [authenticated] permission ' + action)
            actions.add(action)

        self.env.log.debug('HubzeroPermissionStore::get_user_permission(' + username + ') end')
        self.db.disconnect()
        return list(actions)

    def get_users_with_permissions(self, permissions):
        """Retrieve a list of users that have any of the specified permissions
        
        Users are returned as a list of usernames.
        """
      
        self.env.log.debug('HubzeroPermissionStore::get_users_with_permission() start')

        result = set()

        cursor = self.db.cursor()

        if not cursor:
          self.env.log.debug('HubzeroPermissionStore::get_users_with_permission() failed to get db cursor. end')
          self.db.disconnect()
          return list(result)

        if self.project_id == None:
          self.env.log.debug('HubzeroPermissionStore::get_user_with_permissions() no project_id. end')
          self.db.disconnect()
          return list(result)

        groups = set()
        # Dynamic IN-clause: one %s placeholder per permission, then the
        # project_id appended.  The list is passed to cursor.execute() as a
        # single parameter tuple so the driver does the quoting.
        permissions = list(permissions)
        placeholders = ','.join(['%s'] * len(permissions))
        params = tuple(permissions) + (self.project_id,)

        self.env.log.debug('HubzeroPermissionStore::get_users_with_permissions(): %r', permissions)

        # user records with these permissions
        sql_user = ('SELECT DISTINCT(j.username) FROM jos_users AS j, jos_trac_user_permission AS p '
                    'WHERE j.id=p.user_id AND p.action IN ({}) AND p.trac_project_id=%s'
                    .format(placeholders))
        self.env.log.debug('HubzeroPermissionStore::get_users_with_permission(): %s  %r', sql_user, params)
        cursor.execute(sql_user, params)
        rows = cursor.fetchall()
        for username, in rows:
          self.env.log.debug('HubzeroPermissionStore::get_users_with_permissions(): adding user ' + username)
          result.add(username)

        # user records in groups with any of these permissions
        sql_group = ('SELECT DISTINCT(u.username) FROM jos_users AS u,jos_xgroups_members AS xgm, '
                     'jos_xgroups AS xg, jos_trac_group_permission AS p '
                     'WHERE xgm.gidNumber=p.group_id AND xg.gidNumber = p.group_id '
                     'AND u.id=xgm.uidNumber AND p.action IN ({}) AND p.trac_project_id = %s'
                     .format(placeholders))
        self.env.log.debug('HubzeroPermissionStore::get_users_with_permission(): %s  %r', sql_group, params)
        cursor.execute(sql_group, params)
        rows = cursor.fetchall()
        for username, in rows:
          self.env.log.debug('HubzeroPermissionStore::get_users_with_permissions(): adding user [via group]' + username)
          result.add(username)

        self.env.log.debug('HubzeroPermissionStore::get_users_with_permission() end')
        self.db.disconnect()
        return list(result)

    def get_all_permissions(self):
        """Return all permissions for all users.

        The permissions are returned as a list of (subject, action)
        formatted tuples."""

        self.env.log.debug('HubzeroPermissionStore::get_all_permissions() for project ' + str(self.project) + ' start')

        perms = []

        cursor = self.db.cursor()

        if not cursor:
          self.env.log.debug('HubzeroPermissionStore::get_all_permissions() for project ' + str(self.project) + ' no database cursor. end')
          self.db.disconnect()
          return perms
 
        if self.project_id == None:
          self.env.log.debug('HubzeroPermissionStore::get_all_permissions() no project_id. end')
          self.db.disconnect()
          return perms

        # get permission list for each user
        self.env.log.debug('HubzeroPermissionStore::get_all_permissions(): SELECT j.username,p.action FROM jos_users AS j, jos_trac_user_permission AS p WHERE j.id = p.user_id AND p.trac_project_id = %s', self.project_id)
        cursor.execute('SELECT j.username,p.action FROM jos_users AS j, jos_trac_user_permission AS p WHERE j.id = p.user_id AND p.trac_project_id = %s', (self.project_id,))
        rows = cursor.fetchall()
        for username,action in rows:
            self.env.log.debug('HubzeroPermissionStore::get_all_permissions(): adding user permission (' + username + ',' + action + ')')
            perms.append((username,action))

        # get permission list for special anonymous user
        self.env.log.debug('HubzeroPermissionStore::get_all_permissions(): SELECT \'anonymous\',p.action FROM jos_trac_user_permission AS p WHERE p.user_id = \'0\' AND p.trac_project_id = %s', self.project_id)
        cursor.execute('SELECT \'anonymous\',p.action FROM jos_trac_user_permission AS p WHERE p.user_id = \'0\' AND p.trac_project_id = %s', (self.project_id,))
        rows = cursor.fetchall()
        for username,action in rows:
            self.env.log.debug('HubzeroPermissionStore::get_all_permissions(): adding user permission (' + username + ',' + action + ')')
            perms.append((username,action))

        # get permission list for each group
        self.env.log.debug('HubzeroPermissionStore::get_all_permissions(): SELECT xg.cn,p.action FROM jos_xgroups AS xg, jos_trac_group_permission AS p WHERE xg.gidNumber = p.group_id AND p.trac_project_id = %s', self.project_id)
        cursor.execute('SELECT xg.cn,p.action FROM jos_xgroups AS xg, jos_trac_group_permission AS p WHERE  xg.gidNumber = p.group_id AND p.trac_project_id = %s', (self.project_id,))
        rows = cursor.fetchall()
        for groupname,action in rows:
            self.env.log.debug('HubzeroPermissionStore::get_all_permissions(): adding group permission (@' + groupname + ',' + action + ')')
            perms.append(('@'+groupname,action))

        # get member list for each group
        self.env.log.debug('SELECT g.cn,u.username FROM jos_xgroups AS g, jos_xgroups_members AS gm, jos_trac_group_permission AS p, jos_users AS u WHERE u.id=gm.uidNumber AND g.gidNumber = p.group_id AND g.gidNumber=gm.gidNumber AND p.trac_project_id = %s', self.project_id)
        cursor.execute('SELECT g.cn,u.username FROM jos_xgroups AS g, jos_xgroups_members AS gm, jos_trac_group_permission AS p, jos_users AS u WHERE u.id=gm.uidNumber AND g.gidNumber = p.group_id AND g.gidNumber=gm.gidNumber AND p.trac_project_id = %s', (self.project_id,))
        rows = cursor.fetchall()
        for groupname,username in rows:
            self.env.log.debug('HubzeroPermissionStore::get_all_permissions(): adding group membership (' + username + ', @' + groupname + ')')
            perms.append((username,'@'+groupname))

        # get permission list for special authenticated group
        self.env.log.debug('HubzeroPermissionStore::get_all_permissions(): SELECT \'authenticated\',p.action FROM jos_trac_group_permission AS p WHERE p.group_id = \'0\' AND p.trac_project_id = %s', self.project_id)
        cursor.execute('SELECT \'authenticated\',p.action FROM jos_trac_group_permission AS p WHERE  p.group_id = \'0\' AND p.trac_project_id = %s', (self.project_id,))
        rows = cursor.fetchall()
        for groupname,action in rows:
            self.env.log.debug('HubzeroPermissionStore::get_all_permissions(): adding group permission (' + groupname + ',' + action + ')')
            perms.append((groupname,action))

        self.env.log.debug('HubzeroPermissionStore::get_all_permissions() for project ' + str(self.project) + ' end')
        self.db.disconnect()
        return perms

    def grant_permission(self, username, action):
        """Grants a user the permission to perform the specified action."""
        self.env.log.debug('HubzeroPermissionStore::grant_permission() start')

        cursor = self.db.cursor()

        if not cursor:
          self.env.log.debug('HubzeroPermissionStore::grant_permission() no database cursor. end')
          self.db.disconnect()
          return

        uidNumber = None
        gidNumber = None

        if action.isupper():
          if username == 'anonymous':
            uidNumber = '0'
          elif username == 'authenticated':
            gidNumber = '0'
          elif username.startswith('@'):
            username = username[1:]
            self.env.log.debug('HubzeroPermissionStore::grant_permission(): SELECT gidNumber from jos_xgroups WHERE cn=%s', username)
            cursor.execute('SELECT gidNumber from jos_xgroups WHERE cn=%s', (username,))
            row = cursor.fetchone()
            if row:
              self.env.log.debug('found group id ' + str(row[0]))
              gidNumber = str(row[0])
          else:
            self.env.log.debug('HubzeroPermissionStore::grant_permission(): SELECT id FROM jos_users WHERE username=%s', username)
            cursor.execute('SELECT id FROM jos_users WHERE username=%s', (username,))
            row = cursor.fetchone()
            if row:
              self.log.debug('found user id ' + str(row[0]))
              uidNumber = str(row[0])

          if uidNumber is not None:
            self.env.log.debug('HubzeroPermissionStore::grant_permission(): INSERT IGNORE INTO jos_trac_user_permission (user_id,action,trac_project_id) VALUE (%s,%s,%s);', uidNumber, action, self.project_id)
            cursor.execute('INSERT IGNORE INTO jos_trac_user_permission (user_id,action,trac_project_id) VALUE (%s,%s,%s);', (uidNumber, action, self.project_id))
          if gidNumber is not None:
            self.env.log.debug('HubzeroPermissionStore::grant_permission(): INSERT IGNORE INTO jos_trac_group_permission (group_id,action,trac_project_id) VALUE (%s,%s,%s);', gidNumber, action, self.project_id)
            cursor.execute('INSERT IGNORE INTO jos_trac_group_permission (group_id,action,trac_project_id) VALUE (%s,%s,%s);', (gidNumber, action, self.project_id))
        else:
          if username and username != 'anonymous' and username != 'authenticated':

            self.env.log.debug('HubzeroPermissionStore::grant_permission(): SELECT id FROM jos_users WHERE username=%s', username)
            cursor.execute('SELECT id FROM jos_users WHERE username=%s', (username,))
            row = cursor.fetchone()
            if row:
              self.log.debug('found user id' + str(row[0]))
              uidNumber = str(row[0])

            if (action.startswith('@')):
                action = action[1:]
                self.env.log.debug('HubzeroPermissionStore::grant_permission(): SELECT g.gidNumber from jos_xgroups AS g, jos_tool_groups AS tg, jos_trac_project AS proj, jos_tool AS t WHERE tg.role=1 AND g.cn=tg.cn AND t.id=tg.toolid AND CONCAT(\'app:\',t.toolname)=proj.name AND proj.id=%s AND g.cn=%s', self.project_id, action)
                cursor.execute('SELECT g.gidNumber from jos_xgroups AS g, jos_tool_groups AS tg, jos_trac_project AS proj, jos_tool AS t WHERE tg.role=1 AND g.cn=tg.cn AND t.id=tg.toolid AND CONCAT(\'app:\',t.toolname)=proj.name AND proj.id=%s AND g.cn=%s', (self.project_id, action))
                row = cursor.fetchone()
                if row:
                  self.log.debug('found group id ' + str(row[0]))
                  gidNumber = str(row[0])

            if True:
              self.env.log.info('Group membership must be managed through HUBzero in order to maintain LDAP sync')
            elif gidNumber is not None and uidNumber is not None:
              self.env.log.debug('HubzeroPermissionStore::grant_permission(): INSERT IGNORE INTO jos_xgroups_members (gidNumber, uidNumber) VALUES (%s,%s)', gidNumber, uidNumber)
              cursor.execute('INSERT IGNORE INTO jos_xgroups_members (gidNumber, uidNumber) VALUES (%s,%s)', (gidNumber, uidNumber))
              self.env.debug.info('Granted permission for %s to %s' % (action, username))
            else:
              self.env.log.info('Unknown user or group in grant request')

        self.env.log.debug('HubzeroPermissionStore::grant_permission() end')
        self.db.disconnect()

    def revoke_permission(self, username, action):
        """Revokes a users' permission to perform the specified action."""
        
        self.env.log.debug('HubzeroPermissionStore::revoke_permission() start')

        cursor = self.db.cursor()

        if not cursor:
          self.env.log.debug('HubzeroPermissionStore::revoke_permission() no database cursor. end')
          self.db.disconnect()
          return

        uidNumber = None
        gidNumber = None

        if action.isupper():
          if username == 'anonymous':
            uidNumber = '0'
          elif username == 'authenticated':
            gidNumber = '0'
          elif username.startswith('@'):
            username = username[1:]
            self.env.log.debug('HubzeroPermissionStore::revoke_permission(): SELECT gidNumber from jos_xgroups WHERE cn=%s', username)
            cursor.execute('SELECT gidNumber from jos_xgroups WHERE cn=%s', (username,))
            row = cursor.fetchone()
            if row:
              self.log.debug('found group id ' + str(row[0]))
              gidNumber = str(row[0])
          else:
            self.env.log.debug('HubzeroPermissionStore::revoke_permission(): SELECT id FROM jos_users WHERE username=%s', username)
            cursor.execute('SELECT id FROM jos_users WHERE username=%s', (username,))
            row = cursor.fetchone()
            if row:
              self.log.debug('found user id ' + str(row[0]))
              uidNumber = str(row[0])

          if uidNumber is not None:
            self.env.log.debug('HubzeroPermissionStore::revoke_permission(): DELETE FROM jos_trac_user_permission WHERE trac_project_id=%s AND user_id=%s AND action=%s;', self.project_id, uidNumber, action)
            cursor.execute('DELETE FROM jos_trac_user_permission WHERE trac_project_id=%s AND user_id=%s AND action=%s;', (self.project_id, uidNumber, action))
          if gidNumber is not None:
            self.env.log.debug('HubzeroPermissionStore::revoke_permission(): DELETE FROM jos_trac_group_permission WHERE trac_project_id=%s AND group_id=%s AND action=%s;', self.project_id, gidNumber, action)
            cursor.execute('DELETE FROM jos_trac_group_permission WHERE trac_project_id=%s AND group_id=%s AND action=%s;', (self.project_id, gidNumber, action))

        else:

          if username and username != 'anonymous' and username != 'authenticated':

            self.env.log.debug('HubzeroPermissionStore::revoke_permission(): SELECT id FROM jos_users WHERE username=%s', username)
            cursor.execute('SELECT id FROM jos_users WHERE username=%s', (username,))
            row = cursor.fetchone()
            if row:
              self.log.debug('found user id ' + str(row[0]))
              uidNumber = str(row[0])

            if action.startswith('@'):
              action = action[1:]
            self.env.log.debug('HubzeroPermissionStore::grant_permission(): SELECT g.gidNumber from jos_xgroups AS g, jos_tool_groups AS tg, jos_trac_project AS proj, jos_tool AS t WHERE tg.role=1 AND g.cn=tg.cn AND t.id=tg.toolid AND CONCAT(\'app:\',t.toolname)=proj.name AND proj.id=%s AND g.cn=%s', self.project_id, action)
            cursor.execute('SELECT g.gidNumber from jos_xgroups AS g, jos_tool_groups AS tg, jos_trac_project AS proj, jos_tool AS t WHERE tg.role=1 AND g.cn=tg.cn AND t.id=tg.toolid AND CONCAT(\'app:\',t.toolname)=proj.name AND proj.id=%s AND g.cn=%s', (self.project_id, action))
            row = cursor.fetchone()
            if row:
              self.log.debug('found group id ' + str(row[0]))
              gidNumber = str(row[0])

            if True:
              self.env.log.info('Group membership must be managed through HUBzero in order to maintain LDAP sync')
            elif gidNumber is not None and uidNumber is not None:
              self.env.log.debug('HubzeroPermissionStore::revoke_permission(): DELETE FROM jos_xgroups_members WHERE gidNumber=%s AND uidNumber=%s', gidNumber, uidNumber)
              cursor.execute('DELETE FROM jos_xgroups_members WHERE gidNumber=%s AND uidNumber=%s', (gidNumber, uidNumber))
              self.env.log.debug('HubzeroPermissionStore::revoke_permission(): DELETE FROM jos_xgroups_managers WHERE gidNumber=%s AND uidNumber=%s', gidNumber, uidNumber)
              cursor.execute('DELETE FROM jos_xgroups_managers WHERE gidNumber=%s AND uidNumber=%s', (gidNumber, uidNumber))
              self.log.debug('Revoked permission for %s to %s' % (action, username))
            else:
              self.log.info('Unknown group or user in revoke request')

        self.env.log.debug('HubzeroPermissionStore::revoke_permission() end')
        self.db.disconnect()

class HubzeroPermissionGroupProvider(Component):
    """Provides the basic builtin permission groups 'anonymous' and 'authenticated' and HUBzero groups."""

    implements(IPermissionGroupProvider)

    def __init__(self):
        self.env.log.debug('HubzeroPermissionGroupProvider::__init__() start')
        self.db = HubzeroDatabaseConnection(self.env)
        self.project = self.env.config.get('project', 'name')
        self.db = HubzeroDatabaseConnection(self.env)
        self.env.log.debug('HubzeroPermissionGroupProvider::__init__() end')

    def __del__(self):
      self.env.log.debug('HubzeroPermissionGroupProvider::__del__() start')
      self.db.disconnect()
      self.env.log.debug('HubzeroPermissionGroupProvider::__del__() end')

    def get_permission_groups(self, username):
        self.env.log.debug('HubzeroPermissionGroupProvider::get_permission_groups(' + username + ') start')

        groups = ['anonymous']
        
        if username and username != 'anonymous':
            groups.append('authenticated')

        cursor = self.db.cursor()

        if not cursor:
          self.env.log.debug('HubzeroPermissionGroupProvider::get_permission_groups(' + username + ') no database cursor. end')
          self.db.disconnect()
          return groups

        if username and username != 'anonymous':
            # NOTE: this query also references an undefined `proj` alias in
            # WHERE / FROM -- a pre-existing bug fixed in a separate commit.
            self.env.log.debug('HubzeroPermissionGroupProvider::get_permission_groups(%s): SELECT g.cn,p.action FROM jos_xgroups AS g, jos_xgroups_members AS m, jos_trac_group_permission AS p, jos_users AS u WHERE proj.name = %s AND m.uidNumber = u.id AND u.username = %s AND m.gidNumber = g.gidNumber AND g.gidNumber = p.group_id AND proj.id = p.trac_project_id', username, self.project, username)
            cursor.execute('SELECT g.cn,p.action FROM jos_xgroups AS g, jos_xgroups_members AS m, jos_trac_group_permission AS p, jos_users AS u WHERE proj.name = %s AND m.uidNumber = u.id AND u.username = %s AND m.gidNumber = g.gidNumber AND g.gidNumber = p.group_id AND proj.id = p.trac_project_id', (self.project, username))
            rows = cursor.fetchall()
            for groupname,action in rows:
              self.env.log.debug('HubzeroPermissionStore::get_permission_groups(' + username + '): adding group (' + groupname + ')')
              groups.append('@' + groupname)

        self.db.disconnect()
        self.env.log.debug('HubzeroPermissionGroupProvider::get_permission_groups(' + username + ') end')
        return groups
