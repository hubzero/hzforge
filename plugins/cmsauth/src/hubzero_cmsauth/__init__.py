"""hubzero-trac-cmsauth -- HUBzero CMS single sign-on for Trac.

Trac plugin that bridges the HUBzero CMS session into Trac's request
context.  Two components:

* `HubzeroSessionAuthenticator` (IAuthenticator) -- forwards the browser's
  Cookie header to `/api/v1.1/members/currentuser` on the local CMS;
  the API endpoint reads the session cookie, looks up `jos_session`, and
  returns the profile (with `username`).  Trac uses that as REMOTE_USER.

* `HubzeroLoginModule` (IRequestHandler) -- claims Trac's `/login` and
  `/logout` and 302-redirects them to the CMS's matching endpoints, with
  the Trac return URL base64-encoded into the `return` query param.

No third-party dependencies: pure stdlib (http.client + json + ssl).
Works on Py2.7 and Py3.6+.
"""
from hubzero_cmsauth.api import *  # noqa: F401, F403
