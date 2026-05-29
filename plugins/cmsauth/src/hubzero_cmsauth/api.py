"""HUBzero CMS single sign-on for Trac (hubzero-trac-cmsauth 1.0.0).

This is a thin **subclass of `trac.web.auth.LoginModule`**.  It reuses all
of Trac's canonical authentication machinery -- the `auth_cookie` table,
the `trac_auth` cookie set/expire/validate flow, the `_redirect_back`
post-action redirect, and the `IAuthenticator`/`IRequestHandler`/
`INavigationContributor` interface implementations -- and only overrides
the hooks that need to know about HUBzero:

* `authenticate(req)` -- on cold cache (no REMOTE_USER, no trac_auth
  cookie), forward the browser's `Cookie:` header to the local CMS at
  `/api/v1.1/members/currentuser` and use the returned `profile.username`
  as `req.environ['REMOTE_USER']`.  Parent's `authenticate()` then
  routes that through the standard REMOTE_USER path.

* `_do_login(req)` -- if `req.remote_user` is set (Apache LDAP or our
  authenticate() above), let parent's `_do_login` run -- it INSERTs into
  `auth_cookie` and sets the `trac_auth` cookie.  Otherwise redirect to
  HUBzero's `/login?return=<base64(/tools/<env>/login)>`; the CMS
  authenticates and bounces back to our `/login`, where authenticate()
  resolves the now-present CMS session.

* `_do_logout(req)` -- bypass parent's POST-method gate (parent's CSRF
  protection assumes the chrome's logout form posts; we additionally
  accept GET so a CMS-side logout link works), still run the parent's
  `DELETE FROM auth_cookie` + cookie-expiry.  Falls through to
  `_redirect_back` for the actual redirect.

* `_redirect_back(req)` -- after a successful /login, send the user to
  the env's wiki home (or the referer).  After /logout, redirect to
  HUBzero's `/logout?return=<base64(/tools/<env>/wiki)>` so the CMS also
  clears its session, then bounces back to Trac as anonymous.

This pattern follows AccountManagerPlugin's design
(https://github.com/SpamExperts/AccountManagerPlugin/blob/master/acct_mgr/web_ui.py)
and Trac's own `trac.web.auth.LoginModule`
(https://trac.edgewall.org/browser/trunk/trac/web/auth.py).

No third-party dependencies: pure stdlib (http.client + json + ssl).
Works on Py2.7 and Py3.6+.

trac.ini per env should pair this with parent disabled:
    [components]
    hubzero_cmsauth.* = enabled
    trac.web.auth.LoginModule = disabled
"""
from __future__ import absolute_import, division, print_function, unicode_literals

import base64
import json
import logging
import re
import ssl

try:                                      # Py3
    from http.client import HTTPSConnection, HTTPConnection
except ImportError:                       # Py2
    from httplib import HTTPSConnection, HTTPConnection   # noqa: F401

from trac.config import IntOption, Option
from trac.web.auth import LoginModule


log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------- #
# HubzeroSSOLoginModule
# ---------------------------------------------------------------------------- #
class HubzeroSSOLoginModule(LoginModule):
    """SSO via HUBzero CMS session API.  Subclass of trac.web.auth.LoginModule;
    overrides only the hooks that need to know about HUBzero."""

    # --- CMS API transport ------------------------------------------------- #
    # No host/port/scheme knobs by design: the API call uses the exact same
    # scheme + host + port the browser used to reach Trac (read from the
    # incoming request's wsgi.url_scheme + HTTP_HOST).  Zero per-env config;
    # if the user can reach Trac, the CMS API is reachable at the same
    # origin by definition.
    api_path = Option(
        "hubzero_cmsauth", "api_path",
        "/api/v1.1/members/currentuser",
        "Path on the CMS that returns the current user's profile as JSON "
        "(authenticated by the Cookie header).  Defaults to the existing "
        "com_members v1.1 currentuser endpoint.",
    )
    api_timeout_seconds = IntOption(
        "hubzero_cmsauth", "api_timeout_seconds", 5,
        "Per-request timeout for the CMS API call.  Loopback responses are "
        "sub-millisecond; the timeout is a safety net against API hangs.",
    )

    # --- CMS redirect URLs ------------------------------------------------- #
    cms_login_url  = Option(
        "hubzero_cmsauth", "cms_login_url",  "/login",
        "Path on the CMS host to which Trac /login redirects when there's "
        "no CMS session yet (with ?return=<base64(/tools/<env>/login)> "
        "appended so the CMS bounces the user back to us after auth).",
    )
    cms_logout_url = Option(
        "hubzero_cmsauth", "cms_logout_url", "/logout",
        "Path on the CMS host to which Trac /logout redirects (with "
        "?return=<base64(/tools/<env>/wiki)> so the CMS bounces the user "
        "back to the env wiki as anonymous after logout).",
    )

    # -- IAuthenticator (override) ----------------------------------------- #

    def authenticate(self, req):
        """Return the username, or None for anonymous.

        Three paths in order:
            (1) REMOTE_USER already set (e.g. Apache LDAP, or our own
                previous call within this request) -> parent honors it
            (2) trac_auth cookie present -> parent looks it up in
                auth_cookie table (the standard Trac fast path)
            (3) neither -> try the CMS API.  If it returns a username,
                set REMOTE_USER in the environ and let parent take the
                'standard REMOTE_USER' path.
        """
        if (not req.environ.get("REMOTE_USER")
                and "trac_auth" not in req.incookie):
            name = self._authenticate_via_cms(req)
            if name:
                req.environ["REMOTE_USER"] = name
        return LoginModule.authenticate(self, req)

    # -- IRequestHandler hooks (override) ---------------------------------- #

    def _do_login(self, req):
        """Override: if no REMOTE_USER, bounce to CMS login.  Otherwise let
        parent INSERT auth_cookie + set trac_auth (the canonical path)."""
        if not req.remote_user:
            # Try once more via the CMS API -- the user may have arrived at
            # /login carrying a CMS session cookie set by the parent CMS.
            name = self._authenticate_via_cms(req)
            if name:
                req.environ["REMOTE_USER"] = name
        if not req.remote_user:
            # Still no identity -> redirect to CMS login.  After CMS auth,
            # the CMS will send the user back to our /login with the CMS
            # session cookie; we'll come through here again, find REMOTE_USER,
            # and fall through to the parent _do_login below.
            self._redirect_to_cms(req, self.cms_login_url,
                                  return_to=req.href.login())
        # Parent handles: DELETE old rows, INSERT new auth_cookie row, set
        # the trac_auth cookie morsel.  process_request() then calls
        # _redirect_back() which we override.
        LoginModule._do_login(self, req)

    def _do_logout(self, req):
        """Override: bypass parent's POST-only and anonymous gates, but
        still do the canonical DELETE + cookie expire.  Falls through to
        _redirect_back() for the actual redirect."""
        # NB parent's behavior we DO want: DELETE the row from auth_cookie
        # and call _expire_cookie() to set the trac_auth cookie to expire.
        # The behavior we DO NOT want: returning early on non-POST or
        # anonymous (we want GET-from-anywhere to redirect to CMS logout).
        if "trac_auth" in req.incookie:
            self.env.db_transaction(
                "DELETE FROM auth_cookie WHERE cookie=%s",
                (req.incookie["trac_auth"].value,))
        elif req.authname and req.authname != "anonymous":
            self.env.db_transaction(
                "DELETE FROM auth_cookie WHERE name=%s",
                (req.authname,))
        self._expire_cookie(req)
        # process_request() then calls _redirect_back(), which we override.

    def _redirect_back(self, req):
        """Override: send the user where THIS plugin wants them after /login
        or /logout, instead of parent's same-host-referer logic.

        Open-redirect guard: `?referer=` is honored ONLY if it points back
        to the same origin (our scheme+host).  Cross-origin / protocol-
        relative / scheme-mismatched targets fall back to the env wiki
        home.  This mirrors stock trac.web.auth.LoginModule's same-site
        check (auth.py:268-269); without it, an attacker could send a
        just-logged-in user to any URL via
        /login?referer=https://evil.com/phish."""
        if req.path_info.rstrip("/") == "/logout":
            self._redirect_to_cms(req, self.cms_logout_url,
                                  return_to=req.href.wiki())
        # /login -- successful auth path.
        target = req.args.get("referer")
        if not self._is_same_origin(req, target):
            target = req.href.wiki()
        req.redirect(target)

    # -- CMS HTTP transport ------------------------------------------------- #

    def _authenticate_via_cms(self, req):
        """Read the browser's Cookie header, forward it to the CMS API,
        return the resolved username.  Any error -> None (fail-safe)."""
        cookie_header = self._cookie_header(req)
        if not cookie_header:
            return None
        try:
            return self._call_api(req, cookie_header)
        except Exception as e:                                          # noqa: BLE001
            log.warning("hubzero_cmsauth: API lookup failed (%s); "
                        "treating request as anonymous", e)
            return None

    def _call_api(self, req, cookie_header):
        """Open a one-shot HTTP(S) connection to the same scheme+host+port
        the browser used to reach Trac, forward the cookies, return the
        response's `profile.username` (or None for 401/403 anonymous)."""
        env_ = getattr(req, "environ", None) or {}
        scheme = (env_.get("wsgi.url_scheme") or "https").lower()
        host_header = env_.get("HTTP_HOST") or env_.get("SERVER_NAME") or "localhost"
        # HTTP_HOST may carry an explicit port ("foo:8443"); split it off
        # for the connect target but keep it intact for the Host header.
        if ":" in host_header:
            host, _, port_s = host_header.partition(":")
            try:
                port = int(port_s)
            except ValueError:
                port = 443 if scheme == "https" else 80
        else:
            host = host_header
            port = 443 if scheme == "https" else 80
        if scheme == "https":
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE   # same-origin -- no MITM risk
            conn = HTTPSConnection(host, port,
                                   timeout=self.api_timeout_seconds,
                                   context=ctx)
        else:
            conn = HTTPConnection(host, port,
                                  timeout=self.api_timeout_seconds)
        try:
            conn.request("GET", self.api_path, headers={
                "Host":   host_header,
                "Cookie": cookie_header,
                "Accept": "application/json",
            })
            resp = conn.getresponse()
            status = resp.status
            body = resp.read()
        finally:
            conn.close()

        if status in (401, 403):
            return None
        if status != 200:
            raise RuntimeError("CMS API returned HTTP %d" % status)
        try:
            payload = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as e:
            raise RuntimeError("CMS API returned non-JSON body: %s" % e)
        profile = payload.get("profile") if isinstance(payload, dict) else None
        if not isinstance(profile, dict):
            raise RuntimeError("CMS API response missing 'profile' object")
        username = profile.get("username")
        return username or None

    # -- helpers ------------------------------------------------------------ #

    @staticmethod
    def _cookie_header(req):
        """Raw Cookie header as the browser sent it (parent's req.incookie
        is parsed; the API call wants the original string)."""
        env = getattr(req, "environ", None) or {}
        raw = env.get("HTTP_COOKIE") or ""
        if raw:
            return raw
        try:
            return req.incookie.output(header="", sep="; ").strip()
        except AttributeError:
            return ""

    def _redirect_to_cms(self, req, cms_path, return_to):
        """302 to `<cms_scheme>://<cms_host><cms_path>?return=<base64(return_to)>`.
        CMS scheme + host come from the incoming request -- same origin."""
        env_ = getattr(req, "environ", None) or {}
        scheme = env_.get("wsgi.url_scheme", "https")
        host = env_.get("HTTP_HOST") or env_.get("SERVER_NAME", "")
        target = "%s://%s%s?return=%s" % (
            scheme, host, cms_path, self._b64(return_to))
        req.redirect(target)

    @staticmethod
    def _is_same_origin(req, target):
        """True iff `target` is a redirect URL pointing back at OUR
        scheme+host: either a same-host absolute URL, or a host-relative
        path.  Used to guard `_redirect_back` against open-redirect via
        `?referer=https://evil.com/`.

        Same as stock trac.web.auth.LoginModule's check: the redirect
        either lives under `req.base_url` (same scheme+host prefix), or
        is a path that the browser will interpret relative to our host.

        False for: None / empty / protocol-relative (`//evil.com/x`,
        which the browser would resolve to evil.com under the current
        scheme) / different-scheme-or-host absolute URLs."""
        if not target:
            return False
        # Protocol-relative ("//example.com/x") is NOT same-origin: the
        # browser would resolve it against the current scheme but point
        # at example.com -- exactly the open-redirect vector we're closing.
        if target.startswith("//"):
            return False
        # Host-relative path -- the browser will resolve it against our
        # current scheme+host, so it's same-origin by construction.
        if target.startswith("/"):
            return True
        # Absolute URL: must match our scheme+host exactly.  Build the
        # same-origin prefix from the incoming request.
        env_ = getattr(req, "environ", None) or {}
        scheme = (env_.get("wsgi.url_scheme") or "https").lower()
        host = env_.get("HTTP_HOST") or env_.get("SERVER_NAME") or ""
        if not host:
            return False
        prefix = "%s://%s" % (scheme, host)
        # Exact match (e.g. "https://help.hubzero.org") or anything under
        # our root path ("https://help.hubzero.org/..."); reject lookalike
        # hosts like "https://help.hubzero.org.evil.com" by requiring the
        # next char after the prefix to be "/" or end-of-string.
        return target == prefix or target.startswith(prefix + "/")

    @staticmethod
    def _b64(s):
        """URL-safe base64 (RFC 4648 Section 5: `-`/`_` instead of `+`/`/`).

        The encoded string lands in a query parameter (`?return=<b64>`),
        so the standard alphabet would corrupt `+` to space when the CMS
        decodes the query string.  PHP's `base64_decode()` accepts both
        alphabets by default, so this is safe on the receiving end."""
        return base64.urlsafe_b64encode(s.encode("utf-8")).decode("ascii")
