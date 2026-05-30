"""Tests for hubzero_macros.image.imageMacro and hubzero_macros.link.linkMacro.

The macros are env-agnostic (build URLs from `self.env.abs_href()` at
request time, no hardcoded env name).  These tests verify the rendered
HTML for the basic / already-slashed / different-env paths.
"""
from __future__ import absolute_import, division, print_function, unicode_literals

import pytest


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


# -----------------------------------------------------------------------------
# Stored-XSS regression (0.1.1).  A Trac macro's returned string is inserted
# into the page as markup WITHOUT further escaping, so every wiki-author-
# controlled value interpolated into the output must be HTML-escaped here.
# The pre-0.1.1 code used raw %-format and was exploitable by anyone with
# WIKI_MODIFY.
# -----------------------------------------------------------------------------

def test_link_escapes_text_xss(link_macro):
    """A script payload in the link TEXT is escaped, not emitted as markup."""
    out = link_macro.expand_macro(
        formatter=None, name='link', args='/x <img src=x onerror=alert(1)>')
    assert "<img" not in out                  # payload tag neutralized
    assert "&lt;img" in out
    assert out.startswith('<a class="ext-link"')


def test_link_escapes_attribute_breakout(link_macro):
    """A double-quote in the PATH token must not break out of href."""
    out = link_macro.expand_macro(
        formatter=None, name='link', args='/x"><script>alert(1)</script>')
    assert "<script>" not in out
    assert "&quot;" in out                    # breakout quote escaped
    assert "&lt;script&gt;" in out


def test_image_escapes_attribute_breakout(image_macro):
    """A double-quote in the image name must not break out of src (the
    original [[image("onerror=alert(1) x=]] bug)."""
    out = image_macro.expand_macro(
        formatter=None, name='image', args='"onerror=alert(1) x=')
    assert '"onerror' not in out              # raw breakout quote gone
    assert "&quot;" in out
    assert out.count('"') == 2               # only the two src="" delimiters


def test_link_ampersand_escaped_not_double(link_macro):
    """`&` -> &amp; exactly once (correct order: & escaped first)."""
    out = link_macro.expand_macro(formatter=None, name='link', args='/a&b text&more')
    assert "&amp;b" in out and "text&amp;more" in out
    assert "&amp;amp;" not in out             # not double-escaped


@pytest.mark.parametrize("bad", ['', '   ', None])
def test_link_empty_args_no_crash(link_macro, bad):
    """Was IndexError (empty) / AttributeError (None) pre-0.1.1."""
    assert link_macro.expand_macro(formatter=None, name='link', args=bad) == ''


@pytest.mark.parametrize("bad", ['', '   ', None])
def test_image_empty_args_no_crash(image_macro, bad):
    assert image_macro.expand_macro(formatter=None, name='image', args=bad) == ''
