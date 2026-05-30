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
import time

try:                                      # Py3
    from http.client import HTTPSConnection, HTTPConnection
except ImportError:                       # Py2
    from httplib import HTTPSConnection, HTTPConnection   # noqa: F401

from trac.config import BoolOption, IntOption, Option
from trac.web.auth import LoginModule


log = logging.getLogger(__name__)


# Usernames that Trac reserves for its own permission machinery; the CMS
# API must NEVER cause us to set REMOTE_USER to one of these.  If it did
# -- e.g. a corrupt jos_users row, a buggy API change, or a malicious
# account whose profile.username is literally "anonymous" -- we'd grant
# the bearer the permissions of the Trac anonymous/authenticated group
# itself.  Compared case-insensitively against the stripped CMS value.
_RESERVED_USERNAMES = frozenset(["anonymous", "authenticated"])


# Name of the sibling cookie we set to track when the last CMS recheck
# happened for this trac_auth session.  Not signed -- a user who tampers
# with the value can at most defer their own recheck (which keeps them
# in the SAME pre-recheck state as today's default code path), so the
# only escalation is "back to back-compat behavior".  Set str() for
# Py2 unicode_literals compat (Cookie.SimpleCookie barfs on unicode keys).
_RECHECK_COOKIE = str("hubzero_cms_check")


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
    recheck_interval_seconds = IntOption(
        "hubzero_cmsauth", "recheck_interval_seconds", 0,
        "Seconds between forced re-checks of the CMS session for a given "
        "trac_auth-bearing browser.  0 (default) = never re-check; trust "
        "the trac_auth cookie until Trac's own auth_cookie_lifetime "
        "expires it (7 days by default).  Positive values close review "
        "item #4: a user renamed or deactivated in the CMS keeps using "
        "Trac as their old self until the cookie expires.  Recommended "
        "starting point: 300 (5 minutes) -- one extra CMS API call per "
        "5-minute window per active browser, sub-millisecond loopback "
        "responses, propagates rename/deactivation in <= 5 min.",
    )
    verify_tls = BoolOption(
        "hubzero_cmsauth", "verify_tls", True,
        "Verify the CMS API's TLS certificate.  Default true (validate "
        "against the system trust store + RFC 2818 hostname check, the "
        "stdlib `ssl.create_default_context` default).  Set false ONLY "
        "for hosts whose local CMS cert can't be validated -- e.g. a "
        "self-signed dev hub.  Default-on closes a MITM hole the 1.0.0 "
        "code path left open by hardcoding `CERT_NONE`.",
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
                auth_cookie table (the standard Trac fast path).
                Optionally, after `recheck_interval_seconds` have passed
                since the last CMS check for this browser, force a
                re-check against the CMS API and invalidate the trac_auth
                row if the user has been renamed/deactivated upstream.
            (3) neither -> try the CMS API.  If it returns a username,
                set REMOTE_USER in the environ and let parent take the
                'standard REMOTE_USER' path.
        """
        has_remote_user = bool(req.environ.get("REMOTE_USER"))
        has_trac_auth = "trac_auth" in req.incookie
        if not has_remote_user and not has_trac_auth:
            # Cold cache: path (3) -- resolve via CMS API.
            name = self._authenticate_via_cms(req)
            if name:
                req.environ["REMOTE_USER"] = name
                self._stamp_recheck(req)
        elif has_trac_auth and not has_remote_user:
            # Warm cache: path (2).  Optionally re-validate against CMS
            # if the recheck window has expired.
            if (self.recheck_interval_seconds > 0
                    and self._should_recheck(req)):
                self._do_periodic_recheck(req)
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
        # Stamp the recheck cookie so the next request hits the trac_auth
        # fast path and (with recheck disabled or window not yet expired)
        # doesn't re-call the CMS API needlessly.
        self._stamp_recheck(req)

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
            return   # defensive: _redirect_to_cms raises RequestDone, but
                     # don't fall through to the /login referer logic if a
                     # future Trac ever makes req.redirect() return.
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
        # Split host:port for the connect target, keeping host_header intact
        # for the forwarded Host header.
        host, port = self._split_host_port(host_header, scheme)
        if scheme == "https":
            ctx = ssl.create_default_context()
            if not self.verify_tls:
                # Operator opt-out -- only legitimate when the CMS cert
                # can't be validated by the system trust store (self-signed
                # dev hub).  Default-on (verify_tls = True in trac.ini)
                # keeps the cert chain + hostname check intact.
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
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
        if not username:
            return None
        # Strip whitespace; reject reserved Trac usernames (anonymous /
        # authenticated) regardless of case.  Strip BEFORE the reserved
        # check so " Anonymous " doesn't slip through.  Returning the
        # stripped form for non-reserved names keeps Trac's downstream
        # username comparisons (auth_cookie lookup, permission_groups)
        # well-defined -- they're whitespace-sensitive.
        stripped = username.strip()
        if not stripped or stripped.lower() in _RESERVED_USERNAMES:
            log.warning("hubzero_cmsauth: CMS API returned reserved/empty "
                        "username %r; treating as anonymous", username)
            return None
        return stripped

    # -- helpers ------------------------------------------------------------ #

    @staticmethod
    def _cookie_header(req):
        """Raw Cookie header as the browser sent it, with the `trac_auth`
        morsel filtered out (parent's req.incookie is parsed; the API call
        wants the string form).

        Why strip trac_auth: it's Trac's own session cookie, set by
        `LoginModule._do_login` after a successful auth.  The CMS doesn't
        recognize it and just discards it -- so forwarding it leaks no
        secret per se, but exposes the value to any logging / proxy /
        request-id capture on the CMS path that has nothing to do with
        Trac.  Defense-in-depth: don't send Trac internals to a service
        that has no need for them."""
        env = getattr(req, "environ", None) or {}
        raw = env.get("HTTP_COOKIE") or ""
        if not raw:
            try:
                raw = req.incookie.output(header="", sep="; ").strip()
            except AttributeError:
                return ""
        return HubzeroSSOLoginModule._strip_cookie(raw, "trac_auth")

    @staticmethod
    def _strip_cookie(raw, name):
        """Return `raw` with the cookie morsel named `name` removed.
        Cookie-name comparison is case-sensitive (RFC 6265 cookie names
        are case-sensitive; Trac sets `trac_auth` in exactly that form)."""
        prefix = name + "="
        morsels = [m.strip() for m in raw.split(";")]
        morsels = [m for m in morsels if m and not m.startswith(prefix)]
        return "; ".join(morsels)

    def _redirect_to_cms(self, req, cms_path, return_to):
        """302 to `<cms_scheme>://<cms_host><cms_path>?return=<base64(return_to)>`.
        CMS scheme + host come from the incoming request -- same origin."""
        env_ = getattr(req, "environ", None) or {}
        scheme = env_.get("wsgi.url_scheme", "https")
        host = env_.get("HTTP_HOST") or env_.get("SERVER_NAME", "")
        target = "%s://%s%s?return=%s" % (
            scheme, host, cms_path, self._b64(return_to))
        req.redirect(target)

    # -- periodic CMS re-check (review #4) --------------------------------- #

    def _should_recheck(self, req):
        """True if the recheck window has expired since the last CMS check
        for this browser.  Reads the `hubzero_cms_check` cookie; absent or
        unparseable -> True (force a check on first request after a deploy
        with recheck enabled, or after the user clears their cookies).
        Always False when `recheck_interval_seconds <= 0` (the knob is off)."""
        if self.recheck_interval_seconds <= 0:
            return False
        last = self._last_check_timestamp(req)
        if last is None:
            return True
        return (time.time() - last) >= self.recheck_interval_seconds

    @staticmethod
    def _last_check_timestamp(req):
        """Read the `hubzero_cms_check` cookie value (a unix timestamp set
        by `_stamp_recheck`) and return it as a float.  Returns None when
        the cookie is missing or the value isn't a parseable number."""
        morsel = req.incookie.get(_RECHECK_COOKIE) if req.incookie else None
        if morsel is None:
            return None
        try:
            return float(morsel.value)
        except (TypeError, ValueError):
            return None

    def _stamp_recheck(self, req):
        """Set the `hubzero_cms_check` outcookie to the current unix
        timestamp.  Path matches the env's base href so the browser only
        sends this cookie back to /tools/<env>/* (same scope as trac_auth)."""
        morsel = req.outcookie[_RECHECK_COOKIE] = str(int(time.time()))
        req.outcookie[_RECHECK_COOKIE]["path"] = self._cookie_path(req)
        req.outcookie[_RECHECK_COOKIE]["httponly"] = True
        env_ = getattr(req, "environ", None) or {}
        if (env_.get("wsgi.url_scheme") or "https") == "https":
            req.outcookie[_RECHECK_COOKIE]["secure"] = True

    def _do_periodic_recheck(self, req):
        """Compare the CMS-side username to the Trac-side username (the
        one resolved from `trac_auth` -> auth_cookie.name).  Three cases:

          (a) CMS says the user is still authenticated AS THE SAME NAME ->
              stamp a fresh recheck timestamp; no behavior change.

          (b) CMS says the user has been RENAMED (different username
              returned from the API) -> invalidate Trac's auth_cookie row
              + clear the trac_auth cookie so this request and the next
              behave as if they were anonymous.  User then re-logs in
              under the new name (typically one click since the CMS
              session is still valid).

          (c) CMS says the user is NO LONGER AUTHENTICATED (401/403, or
              network error explicitly indistinguishable from "session
              expired") -> same invalidate.  This is the deactivation
              case: a user fired/banned in the CMS stops being authed in
              Trac within `recheck_interval_seconds`.

        Fail-safe on transient API errors: if `_authenticate_via_cms`
        returns None due to a network blip (not because the session is
        actually gone), we'd invalidate the trac_auth.  The user gets a
        single forced re-login on the network blip; they CAN re-login
        because the CMS session itself is still valid.  Trade-off: a
        flaky network briefly degrades UX rather than allowing stale
        auth.  Acceptable because the alternative ("trust the cookie
        even when CMS is silent") is what review #4 exists to fix."""
        # Resolve the Trac-side name FIRST.  If the local auth_cookie lookup
        # errors (a transient SQLite blip, NOT a CMS signal), skip this
        # recheck entirely: don't invalidate, and don't re-stamp the recheck
        # cookie -- so the next request retries.  Logging a user out because
        # the LOCAL session table hiccuped would be a self-inflicted outage.
        try:
            trac_name = self._authname_from_trac_auth(req)
        except Exception as e:                                # noqa: BLE001
            log.warning("hubzero_cmsauth: auth_cookie lookup failed during "
                        "recheck (%s); leaving session as-is, will retry", e)
            return
        new_name = self._authenticate_via_cms(req)
        if new_name is None or new_name != trac_name:
            log.info("hubzero_cmsauth: invalidating trac_auth (was %r, "
                     "CMS now says %r); user will be forced through /login",
                     trac_name, new_name)
            self._invalidate_trac_auth(req)
            return
        self._stamp_recheck(req)

    def _authname_from_trac_auth(self, req):
        """Look up the name associated with the `trac_auth` cookie in Trac's
        own auth_cookie table.  Returns the name, or None if there's no
        matching row (genuinely-unknown cookie).

        RAISES on a DB error -- the caller (`_do_periodic_recheck`) must be
        able to distinguish "no such row" (a real reason to invalidate the
        session) from "the local auth_cookie query failed" (a transient
        SQLite blip unrelated to the CMS -- must NOT log the user out)."""
        try:
            cookie_value = req.incookie["trac_auth"].value
        except (KeyError, AttributeError):
            return None
        for row in self.env.db_query(
                "SELECT name FROM auth_cookie WHERE cookie=%s",
                (cookie_value,)):
            return row[0]
        return None

    def _invalidate_trac_auth(self, req):
        """DELETE the auth_cookie row for the current trac_auth value,
        expire the trac_auth cookie on the wire, and remove it from
        req.incookie so the parent class's authenticate() sees no
        cookie and returns None (anonymous) for the remainder of this
        request.  Also expires the hubzero_cms_check sibling cookie."""
        try:
            cookie_value = req.incookie["trac_auth"].value
        except (KeyError, AttributeError):
            cookie_value = None
        if cookie_value:
            try:
                self.env.db_transaction(
                    "DELETE FROM auth_cookie WHERE cookie=%s",
                    (cookie_value,))
            except Exception as e:                            # noqa: BLE001
                log.warning("hubzero_cmsauth: auth_cookie DELETE failed "
                            "(%s); cookie still expired on the wire", e)
        # Remove from incookie so the parent's authenticate sees no cookie
        # for the rest of this request.
        try:
            del req.incookie["trac_auth"]
        except (KeyError, TypeError):
            pass
        self._expire_cookie(req)
        # Expire our sibling cookie too (Max-Age=0), with the SAME attributes
        # _stamp_recheck set (path + HttpOnly + Secure on https) -- some
        # browsers only honor a delete cookie whose attributes match the
        # original, and it keeps the expiring Set-Cookie consistent.
        req.outcookie[_RECHECK_COOKIE] = ""
        req.outcookie[_RECHECK_COOKIE]["path"] = self._cookie_path(req)
        req.outcookie[_RECHECK_COOKIE]["max-age"] = 0
        req.outcookie[_RECHECK_COOKIE]["httponly"] = True
        env_ = getattr(req, "environ", None) or {}
        if (env_.get("wsgi.url_scheme") or "https") == "https":
            req.outcookie[_RECHECK_COOKIE]["secure"] = True

    @staticmethod
    def _cookie_path(req):
        """Path attribute for the recheck cookie -- matches the env's base
        href (e.g. `/tools/hzforgetest`) so the browser only sends this
        cookie back to the same env's URLs."""
        try:
            return req.href()
        except (AttributeError, TypeError):
            return "/"

    # -- _redirect_back (review #2 open-redirect guard) -------------------- #

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
        scheme) / backslash- or control-char-obfuscated authority
        (`/\\evil.com`, `/\\thttp://evil.com`) / different-scheme-or-host
        absolute URLs."""
        if not target:
            return False
        # Browsers strip ASCII control chars (TAB/CR/LF/NUL and the rest of
        # C0, plus DEL) from a URL before resolving it, so a value like
        # "/\thttp://evil.com" or "/\nhttp://evil.com" can resolve OFF-origin
        # after the browser strips the control char.  Reject any control
        # char outright -- none belong in a legitimate same-origin referer.
        if any(ord(c) < 0x20 or ord(c) == 0x7f for c in target):
            return False
        # Browsers treat backslash as equivalent to "/" in the authority, so
        # "/\evil.com" resolves to "//evil.com" -> protocol-relative ->
        # evil.com.  A backslash never appears in a legitimate same-origin
        # path or URL referer; reject outright.  (This is the bypass the
        # 1.0.1 "//" check missed -- see test_redirect_back_rejects_*.)
        if "\\" in target:
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
