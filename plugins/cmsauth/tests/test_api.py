"""Test suite for hubzero-trac-cmsauth (canonical-inheritance refactor).

We test the parts WE wrote:
  * authenticate() decides when to call the CMS API and when to defer
    to the parent class
  * _do_login() bounces to CMS when no REMOTE_USER and delegates to
    parent when there is one
  * _do_logout() bypasses parent's POST-only/anonymous gates while still
    delegating to _expire_cookie (and doing the DB DELETE)
  * _redirect_back() goes to CMS for /logout, env wiki for /login
  * _call_api()'s HTTP transport behaviors (status codes, JSON shape,
    network errors)
  * Helpers: _cookie_header(), _b64(), _redirect_to_cms()

We deliberately do NOT test the parent class's auth_cookie INSERT/DELETE,
trac_auth cookie attribute setting, or _expire_cookie's exact morsel
mutations -- those belong to trac.web.auth.LoginModule and are covered
by the canary integration tests on the help host.  The conftest stubs
the parent to record which super-method was called so our delegation
contract is testable.
"""
from __future__ import absolute_import, division, print_function, unicode_literals

import base64
import socket

import pytest

from hubzero_cmsauth.api import HubzeroSSOLoginModule


def _b64decode(s):
    """Py2/Py3-safe wrapper around base64.urlsafe_b64decode.  Py2's
    `urlsafe_b64decode` calls `s.translate(table)` with a Py2 `str`
    translation table, which raises TypeError if `s` is unicode (and
    every string literal in this file is unicode under
    `from __future__ import unicode_literals`).  Coerce to ASCII bytes
    first."""
    return base64.urlsafe_b64decode(s.encode("ascii")
                                    if isinstance(s, type(u"")) else s)


# ============================================================================ #
# authenticate()
# ============================================================================ #

def test_authenticate_calls_cms_api_when_no_remote_user_and_no_trac_auth(
        sso, make_req, http_stub):
    """Cold cache: no REMOTE_USER, no trac_auth cookie, BUT a CMS Cookie
    header is present -> call CMS API, set REMOTE_USER, defer to parent."""
    req = make_req(cookie="jos_session=sid")
    assert sso.authenticate(req) == "jdoe"
    # Parent.authenticate WAS called (after we set REMOTE_USER for it)
    assert req._super_calls == ["authenticate"]
    # ... and the API call happened with the forwarded cookie
    assert http_stub.last_conn is not None
    assert http_stub.last_conn.calls[0]["headers"]["Cookie"] == "jos_session=sid"
    # The CMS-resolved name landed in REMOTE_USER so parent could pick it up
    assert req.environ["REMOTE_USER"] == "jdoe"


def test_authenticate_skips_api_when_remote_user_already_set(
        sso, make_req, http_stub):
    """Apache LDAP (or any upstream) already set REMOTE_USER -> trust it,
    don't burn an API call."""
    req = make_req(remote_user="bob", cookie="jos_session=sid")
    assert sso.authenticate(req) == "bob"
    # No API hit
    assert http_stub.last_conn is None
    # Parent ran (returned REMOTE_USER as-is per its standard logic)
    assert req._super_calls == ["authenticate"]


def test_authenticate_skips_api_when_trac_auth_cookie_present(
        sso, make_req, http_stub):
    """trac_auth cookie present -> let parent do its auth_cookie table
    lookup; don't pre-empt with the API."""
    class _Morsel(object):
        value = "abc123"
    req = make_req(cookie="trac_auth=abc123")
    req.incookie["trac_auth"] = _Morsel()
    sso.authenticate(req)
    # No API call
    assert http_stub.last_conn is None
    # Parent ran (will do auth_cookie lookup -- not our concern)
    assert req._super_calls == ["authenticate"]


def test_authenticate_returns_none_when_no_cookies_at_all(
        sso, make_req, http_stub):
    req = make_req()    # no cookie
    result = sso.authenticate(req)
    assert result is None
    # No API call (no Cookie header to forward)
    assert http_stub.last_conn is None
    # Parent did run (and returned None since no REMOTE_USER and no cookie)
    assert req._super_calls == ["authenticate"]


def test_authenticate_treats_403_as_anonymous(sso, make_req, http_stub):
    http_stub.stage_response(403, b'{"message":"Access Denied","code":403}')
    req = make_req(cookie="jos_session=expired")
    result = sso.authenticate(req)
    assert result is None
    assert "REMOTE_USER" not in req.environ


def test_authenticate_treats_500_as_anonymous_fail_safe(sso, make_req, http_stub):
    """API outage -> NEVER grant access.  Falls back to anonymous."""
    http_stub.stage_response(500, b'<html>Server Error</html>')
    req = make_req(cookie="jos_session=sid")
    assert sso.authenticate(req) is None
    assert "REMOTE_USER" not in req.environ


def test_authenticate_treats_network_error_as_anonymous(sso, make_req, http_stub):
    http_stub.stage_exception(socket.timeout("API hung"))
    req = make_req(cookie="jos_session=sid")
    assert sso.authenticate(req) is None


def test_authenticate_treats_malformed_json_as_anonymous(sso, make_req, http_stub):
    http_stub.stage_response(200, b'<html>not json</html>')
    req = make_req(cookie="jos_session=sid")
    assert sso.authenticate(req) is None


def test_authenticate_treats_missing_username_as_anonymous(sso, make_req, http_stub):
    http_stub.stage_response(200, b'{"profile":{"id":1,"name":"Jane"}}')
    req = make_req(cookie="jos_session=sid")
    assert sso.authenticate(req) is None


# ---- reserved-username guard (must NEVER impersonate anonymous/authenticated) ---- #

@pytest.mark.parametrize("payload_username", [
    "anonymous",       # exact match -- would grant Trac's anonymous-group perms
    "ANONYMOUS",       # case variant -- Trac's reserved name is case-insensitive
    "Anonymous",
    "authenticated",   # the OTHER reserved name -- "all logged-in users" group
    "Authenticated",
    " anonymous ",     # whitespace-wrapped -- strip before the reserved check
    "\tanonymous\n",   # ditto, with weirder whitespace
])
def test_authenticate_rejects_reserved_username(
        sso, make_req, http_stub, payload_username):
    """If the CMS API ever returns a username equal to one of Trac's
    reserved names ("anonymous", "authenticated"), we must NOT set
    REMOTE_USER to it -- doing so would impersonate the bearer as
    Trac's anonymous user or as a member of the special "authenticated"
    group.  The plugin treats that response as anonymous instead and
    logs a warning.  Compared case-insensitively and after stripping
    whitespace so " Anonymous " is also blocked."""
    import json
    body = json.dumps({"profile": {"id": 1, "username": payload_username}}).encode("utf-8")
    http_stub.stage_response(200, body)
    req = make_req(cookie="jos_session=sid")
    assert sso.authenticate(req) is None
    # Did NOT leak the reserved name into REMOTE_USER:
    assert "REMOTE_USER" not in req.environ


def test_authenticate_strips_whitespace_from_real_username(
        sso, make_req, http_stub):
    """Defensive: if the CMS API ever returns " jdoe " (whitespace-wrapped),
    return the stripped form so Trac's downstream comparisons (auth_cookie
    rows keyed by name, IPermissionStore lookups by username) stay
    well-defined.  Whitespace-sensitive equality would otherwise leave
    the user permanently mis-identified."""
    http_stub.stage_response(200, b'{"profile":{"id":1,"username":"  jdoe  "}}')
    req = make_req(cookie="jos_session=sid")
    assert sso.authenticate(req) == "jdoe"
    assert req.environ["REMOTE_USER"] == "jdoe"


def test_authenticate_treats_whitespace_only_username_as_anonymous(
        sso, make_req, http_stub):
    """A username that's only whitespace must NOT slip through -- after
    strip() it becomes empty and Trac's authname rules treat the empty
    string as anonymous, which would re-introduce the impersonation we
    just closed above."""
    http_stub.stage_response(200, b'{"profile":{"id":1,"username":"   "}}')
    req = make_req(cookie="jos_session=sid")
    assert sso.authenticate(req) is None
    assert "REMOTE_USER" not in req.environ


# ============================================================================ #
# _call_api()
# ============================================================================ #

def test_call_api_forwards_host_header_from_request(sso, make_req, http_stub):
    """The connect target host/port AND the forwarded Host header all
    match the incoming request's scheme + host (so Apache vhost matching
    serves the API call from the same vhost the user is talking to)."""
    req = make_req(cookie="jos_session=sid")
    req.environ["HTTP_HOST"] = "help.hubzero.org"
    sso.authenticate(req)
    conn = http_stub.last_conn
    assert conn.host == "help.hubzero.org"
    assert conn.port == 443
    assert conn.calls[0]["headers"]["Host"] == "help.hubzero.org"
    assert conn.calls[0]["url"] == "/api/v1.1/members/currentuser"


def test_call_api_uses_http_when_scheme_is_http(sso, make_req, http_stub):
    """Wsgi.url_scheme=http -> port 80, HTTPConnection (not HTTPS)."""
    req = make_req(cookie="jos_session=sid")
    req.environ["wsgi.url_scheme"] = "http"
    req.environ["HTTP_HOST"] = "internal.lab"
    sso.authenticate(req)
    assert http_stub.last_conn.port == 80


def test_call_api_verifies_tls_by_default(sso, make_req, http_stub):
    """Default (`verify_tls = True`): the HTTPSConnection gets the stdlib
    default context, which validates the cert chain and checks the
    hostname.  Closes the MITM hole the 1.0.0 path left open by hardcoding
    CERT_NONE."""
    import ssl
    req = make_req(cookie="jos_session=sid")
    sso.authenticate(req)
    ctx = http_stub.last_conn.kw["context"]
    assert ctx.verify_mode  == ssl.CERT_REQUIRED    # stdlib default
    assert ctx.check_hostname is True               # stdlib default


def test_call_api_skips_tls_verify_when_opted_out(sso, make_req, http_stub):
    """`verify_tls = false` in trac.ini (operator opt-out for hosts whose
    CMS cert can't be validated -- self-signed dev hubs).  Falls back to
    1.0.0 behavior: CERT_NONE + hostname check off."""
    import ssl
    sso.verify_tls = False                          # simulate trac.ini override
    req = make_req(cookie="jos_session=sid")
    sso.authenticate(req)
    ctx = http_stub.last_conn.kw["context"]
    assert ctx.verify_mode  == ssl.CERT_NONE
    assert ctx.check_hostname is False


def test_call_api_honors_explicit_port_in_host_header(sso, make_req, http_stub):
    """HTTP_HOST may carry 'host:port'; we split it for the connect
    target but keep the verbatim string in the Host header (which is
    what Apache's vhost matching expects)."""
    req = make_req(cookie="jos_session=sid")
    req.environ["HTTP_HOST"] = "help.hubzero.org:8443"
    sso.authenticate(req)
    conn = http_stub.last_conn
    assert conn.host == "help.hubzero.org"
    assert conn.port == 8443
    assert conn.calls[0]["headers"]["Host"] == "help.hubzero.org:8443"


# ============================================================================ #
# _do_login()
# ============================================================================ #

def test_do_login_redirects_to_cms_when_no_remote_user(
        sso, make_req, http_stub, RedirectDone):
    """No REMOTE_USER, no CMS session cookie -> bounce to CMS /login.
    Return URL points BACK to our /login so the post-CMS-auth round-trip
    cycles through us again (then REMOTE_USER will be set and parent
    runs)."""
    http_stub.stage_response(403, b'{}')           # CMS returns guest
    req = make_req(path_info="/login", cookie="jos_session=expired")
    with pytest.raises(RedirectDone) as excinfo:
        sso._do_login(req)
    target = excinfo.value.url
    assert target.startswith("https://help.hubzero.org/login?return=")
    decoded = _b64decode(target.split("return=", 1)[1]).decode("utf-8")
    assert decoded == "/tools/hzforgetest/login"   # come back to OUR /login


def test_do_login_delegates_to_parent_when_remote_user_set(sso, make_req):
    """REMOTE_USER set (Apache LDAP, or from a prior authenticate() call) ->
    let parent do its INSERT auth_cookie + set trac_auth cookie."""
    req = make_req(path_info="/login", remote_user="alice")
    sso._do_login(req)
    assert req._super_calls == ["_do_login"]


def test_do_login_recovers_via_cms_api_on_post_redirect_landing(
        sso, make_req, http_stub):
    """User landed back at /login after CMS auth carrying the CMS session
    cookie -> our _do_login should resolve via CMS API, set REMOTE_USER,
    and call parent.  No bounce to CMS this time."""
    # default http_stub response: 200 + profile with username=jdoe
    req = make_req(path_info="/login", cookie="jos_session=valid")
    sso._do_login(req)
    # Parent took over -- meaning REMOTE_USER got set + we delegated
    assert req.environ["REMOTE_USER"] == "jdoe"
    assert req._super_calls == ["_do_login"]


# ============================================================================ #
# _do_logout()
# ============================================================================ #

def test_do_logout_calls_expire_cookie_via_parent(sso, make_req):
    """We bypass parent's POST-only gate but still delegate to _expire_cookie
    (which sets the trac_auth cookie's expires-in-past).  No redirect happens
    in _do_logout itself -- that's _redirect_back's job."""
    class _Morsel(object):
        value = "abc"
    req = make_req(path_info="/logout", authname="jdoe", cookie="trac_auth=abc")
    req.incookie["trac_auth"] = _Morsel()
    # We can't easily verify the env.db_transaction DELETE without a real
    # env, but we CAN verify _expire_cookie was called (records via stub).
    # NB this currently fails fast because our stub env doesn't have
    # db_transaction; skip the DB-touching path by clearing incookie.
    req.incookie = {}
    sso._do_logout(req)
    assert "_expire_cookie" in req._super_calls


def test_do_logout_does_not_redirect_directly(sso, make_req):
    """_do_logout itself must not redirect -- process_request's contract
    is to then call _redirect_back."""
    req = make_req(path_info="/logout")
    # No incookie['trac_auth'] -> skip the DB path entirely
    sso._do_logout(req)
    assert req.redirected_to is None


# ============================================================================ #
# _redirect_back()
# ============================================================================ #

def test_redirect_back_for_logout_goes_to_cms_logout(
        sso, make_req, RedirectDone):
    """After /logout has run, _redirect_back sends to CMS /logout with
    return=<wiki home> so the user lands back at the env wiki anonymous."""
    req = make_req(path_info="/logout", authname="anonymous")
    with pytest.raises(RedirectDone) as excinfo:
        sso._redirect_back(req)
    target = excinfo.value.url
    assert target.startswith("https://help.hubzero.org/logout?return=")
    decoded = _b64decode(target.split("return=", 1)[1]).decode("utf-8")
    assert decoded == "/tools/hzforgetest/wiki"


def test_redirect_back_for_login_goes_to_env_wiki(
        sso, make_req, RedirectDone):
    """After /login has run successfully, _redirect_back sends to the env
    wiki home (or the explicit `referer` arg if present)."""
    req = make_req(path_info="/login", authname="jdoe")
    with pytest.raises(RedirectDone) as excinfo:
        sso._redirect_back(req)
    assert excinfo.value.url == "/tools/hzforgetest/wiki"


def test_redirect_back_for_login_honors_same_origin_path_referer(
        sso, make_req, RedirectDone):
    """A host-relative ?referer= path (the common case from Trac chrome)
    overrides the env wiki default.  Matches stock LoginModule's
    behavior for same-site referers."""
    req = make_req(path_info="/login", authname="jdoe",
                   args={"referer": "/tools/hzforgetest/ticket/42"})
    with pytest.raises(RedirectDone) as excinfo:
        sso._redirect_back(req)
    assert excinfo.value.url == "/tools/hzforgetest/ticket/42"


def test_redirect_back_for_login_honors_same_origin_absolute_referer(
        sso, make_req, RedirectDone):
    """A fully-qualified ?referer= URL that points back at us is also
    honored -- some chrome serializations emit the absolute form."""
    req = make_req(path_info="/login", authname="jdoe",
                   args={"referer":
                         "https://help.hubzero.org/tools/hzforgetest/ticket/42"})
    with pytest.raises(RedirectDone) as excinfo:
        sso._redirect_back(req)
    assert excinfo.value.url == \
        "https://help.hubzero.org/tools/hzforgetest/ticket/42"


# ---- open-redirect guard (the parts that matter for security) ---- #

def test_redirect_back_rejects_cross_origin_referer(
        sso, make_req, RedirectDone):
    """Open-redirect guard: a cross-origin ?referer= must be ignored,
    not blindly redirected to.  Without this check, a logged-in user
    clicking /login?referer=https://evil.com/phish lands on evil.com --
    an authenticated open redirect, perfect phishing primitive.  Stock
    Trac's LoginModule rejects this; our override now does too."""
    req = make_req(path_info="/login", authname="jdoe",
                   args={"referer": "https://evil.com/phish"})
    with pytest.raises(RedirectDone) as excinfo:
        sso._redirect_back(req)
    # Falls back to env wiki -- NOT to evil.com
    assert excinfo.value.url == "/tools/hzforgetest/wiki"


def test_redirect_back_rejects_protocol_relative_referer(
        sso, make_req, RedirectDone):
    """`//evil.com/x` is a protocol-relative URL: the browser resolves
    it under the current scheme but at evil.com.  Must be rejected as
    cross-origin."""
    req = make_req(path_info="/login", authname="jdoe",
                   args={"referer": "//evil.com/phish"})
    with pytest.raises(RedirectDone) as excinfo:
        sso._redirect_back(req)
    assert excinfo.value.url == "/tools/hzforgetest/wiki"


def test_redirect_back_rejects_lookalike_host_referer(
        sso, make_req, RedirectDone):
    """A host that merely starts with our hostname but isn't our host --
    `help.hubzero.org.evil.com` -- must NOT match the same-origin prefix
    check.  Tests that the prefix comparison requires the next char to
    be `/` (path separator) rather than allowing arbitrary continuation."""
    req = make_req(path_info="/login", authname="jdoe",
                   args={"referer":
                         "https://help.hubzero.org.evil.com/phish"})
    with pytest.raises(RedirectDone) as excinfo:
        sso._redirect_back(req)
    assert excinfo.value.url == "/tools/hzforgetest/wiki"


def test_redirect_back_rejects_scheme_mismatch_referer(
        sso, make_req, RedirectDone):
    """`http://` referer when we serve `https://` is not same-origin --
    the browser treats them as different origins.  Must be rejected."""
    req = make_req(path_info="/login", authname="jdoe",
                   args={"referer":
                         "http://help.hubzero.org/tools/hzforgetest/wiki"})
    with pytest.raises(RedirectDone) as excinfo:
        sso._redirect_back(req)
    assert excinfo.value.url == "/tools/hzforgetest/wiki"


def test_is_same_origin_helper_unit(sso, make_req):
    """Direct unit test of the same-origin classifier -- covers the
    branches the _redirect_back tests already exercise, plus a few
    inputs that aren't worth a full RedirectDone round-trip."""
    req = make_req(path_info="/login")
    # Truthy positive cases
    assert sso._is_same_origin(req, "/tools/hzforgetest/wiki")
    assert sso._is_same_origin(req, "https://help.hubzero.org")          # exact prefix
    assert sso._is_same_origin(req, "https://help.hubzero.org/x/y")      # under prefix
    # Falsy negative cases
    assert not sso._is_same_origin(req, None)
    assert not sso._is_same_origin(req, "")
    assert not sso._is_same_origin(req, "//evil.com/x")                  # protocol-relative
    assert not sso._is_same_origin(req, "https://evil.com/")             # cross-origin
    assert not sso._is_same_origin(req, "http://help.hubzero.org/x")     # scheme mismatch
    assert not sso._is_same_origin(req, "https://help.hubzero.org.evil.com/x")


def test_redirect_back_for_logout_carries_correct_env_in_return_url(
        sso, make_req, RedirectDone):
    """Different env -> different return URL -- no hardcoded hzforgetest."""
    req = make_req(path_info="/logout",
                   abs_base="https://help.hubzero.org/tools/bio3d")
    with pytest.raises(RedirectDone) as excinfo:
        sso._redirect_back(req)
    decoded = _b64decode(
        excinfo.value.url.split("return=", 1)[1]).decode("utf-8")
    assert decoded == "/tools/bio3d/wiki"


# ============================================================================ #
# Helpers
# ============================================================================ #

def test_cookie_header_strips_trac_auth_morsel(sso, make_req):
    """_cookie_header MUST drop the `trac_auth=...` morsel before forwarding
    to the CMS API.  Defense-in-depth: the CMS doesn't recognize Trac's
    own session cookie and just discards it, but exposing the value to any
    logging / proxy / request-id capture on the CMS path is unhygienic."""
    req = make_req(cookie="a=1; b=2; trac_auth=xyz")
    assert sso._cookie_header(req) == "a=1; b=2"


def test_cookie_header_strips_trac_auth_when_only_morsel(sso, make_req):
    """If trac_auth is the ONLY cookie, return empty string -- no API call
    will be made downstream (the empty header short-circuits the call)."""
    req = make_req(cookie="trac_auth=xyz")
    assert sso._cookie_header(req) == ""


def test_cookie_header_strips_trac_auth_at_start_or_end(sso, make_req):
    """Position-agnostic: trac_auth at start, middle, or end is all
    handled the same way."""
    assert sso._cookie_header(make_req(cookie="trac_auth=x; jos=y")) == "jos=y"
    assert sso._cookie_header(make_req(cookie="jos=y; trac_auth=x")) == "jos=y"
    assert sso._cookie_header(make_req(cookie="a=1; trac_auth=x; b=2")) == "a=1; b=2"


def test_cookie_header_preserves_lookalike_cookie_names(sso, make_req):
    """Cookies whose names merely START with `trac_auth` (e.g. `trac_auth2`
    or `trac_authority`) are NOT stripped -- our match is on the exact name
    followed by `=`.  Same for case variants (RFC 6265 cookie names are
    case-sensitive; Trac sets exactly `trac_auth`)."""
    assert "trac_auth2=keep" in sso._cookie_header(
        make_req(cookie="trac_auth2=keep; trac_auth=drop"))
    assert "Trac_Auth=keep" in sso._cookie_header(
        make_req(cookie="Trac_Auth=keep; trac_auth=drop"))


def test_cookie_header_empty_when_no_environ(sso, make_req):
    req = make_req()    # no cookie
    assert sso._cookie_header(req) == ""


def test_b64_round_trip(sso):
    encoded = sso._b64("/tools/hzforgetest/wiki")
    assert _b64decode(encoded).decode("utf-8") == "/tools/hzforgetest/wiki"


def test_b64_uses_urlsafe_alphabet(sso):
    """The standard base64 alphabet contains `+` and `/`; both turn into
    garbage when embedded in a `?return=` query value (`+` becomes space
    when the CMS parses the query string).  Switching to RFC 4648 Section 5
    URL-safe base64 (which uses `-` and `_` in their place) closes that
    class.

    `"???"` is a known case: standard base64 -> `"Pz8/"` (trailing slash);
    urlsafe -> `"Pz8_"` (trailing underscore).  The assertion is meaningful
    BECAUSE the two alphabets disagree on this input."""
    encoded = sso._b64("???")
    assert encoded == "Pz8_"               # urlsafe-alphabet output
    assert "/" not in encoded              # the security property
    assert "+" not in encoded


# ============================================================================ #
# Periodic CMS re-check (review #4)
#
# The trac_auth fast path doesn't re-validate the user against the CMS, so a
# rename/deactivation in the CMS isn't visible to Trac until the cookie
# expires (default 7 days).  recheck_interval_seconds > 0 forces a re-check
# every N seconds on warm-cache requests; mismatch invalidates trac_auth.
# ============================================================================ #

import time as _time

# --- _should_recheck (the "is the window up?" gatekeeper) --- #

def test_should_recheck_returns_false_when_interval_is_zero(sso, make_req):
    """The knob defaults to 0 (off).  In that case, no recheck happens
    regardless of the cookie state -- preserves existing back-compat
    behavior for operators who haven't opted in."""
    sso.recheck_interval_seconds = 0
    req = make_req(cookie="trac_auth=t; hubzero_cms_check=1")
    class _M(object): value = "1"
    req.incookie["hubzero_cms_check"] = _M()
    assert sso._should_recheck(req) is False


def test_should_recheck_true_when_check_cookie_absent(sso, make_req):
    """recheck enabled + no recheck cookie yet -> force a check on this
    request (covers first hit after the operator enables the knob)."""
    sso.recheck_interval_seconds = 60
    req = make_req(cookie="trac_auth=t")
    assert sso._should_recheck(req) is True


def test_should_recheck_false_when_check_cookie_recent(sso, make_req):
    """recheck cookie was stamped < interval ago -> within the window,
    no recheck."""
    sso.recheck_interval_seconds = 300
    fresh_ts = str(int(_time.time() - 5))                 # 5 seconds ago
    req = make_req(cookie="trac_auth=t")
    class _M(object): value = fresh_ts
    req.incookie["hubzero_cms_check"] = _M()
    assert sso._should_recheck(req) is False


def test_should_recheck_true_when_check_cookie_stale(sso, make_req):
    """recheck cookie was stamped > interval ago -> window expired,
    re-check this request."""
    sso.recheck_interval_seconds = 300
    stale_ts = str(int(_time.time() - 1000))              # 1000 seconds ago
    req = make_req(cookie="trac_auth=t")
    class _M(object): value = stale_ts
    req.incookie["hubzero_cms_check"] = _M()
    assert sso._should_recheck(req) is True


def test_should_recheck_true_when_cookie_value_unparseable(sso, make_req):
    """A tampered / corrupted timestamp value -> treat as a missing
    cookie, re-check.  Tampering can at most defer one re-check, never
    grant authentication, so loose parsing is fine."""
    sso.recheck_interval_seconds = 300
    req = make_req(cookie="trac_auth=t")
    class _M(object): value = "not-a-number"
    req.incookie["hubzero_cms_check"] = _M()
    assert sso._should_recheck(req) is True


# --- authenticate() integration with recheck --- #

def test_authenticate_skips_recheck_when_disabled(sso, make_req, http_stub):
    """recheck_interval_seconds=0 + warm cache: behaves exactly like
    pre-1.0.4 -- no API hit, just delegate to parent."""
    sso.recheck_interval_seconds = 0
    class _M(object): value = "abc"
    req = make_req(cookie="trac_auth=abc")
    req.incookie["trac_auth"] = _M()
    sso.authenticate(req)
    assert http_stub.last_conn is None                    # no API call
    assert req._super_calls == ["authenticate"]


def test_authenticate_skips_recheck_when_window_fresh(sso, make_req, http_stub):
    """Warm cache + recheck enabled + recent check timestamp -> no API
    call this request."""
    sso.recheck_interval_seconds = 300
    class _AM(object): value = "abc"
    class _CM(object): value = str(int(_time.time() - 5))
    req = make_req(cookie="trac_auth=abc; hubzero_cms_check=5")
    req.incookie["trac_auth"] = _AM()
    req.incookie["hubzero_cms_check"] = _CM()
    sso.authenticate(req)
    assert http_stub.last_conn is None                    # no API call
    assert req._super_calls == ["authenticate"]


def test_authenticate_recheck_continues_on_same_name(sso, env, make_req, http_stub):
    """Warm cache + recheck expired + CMS returns the SAME username ->
    don't invalidate; just re-stamp the recheck cookie and continue.

    Note: in real browser traffic, the request carries BOTH the trac_auth
    cookie (the Trac fast-path key) AND the CMS session cookie (jos_session
    or similar).  _cookie_header() strips trac_auth before forwarding, so
    the CMS receives the session cookie alone -- which is exactly what it
    needs to authenticate the user.  Tests must therefore include BOTH
    cookies in `cookie=` to mirror production."""
    sso.recheck_interval_seconds = 300
    # Stage CMS API to return "jdoe"
    http_stub.stage_response(200, b'{"profile":{"id":1,"username":"jdoe"}}')
    # Stage Trac auth_cookie lookup to also return "jdoe"
    env.db_query_responses.append([("jdoe",)])
    class _AM(object): value = "abc"
    class _CM(object): value = str(int(_time.time() - 1000))   # stale
    req = make_req(cookie="trac_auth=abc; jos_session=sid")
    req.incookie["trac_auth"] = _AM()
    req.incookie["hubzero_cms_check"] = _CM()
    sso.authenticate(req)
    # API was called (recheck happened) but Trac state was NOT invalidated
    assert http_stub.last_conn is not None
    assert env.db_transactions == []                      # no DELETE
    assert "trac_auth" in req.incookie                    # NOT removed
    # And the recheck cookie was re-stamped (a fresh timestamp)
    assert "hubzero_cms_check" in req.outcookie
    new_ts = int(req.outcookie["hubzero_cms_check"].value)
    assert new_ts > int(_time.time()) - 5                  # within last 5s


def test_authenticate_recheck_invalidates_on_username_rename(
        sso, env, make_req, http_stub):
    """Warm cache + recheck expired + CMS returns a DIFFERENT username ->
    invalidate trac_auth (DELETE auth_cookie row, expire cookie, remove
    from incookie so parent sees anonymous)."""
    sso.recheck_interval_seconds = 300
    http_stub.stage_response(200, b'{"profile":{"id":1,"username":"alyssa"}}')
    env.db_query_responses.append([("alice",)])           # Trac-side name
    class _AM(object): value = "abc"
    class _CM(object): value = str(int(_time.time() - 1000))
    req = make_req(cookie="trac_auth=abc; jos_session=sid")
    req.incookie["trac_auth"] = _AM()
    req.incookie["hubzero_cms_check"] = _CM()
    sso.authenticate(req)
    # auth_cookie row was DELETEd
    assert env.db_transactions, "expected a DELETE FROM auth_cookie"
    sql, params = env.db_transactions[0]
    assert sql.startswith("DELETE FROM auth_cookie")
    assert params == ("abc",)
    # trac_auth removed from incookie -> parent sees anonymous
    assert "trac_auth" not in req.incookie
    # _expire_cookie was called (via super)
    assert "_expire_cookie" in req._super_calls


def test_authenticate_recheck_invalidates_on_deactivation(
        sso, env, make_req, http_stub):
    """Warm cache + recheck expired + CMS returns 403 / no session ->
    same invalidation as a rename: user gets kicked back to anonymous."""
    sso.recheck_interval_seconds = 300
    http_stub.stage_response(403, b'{"message":"Access Denied"}')
    env.db_query_responses.append([("alice",)])
    class _AM(object): value = "abc"
    class _CM(object): value = str(int(_time.time() - 1000))
    req = make_req(cookie="trac_auth=abc; jos_session=sid")
    req.incookie["trac_auth"] = _AM()
    req.incookie["hubzero_cms_check"] = _CM()
    sso.authenticate(req)
    assert any(sql.startswith("DELETE FROM auth_cookie")
               for sql, _p in env.db_transactions)
    assert "trac_auth" not in req.incookie


# --- _do_login stamps the recheck cookie on a fresh login --- #

def test_do_login_stamps_recheck_cookie(sso, make_req):
    """After a successful login, the recheck cookie should carry a fresh
    timestamp so the next request doesn't immediately re-call the CMS
    (within the configured window)."""
    sso.recheck_interval_seconds = 300
    req = make_req(path_info="/login", remote_user="alice")
    sso._do_login(req)
    assert "hubzero_cms_check" in req.outcookie
    ts = int(req.outcookie["hubzero_cms_check"].value)
    assert ts > int(_time.time()) - 5
