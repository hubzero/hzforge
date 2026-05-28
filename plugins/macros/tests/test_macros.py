"""Tests for hubzero_macros.image.imageMacro and hubzero_macros.link.linkMacro.

The macros are env-agnostic (build URLs from `self.env.abs_href()` at
request time, no hardcoded env name).  These tests verify the rendered
HTML for the basic / already-slashed / different-env paths.
"""
from __future__ import absolute_import, division, print_function, unicode_literals


# -----------------------------------------------------------------------------
# imageMacro
# -----------------------------------------------------------------------------

def test_image_macro_basic(image_macro):
    """[[image foo.png]]  ->  <img src="<abs_href>/attachment/wiki/Images/foo.png?format=raw" />"""
    out = image_macro.expand_macro(formatter=None, name='image', args='foo.png')
    assert out == '<img src="/tools/myenv/attachment/wiki/Images/foo.png?format=raw" />'


def test_image_macro_with_leading_slash(image_macro):
    """[[image /foo.png]]  -- the leading slash is idempotent (no `//`)."""
    out = image_macro.expand_macro(formatter=None, name='image', args='/foo.png')
    assert out == '<img src="/tools/myenv/attachment/wiki/Images/foo.png?format=raw" />'


def test_image_macro_uses_env_abs_href(env, image_macro):
    """A different env (different abs_href) renders the same macro at the new prefix."""
    env._abs_href_value = '/tools/different'
    out = image_macro.expand_macro(formatter=None, name='image', args='x.png')
    assert out == '<img src="/tools/different/attachment/wiki/Images/x.png?format=raw" />'


# -----------------------------------------------------------------------------
# linkMacro
# -----------------------------------------------------------------------------

def test_link_macro_basic(link_macro):
    """[[link(/report Ticket System)]]  ->  <a class="ext-link" href="<abs_href>/report">Ticket System</a>"""
    out = link_macro.expand_macro(formatter=None, name='link', args='/report Ticket System')
    assert out == '<a class="ext-link" href="/tools/myenv/report">Ticket System</a>'


def test_link_macro_adds_leading_slash(link_macro):
    """[[link(report text)]]  -- the macro inserts the leading `/` if absent."""
    out = link_macro.expand_macro(formatter=None, name='link', args='report Ticket System')
    assert out == '<a class="ext-link" href="/tools/myenv/report">Ticket System</a>'


def test_link_macro_uses_env_abs_href(env, link_macro):
    env._abs_href_value = '/tools/different'
    out = link_macro.expand_macro(formatter=None, name='link', args='/page text')
    assert out == '<a class="ext-link" href="/tools/different/page">text</a>'


def test_link_macro_joins_multiword_text(link_macro):
    """The `text` after the path is everything after the first token, space-joined."""
    out = link_macro.expand_macro(formatter=None, name='link', args='/x one two three')
    assert out == '<a class="ext-link" href="/tools/myenv/x">one two three</a>'
