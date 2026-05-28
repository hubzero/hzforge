"""HUBzero CMS single sign-on for Trac (hubzero-trac-cmsauth 1.0.0).

This module defines the two Trac Components the plugin ships:

  HubzeroSessionAuthenticator (IAuthenticator)
    Auth flow per request:
      1. trac_auth cookie present?  Look it up in Trac's local auth_cookie
         table; on hit, return the name (no API call -- this is the steady-
         state common case).
      2. Otherwise, forward the browser's Cookie header to the local CMS at
         `/api/v1.1/members/currentuser`.  HTTPS to 127.0.0.1 with the right
         Host header so Apache's name-based vhost routing lands on the CMS
         vhost.  The CMS reads its own session cookie, looks up jos_session,
         returns the profile JSON.
      3. On API success, INSERT (cookie, username, ip, time) into auth_cookie,
         set trac_auth on the outgoing response, return the username.  All
         subsequent requests from this browser take path 1 -- no API hop.

    Failures are conservative: 403/401 from the CMS (guest), 4xx/5xx, network
    error, malformed JSON, missing 'username' field -- all become "anonymous"
    (return None).  We NEVER grant access on an API error.

  HubzeroLoginModule (IRequestHandler + INavigationContributor)
    Owns Trac's /login (302 -> CMS /login?return=<base64>) and /logout (DELETE
    the auth_cookie row + clear trac_auth + 302 -> CMS /logout?return=<base64>).
    Replaces Trac's built-in trac.web.auth.LoginModule; the plugin's trac.ini
    enable line should be paired with
        [components] trac.web.auth.LoginModule = disabled

No third-party dependencies.  Pure stdlib (http.client + json + ssl + os +
binascii + base64 + re + time).  Works on Py2.7 and Py3.6+.
"""
from __future__ import absolute_import, division, print_function, unicode_literals

import base64
import binascii
import json
import logging
import os
import re
import ssl
import time

try:                                      # Py3
    from http.client import HTTPSConnection, HTTPConnection
except ImportError:                       # Py2
    from httplib import HTTPSConnection, HTTPConnection   # noqa: F401

from trac.core import Component, implements
from trac.config import BoolOption, IntOption, Option
from trac.web.api import IAuthenticator, IRequestHandler
from trac.web.chrome import INavigationContributor


log = logging.getLogger(__name__)


# Cookie names + attributes must be native `str` on Py2 (the stdlib's Cookie
# module uses string.translate(key, idmap, LegalChars), which raises
# TypeError on a unicode key -- and `from __future__ import unicode_literals`
# above makes every bare literal unicode).  These constants force the right
# type on both interpreter majors (str() is a no-op on Py3, unicode->bytes on
# Py2).
_TRAC_AUTH        = str("trac_auth")
_ATTR_PATH        = str("path")
_ATTR_SECURE      = str("secure")
_ATTR_HTTPONLY    = str("httponly")
_ATTR_EXPIRES     = str("expires")
_EXPIRES_EPOCH    = str("Thu, 01 Jan 1970 00:00:00 GMT")


# ---------------------------------------------------------------------------- #
# Authenticator
# ---------------------------------------------------------------------------- #
class HubzeroSessionAuthenticator(Component):
    """Trac IAuthenticator backed by trac_auth (fast path) + HUBzero CMS API
    (slow path, only when there's no valid trac_auth cookie).

    Config options live in `[hubzero_cmsauth]` in trac.ini.  Sensible
    defaults for the help host's layout; an operator only sets cms_base_url
    or use_https in a non-loopback deployment."""

    implements(IAuthenticator)

    # --- CMS API transport ------------------------------------------------- #
    # No host/port/scheme knobs by design: the API call uses the exact same
    # scheme + host + port the browser used to reach Trac (read from the
    # incoming request's wsgi.url_scheme + HTTP_HOST).  This means there's
    # nothing to configure per-env and nothing that can drift between Trac
    # and the CMS -- if the user can reach Trac at https://help.hubzero.org,
    # the CMS API is reachable at https://help.hubzero.org/api/... by
    # definition.  The trade-off vs loopback is one extra Apache hop per
    # API call; that's amortized to ~one call per browser session by the
    # trac_auth cookie path.
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

    # --- trac_auth cookie attributes -------------------------------------- #
    check_auth_ip = BoolOption(
        "hubzero_cmsauth", "check_auth_ip", True,
        "If true, the trac_auth cookie is only honored when the client IP "
        "matches the one recorded at issue time.  Mirrors Trac's built-in "
        "[trac] check_auth_ip behavior.  Disable for environments where "
        "users legitimately change IP (mobile networks, NAT pools).",
    )
    auth_cookie_path = Option(
        "hubzero_cmsauth", "auth_cookie_path", "",
        "Path attribute for the trac_auth cookie.  Empty (default) means "
        "'use the env's href root', which scopes the cookie to /tools/<env>/ "
        "so it doesn't leak to other Trac envs on the same host.",
    )

    # --- helpers ----------------------------------------------------------- #

    def _new_cookie_value(self):
        """A random 40-char hex cookie value.  Matches Trac's hex_entropy()
        output shape (binascii.hexlify of 20 bytes); using os.urandom directly
        avoids depending on trac.util internals."""
        return binascii.hexlify(os.urandom(20)).decode("ascii")

    # -- IAuthenticator ----------------------------------------------------- #

    def authenticate(self, req):
        """Return the authenticated username, or None for anonymous.

        Trac calls this on every request.  We take three paths:
            (1) trac_auth cookie present + valid -> return its name (fast)
            (2) no/invalid trac_auth + has CMS Cookie -> ask CMS API (slow)
            (3) neither -> anonymous
        Path 2 also sets a fresh trac_auth on the response, so subsequent
        requests from the same browser take path 1."""
        name = self._name_from_trac_auth(req)
        if name:
            return name

        cookie_header = self._cookie_header(req)
        if not cookie_header:
            return None

        try:
            name = self._call_api(req, cookie_header)
        except Exception as e:                                          # noqa: BLE001
            # Conservative: any error -> anonymous.  Log so operators can see.
            log.warning("hubzero_cmsauth: API lookup failed (%s); "
                        "treating request as anonymous", e)
            return None

        if not name:
            return None

        # Issue a trac_auth cookie so the next request from this browser
        # skips the API call.
        self._issue_trac_auth(req, name)
        return name

    # -- trac_auth path ----------------------------------------------------- #

    def _name_from_trac_auth(self, req):
        """Read the trac_auth cookie and resolve it via Trac's local
        auth_cookie table.  Returns the bound username, or None if the
        cookie is absent / unknown / IP-mismatched."""
        cookie = req.incookie.get(_TRAC_AUTH) if req.incookie else None
        value = getattr(cookie, "value", None) if cookie else None
        if not value:
            return None
        try:
            with self.env.db_query as db:
                for name, ipnr in db(
                    "SELECT name, ipnr FROM auth_cookie WHERE cookie=%s",
                    (value,),
                ):
                    if self.check_auth_ip and ipnr != self._remote_addr(req):
                        return None
                    return name
        except Exception as e:                                          # noqa: BLE001
            log.warning("hubzero_cmsauth: auth_cookie lookup failed (%s)", e)
        return None

    def _issue_trac_auth(self, req, name):
        """INSERT a fresh row into auth_cookie and set the trac_auth cookie
        on the outgoing response.  Session-cookie lifetime (no expires/
        max-age), Secure + HttpOnly, scoped to the env path."""
        value = self._new_cookie_value()
        try:
            with self.env.db_transaction as db:
                db(
                    "INSERT INTO auth_cookie (cookie, name, ipnr, time) "
                    "VALUES (%s, %s, %s, %s)",
                    (value, name, self._remote_addr(req), int(time.time())),
                )
        except Exception as e:                                          # noqa: BLE001
            # Don't fail the request on DB write -- just log and skip
            # cookie issuance.  The user is authed for this request; the
            # next request will re-validate via the API.
            log.warning("hubzero_cmsauth: failed to insert auth_cookie (%s); "
                        "continuing without setting trac_auth", e)
            return
        req.outcookie[_TRAC_AUTH] = str(value)
        morsel = req.outcookie[_TRAC_AUTH]
        morsel[_ATTR_PATH]     = str(self.auth_cookie_path or self._env_href(req))
        morsel[_ATTR_SECURE]   = True
        morsel[_ATTR_HTTPONLY] = True

    # -- CMS API transport -------------------------------------------------- #

    def _call_api(self, req, cookie_header):
        """Open a one-shot HTTP(S) connection to the same scheme + host +
        port the browser used to reach Trac, forward the cookies to
        api_path, and return the response's `profile.username` field (or
        None for a 401/403 anonymous response).  Raises on transport or
        protocol errors so authenticate() converts them to anonymous.

        The point of deriving the API endpoint from the incoming request
        (rather than hard-coding a loopback target) is zero per-env config
        and zero drift: if the user can reach Trac, the CMS API is
        reachable at the same origin by definition."""
        env_ = getattr(req, "environ", None) or {}
        scheme = (env_.get("wsgi.url_scheme") or "https").lower()
        host_header = env_.get("HTTP_HOST") or env_.get("SERVER_NAME") or "localhost"
        # HTTP_HOST may carry an explicit port ("foo.example:8443"); split it
        # off for the connect target but keep it intact for the Host header
        # (vhost matching uses host:port verbatim).
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
            # CMS says guest/unauthenticated -- normal anonymous outcome.
            return None
        if status != 200:
            raise RuntimeError("CMS API returned HTTP %d" % status)

        try:
            payload = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as e:
            raise RuntimeError("CMS API returned non-JSON body: %s" % e)

        # The currentuser endpoint wraps profile in {"profile": {...}}.
        profile = payload.get("profile") if isinstance(payload, dict) else None
        if not isinstance(profile, dict):
            raise RuntimeError("CMS API response missing 'profile' object")
        username = profile.get("username")
        return username or None        # missing/empty -> anonymous

    # -- request helpers ---------------------------------------------------- #

    @staticmethod
    def _cookie_header(req):
        """Reassemble the raw Cookie header from Trac's request.  Trac parses
        cookies into req.incookie (a SimpleCookie); the API call wants the
        raw header as the browser sent it."""
        env = getattr(req, "environ", None) or {}
        raw = env.get("HTTP_COOKIE") or ""
        if raw:
            return raw
        try:
            return req.incookie.output(header="", sep="; ").strip()
        except AttributeError:
            return ""

    @staticmethod
    def _remote_addr(req):
        env = getattr(req, "environ", None) or {}
        return env.get("REMOTE_ADDR") or getattr(req, "remote_addr", "") or ""

    @staticmethod
    def _env_href(req):
        """Path prefix that scopes the trac_auth cookie to this env.  In
        Trac, req.href() (no args) returns the env's base path -- e.g.
        '/tools/hzforgetest'.  We append '/' so the cookie applies to all
        URLs under the env."""
        try:
            base = req.href() or "/"
        except (TypeError, AttributeError):
            base = "/"
        return base if base.endswith("/") else base + "/"


# ---------------------------------------------------------------------------- #
# Login / logout
# ---------------------------------------------------------------------------- #
# Regex over req.path_info: match exactly "/login" or "/logout" (and the
# trailing-slash variant Trac sometimes hands us), nothing else.
_LOGIN_PATH_RE  = re.compile(r"^/login/?$")
_LOGOUT_PATH_RE = re.compile(r"^/logout/?$")


class HubzeroLoginModule(Component):
    """Owns Trac's /login and /logout.  Replaces trac.web.auth.LoginModule.

    /login GET (anonymous):
        302 -> <cms_base>/login?return=<base64(<env_wiki_home>)>

    /login GET (already authenticated):
        302 -> the env's wiki home (no point re-authenticating)

    /logout GET:
        DELETE the user's auth_cookie row, clear the trac_auth cookie, then
        302 -> <cms_base>/logout?return=<base64(<env_wiki_home>)>.  HUBzero's
        com_login does the whole-origin logout (jos_session row gone, CMS
        cookies cleared) and bounces the user back to Trac, which now sees
        them as anonymous.

    The base64 return value matches HUBzero's com_login convention
    (com_login/site/controllers/auth.php lines 157-169)."""

    implements(IRequestHandler, INavigationContributor)

    cms_login_url  = Option(
        "hubzero_cmsauth", "cms_login_url",  "/login",
        "Path on the CMS host to which Trac /login redirects (with "
        "?return=<base64> appended).  Default matches HUBzero's com_login.",
    )
    cms_logout_url = Option(
        "hubzero_cmsauth", "cms_logout_url", "/logout",
        "Path on the CMS host to which Trac /logout redirects.",
    )
    cms_base_url   = Option(
        "hubzero_cmsauth", "cms_base_url",   "",
        "Absolute URL prefix for the CMS host (e.g. https://help.hubzero.org).  "
        "Empty (default) means 'derive from the incoming request' (same "
        "scheme + host as Trac).  Override only if the CMS lives at a "
        "different origin than Trac.",
    )

    # -- IRequestHandler ---------------------------------------------------- #

    def match_request(self, req):
        return bool(_LOGIN_PATH_RE.match(req.path_info)
                    or _LOGOUT_PATH_RE.match(req.path_info))

    def process_request(self, req):
        if _LOGOUT_PATH_RE.match(req.path_info):
            self._do_logout(req)            # never returns (req.redirect raises)
        if req.authname and req.authname != "anonymous":
            # Already logged in -- bounce to the wiki rather than round-trip
            # the CMS for a redundant login.
            req.redirect(self._wiki_home(req))
        self._redirect_to(req, self.cms_login_url, self._wiki_home(req))

    def _do_logout(self, req):
        """DELETE the auth_cookie row matching the trac_auth cookie, clear
        the cookie on the outgoing response, and redirect to the CMS
        logout URL (which will also clear the CMS session)."""
        cookie = req.incookie.get(_TRAC_AUTH) if req.incookie else None
        value = getattr(cookie, "value", None) if cookie else None
        if value:
            try:
                with self.env.db_transaction as db:
                    db("DELETE FROM auth_cookie WHERE cookie=%s", (value,))
            except Exception as e:                                      # noqa: BLE001
                log.warning("hubzero_cmsauth: auth_cookie DELETE failed "
                            "(%s); proceeding with logout redirect", e)
        # Clear the cookie regardless of whether the DB delete succeeded.
        req.outcookie[_TRAC_AUTH] = str("")
        morsel = req.outcookie[_TRAC_AUTH]
        morsel[_ATTR_PATH]    = str(self._env_href(req))
        morsel[_ATTR_EXPIRES] = _EXPIRES_EPOCH
        self._redirect_to(req, self.cms_logout_url, self._wiki_home(req))

    # -- INavigationContributor --------------------------------------------- #

    def get_active_navigation_item(self, req):
        return "login"

    def get_navigation_items(self, req):
        from trac.util.html import tag
        if req.authname and req.authname != "anonymous":
            yield ("metanav", "login",
                   tag.span("logged in as ", tag.b(req.authname)))
            yield ("metanav", "logout",
                   tag.a("Logout", href=req.href.logout()))
        else:
            yield ("metanav", "login",
                   tag.a("Login", href=req.href.login()))

    # -- helpers ------------------------------------------------------------ #

    def _redirect_to(self, req, cms_path, return_to):
        """302 to <cms_base_url><cms_path>?return=<base64(return_to)>."""
        target = "%s%s?return=%s" % (
            self._cms_base_url(req),
            cms_path,
            self._b64(return_to),
        )
        req.redirect(target)

    def _cms_base_url(self, req):
        """Return the CMS host prefix.  cms_base_url config wins; else
        same scheme+host as the incoming Trac request."""
        if self.cms_base_url:
            return self.cms_base_url.rstrip("/")
        env_ = getattr(req, "environ", None) or {}
        scheme = env_.get("wsgi.url_scheme", "https")
        host = env_.get("HTTP_HOST") or env_.get("SERVER_NAME", "")
        return "%s://%s" % (scheme, host)

    @staticmethod
    def _wiki_home(req):
        """Path-only URL for the env's wiki home -- where we bounce the user
        after CMS auth.  CMS expects an internal-site path (it'll prepend the
        host on its return); we strip scheme+host from req.abs_href.wiki()."""
        abs_wiki = req.abs_href.wiki()
        idx = abs_wiki.find("/", abs_wiki.find("//") + 2) if "//" in abs_wiki else -1
        return abs_wiki[idx:] if idx >= 0 else abs_wiki

    @staticmethod
    def _env_href(req):
        try:
            base = req.href() or "/"
        except (TypeError, AttributeError):
            base = "/"
        return base if base.endswith("/") else base + "/"

    @staticmethod
    def _b64(s):
        return base64.b64encode(s.encode("utf-8")).decode("ascii")
