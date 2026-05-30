#
# @package      hubzero-forge
# @file         link.py.in
# @copyright    Copyright (c) 2006-2020 The Regents of the University of California.
# @license      http://opensource.org/licenses/MIT MIT
#
# Copyright (c) 2006-2020 The Regents of the University of California.
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

#
# Simple link facility
#
# This macro makes it possible to create links to things like the
# TRAC Ticket System.  For this, you need a link like
# "/tools/name/report", but the usual [http: ...] and
# [wiki: ...] notations just won't work.  Instead, you can
# use [[link(/report Ticket System)]].
#

from __future__ import absolute_import, division, print_function, unicode_literals

from trac.wiki.macros import WikiMacroBase


def _escape(s):
    """HTML-escape the 5 significant characters, in the correct order
    (`&` first).  Identical to what stdlib `html.escape`/`markupsafe`
    produce; safe in BOTH text and double/single-quoted attribute
    contexts.  Used instead of Trac's `tag`/`Markup` so the macro is
    provably safe AND testable without a Trac install (Py2.7 + Py3.6).

    A Trac macro's returned string is inserted into the page as markup
    WITHOUT further escaping -- so every wiki-author-controlled value
    interpolated into the output must be escaped here, or it's stored
    XSS."""
    return (s.replace("&", "&amp;")
             .replace("<", "&lt;")
             .replace(">", "&gt;")
             .replace('"', "&quot;")
             .replace("'", "&#39;"))


class linkMacro(WikiMacroBase):
  """Inserts internal project link."""

  revision = "1.0"
  url = "http://hubzero.org"

  def expand_macro(self, formatter, name, args):
    if not args:
        return ''
    alist = args.split()
    if not alist:
        return ''
    link = alist[0]
    rest = alist[1:]
    if not link.startswith('/'):
        link = '/' + link
    # href is always rooted at the trusted abs_href() base + a path that
    # we force to start with "/", so no javascript:/scheme injection is
    # possible; HTML-escaping then prevents attribute breakout.  The link
    # text (wiki-author-controlled) is escaped for the text context.
    href = _escape(self.env.abs_href() + link)
    text = _escape(" ".join(rest))
    return "<a class=\"ext-link\" href=\"%s\">%s</a>" % (href, text)
