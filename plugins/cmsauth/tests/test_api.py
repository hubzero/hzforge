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

def test_cookie_header_from_environ(sso, make_req):
    req = make_req(cookie="a=1; b=2; trac_auth=xyz")
    assert sso._cookie_header(req) == "a=1; b=2; trac_auth=xyz"


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
