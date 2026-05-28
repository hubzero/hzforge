"""Test fixtures for hubzero-trac-macros.

The two macros only depend on `trac.wiki.macros.WikiMacroBase` (the base
class -- they inherit from it but don't call any of its methods) and on
`self.env.abs_href()` (which Trac sets on the env at request time, and
the macros use to build URLs that work no matter which env is serving
the current request).  Stubs for both let the tests run on any Python
without Trac installed.
"""
from __future__ import absolute_import, division, print_function, unicode_literals

import pathlib
import sys
import types


# --- src/ on sys.path (so the plugin source is importable regardless of
# pytest's rootdir -- works whether pytest is invoked from this plugin's
# directory or from the repo root with multiple test paths). ---

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / 'src'))


# --- Trac stubs (must run before any test imports hubzero_macros.*) ---

for _mod in ('trac', 'trac.wiki', 'trac.wiki.macros'):
    sys.modules.setdefault(_mod, types.ModuleType(_mod))
sys.modules['trac.wiki.macros'].WikiMacroBase = type(str('WikiMacroBase'), (), {})


import pytest   # noqa: E402  (must follow the trac stubs above)


# --- fake trac env (the only env attribute the macros use is abs_href()) ---

class _FakeEnv(object):
    def __init__(self, abs_href='/tools/myenv'):
        self._abs_href_value = abs_href
    def abs_href(self):
        return self._abs_href_value


@pytest.fixture
def env():
    """A minimal stand-in for a Trac env: just `.abs_href()`."""
    return _FakeEnv()


@pytest.fixture
def image_macro(env):
    """An imageMacro instance with .env preset (matching what Trac's
    Component framework does at request time)."""
    from hubzero_macros.image import imageMacro
    m = imageMacro.__new__(imageMacro)
    m.env = env
    return m


@pytest.fixture
def link_macro(env):
    """A linkMacro instance with .env preset."""
    from hubzero_macros.link import linkMacro
    m = linkMacro.__new__(linkMacro)
    m.env = env
    return m
