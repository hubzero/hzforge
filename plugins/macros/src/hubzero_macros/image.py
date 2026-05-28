#
# @package      hubzero-forge
# @file         image.py.in
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
# Simple image facility
#
# This macro makes it easy to upload images onto the wiki via
# the "Attach" button, and then integrate the images onto another
# wiki page.  Just create a wiki page called "Images" and start
# uploading image files to that area.  Then, include [[image name]]
# on other wiki pages, where "name" is the name of the image file.
#
from __future__ import absolute_import, division, print_function, unicode_literals

from trac.wiki.macros import WikiMacroBase


class imageMacro(WikiMacroBase):
  """Inserts image."""

  revision = "1.0"
  url = "http://hubzero.org"

  def expand_macro(self, formatter, name, args):
    alist = args.split()
    link = alist[0]
    rest = alist[1:]
    if not link.startswith('/'):
        link = '/' + link
    return "<img src=\"%s/attachment/wiki/Images%s?format=raw\" />" % (self.env.abs_href(),link)
