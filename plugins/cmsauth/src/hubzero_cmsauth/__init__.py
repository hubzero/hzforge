"""hubzero-trac-cmsauth -- HUBzero CMS single sign-on for Trac.

A thin subclass of `trac.web.auth.LoginModule` that bridges Trac's
canonical auth flow to a HUBzero CMS session.  See `api.py` for the
full design.  No third-party dependencies; works on Py2.7 + Py3.6+.
"""
from hubzero_cmsauth.api import *  # noqa: F401, F403
