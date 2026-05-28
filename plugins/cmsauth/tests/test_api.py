"""Test suite for hubzero-trac-cmsauth.

Cover the IAuthenticator decision tree (trac_auth fast path, CMS API slow
path, all the error cases) and the IRequestHandler /login + /logout flows.
Trac, the CMS API, and the DB are stubbed in conftest.py; no real HTTP or
network is involved.
"""
from __future__ import absolute_import, division, print_function, unicode_literals

import base64
import socket

import pytest

from hubzero_cmsauth.api import (
    HubzeroSessionAuthenticator,
    HubzeroLoginModule,
    _LOGIN_PATH_RE,
    _LOGOUT_PATH_RE,
)


# ============================================================================ #
# HubzeroSessionAuthenticator
# ============================================================================ #

# --- fast path: trac_auth cookie ----------------------------------------- #

def test_authenticate_returns_name_from_valid_trac_auth_cookie(
        authenticator, env, make_req, http_stub):
    """trac_auth cookie present + matches a row in auth_cookie + IP matches
    -> return the cached name WITHOUT calling the CMS API."""
    env.stage_db([("jdoe", "10.0.0.5")])    # one row: (name, ipnr)
    req = make_req(cookie="trac_auth=abc123")
    req.environ["REMOTE_ADDR"] = "10.0.0.5"
    req.incookie["trac_auth"] = _Morsel("abc123")

    assert authenticator.authenticate(req) == "jdoe"
    # auth_cookie SELECT happened; no CMS API call
    assert env.db_dbs[0].executions[0][0].startswith("SELECT name, ipnr FROM auth_cookie")
    assert http_stub.last_conn is None
    # No new trac_auth cookie issued (the one we have is still valid)
    assert "trac_auth" not in req.outcookie


def test_authenticate_rejects_trac_auth_with_mismatched_ip(
        authenticator, env, make_req, http_stub):
    """check_auth_ip on by default: trac_auth value matches a row but IP
    doesn't -> ignore the cookie, fall through to the API path."""
    env.stage_db([("jdoe", "192.0.2.1")])    # row exists, IP "192.0.2.1"
    env.stage_db([])                          # auth_cookie INSERT (no result)
    req = make_req(cookie="trac_auth=abc123; jos_session=somesid")
    req.environ["REMOTE_ADDR"] = "10.0.0.5"   # different IP
    req.incookie["trac_auth"] = _Morsel("abc123")

    assert authenticator.authenticate(req) == "jdoe"        # via API path
    # ... and the API was called because trac_auth was rejected
    assert http_stub.last_conn is not None


def test_authenticate_returns_none_for_unknown_trac_auth(
        authenticator, env, make_req, http_stub):
    """trac_auth cookie unknown + CMS session also gone -> anonymous.
    The code forwards the orphan trac_auth cookie to the CMS API; the API
    (correctly) sees no matching jos_session row and replies 403."""
    env.stage_db([])                                         # no auth_cookie row
    http_stub.stage_response(403, b'{"message":"Access Denied"}')
    req = make_req(cookie="trac_auth=expired")
    req.incookie["trac_auth"] = _Morsel("expired")

    assert authenticator.authenticate(req) is None
    # API WAS called (cookie header existed -- the plugin can't tell trac_auth
    # from a CMS cookie without lookup), but returned 403 -> anonymous
    assert http_stub.last_conn is not None
    # No new auth_cookie row issued (no successful auth)
    assert "trac_auth" not in req.outcookie


def test_authenticate_db_error_during_lookup_falls_through_to_api(
        authenticator, env, make_req, http_stub):
    """If the auth_cookie SELECT itself raises, treat trac_auth as absent
    and try the API.  Don't fail the request."""
    env.stage_db_error(RuntimeError("db locked"))
    env.stage_db([])                                 # auth_cookie INSERT
    req = make_req(cookie="trac_auth=abc; jos_session=sid")
    req.incookie["trac_auth"] = _Morsel("abc")

    assert authenticator.authenticate(req) == "jdoe"
    assert http_stub.last_conn is not None


# --- no cookies at all (the obvious anon path) --------------------------- #

def test_authenticate_returns_none_when_no_cookies(
        authenticator, env, make_req, http_stub):
    req = make_req()    # no cookie
    assert authenticator.authenticate(req) is None
    assert http_stub.last_conn is None   # no API call -- nothing to forward


# --- slow path: CMS API -------------------------------------------------- #

def test_authenticate_calls_api_and_returns_username(
        authenticator, env, make_req, http_stub):
    """No trac_auth cookie, CMS Cookie present -> API call -> 200 -> name."""
    env.stage_db([])    # auth_cookie INSERT
    req = make_req(cookie="jos_session=sid")

    assert authenticator.authenticate(req) == "jdoe"
    assert http_stub.last_conn.calls[0]["method"] == "GET"
    assert http_stub.last_conn.calls[0]["url"] == "/api/v1.1/members/currentuser"
    # Cookie forwarded as-is
    assert http_stub.last_conn.calls[0]["headers"]["Cookie"] == "jos_session=sid"


def test_authenticate_forwards_host_header_from_request(
        authenticator, env, make_req, http_stub):
    """The Host header on the API request matches the incoming request's
    Host, so Apache routes to the same vhost."""
    env.stage_db([])
    req = make_req(cookie="jos_session=sid")
    req.environ["HTTP_HOST"] = "help.hubzero.org"

    authenticator.authenticate(req)
    assert http_stub.last_conn.calls[0]["headers"]["Host"] == "help.hubzero.org"


def test_authenticate_treats_403_as_anonymous(
        authenticator, env, make_req, http_stub):
    http_stub.stage_response(403, b'{"message":"Access Denied","code":403}')
    req = make_req(cookie="jos_session=sid")
    assert authenticator.authenticate(req) is None


def test_authenticate_treats_401_as_anonymous(
        authenticator, env, make_req, http_stub):
    http_stub.stage_response(401, b'')
    req = make_req(cookie="jos_session=sid")
    assert authenticator.authenticate(req) is None


def test_authenticate_treats_500_as_anonymous_fail_safe(
        authenticator, env, make_req, http_stub):
    """Server error -> conservative: anonymous.  We NEVER grant access on
    an API failure."""
    http_stub.stage_response(500, b'<html>Server Error</html>')
    req = make_req(cookie="jos_session=sid")
    assert authenticator.authenticate(req) is None


def test_authenticate_treats_network_error_as_anonymous(
        authenticator, env, make_req, http_stub):
    http_stub.stage_exception(socket.timeout("API hung"))
    req = make_req(cookie="jos_session=sid")
    assert authenticator.authenticate(req) is None


def test_authenticate_treats_malformed_json_as_anonymous(
        authenticator, env, make_req, http_stub):
    http_stub.stage_response(200, b'<html>not json</html>')
    req = make_req(cookie="jos_session=sid")
    assert authenticator.authenticate(req) is None


def test_authenticate_treats_missing_profile_field_as_anonymous(
        authenticator, env, make_req, http_stub):
    http_stub.stage_response(200, b'{"unrelated": "data"}')
    req = make_req(cookie="jos_session=sid")
    assert authenticator.authenticate(req) is None


def test_authenticate_treats_missing_username_as_anonymous(
        authenticator, env, make_req, http_stub):
    """profile dict but no username field -> anonymous (don't guess)."""
    http_stub.stage_response(200, b'{"profile":{"id":1,"name":"Jane"}}')
    req = make_req(cookie="jos_session=sid")
    assert authenticator.authenticate(req) is None


# --- trac_auth cookie issuance ------------------------------------------- #

def test_authenticate_issues_trac_auth_cookie_on_api_success(
        authenticator, env, make_req, http_stub):
    """API said yes -> INSERT row into auth_cookie, set trac_auth cookie
    on the outgoing response (Secure, HttpOnly, path = env href)."""
    env.stage_db([])
    req = make_req(cookie="jos_session=sid")
    req.environ["REMOTE_ADDR"] = "203.0.113.5"

    authenticator.authenticate(req)

    # auth_cookie INSERT happened with (cookie_value, "jdoe", "203.0.113.5", time)
    insert = env.db_dbs[0].executions[0]
    assert insert[0].startswith("INSERT INTO auth_cookie")
    cookie_value, name, ipnr, t = insert[1]
    assert name == "jdoe"
    assert ipnr == "203.0.113.5"
    assert isinstance(t, int) and t > 0
    assert len(cookie_value) == 40   # 20 random bytes -> 40 hex chars

    # And the same value lands on the outgoing cookie
    morsel = req.outcookie["trac_auth"]
    assert morsel.value == cookie_value
    assert morsel["secure"]   is True or morsel["secure"]   == True   # noqa: E712
    assert morsel["httponly"] is True or morsel["httponly"] == True   # noqa: E712
    # No expires/max-age -> session cookie
    assert not morsel["expires"]


def test_authenticate_db_insert_error_does_not_fail_request(
        authenticator, env, make_req, http_stub):
    """auth_cookie INSERT raises -> still return the username (next request
    will re-auth via API).  Don't fail the response just to set a cookie."""
    env.stage_db_error(RuntimeError("disk full"))
    req = make_req(cookie="jos_session=sid")

    assert authenticator.authenticate(req) == "jdoe"
    assert "trac_auth" not in req.outcookie


# ============================================================================ #
# HubzeroLoginModule
# ============================================================================ #

# --- routing ------------------------------------------------------------- #

def test_match_request_only_matches_login_and_logout():
    assert _LOGIN_PATH_RE.match("/login")
    assert _LOGIN_PATH_RE.match("/login/")
    assert not _LOGIN_PATH_RE.match("/loginx")
    assert not _LOGIN_PATH_RE.match("/login/extra")
    assert not _LOGIN_PATH_RE.match("/Login")     # case-sensitive
    assert _LOGOUT_PATH_RE.match("/logout")
    assert _LOGOUT_PATH_RE.match("/logout/")
    assert not _LOGOUT_PATH_RE.match("/logoutall")


def test_match_request_method(login_module, make_req):
    assert login_module.match_request(make_req(path_info="/login")) is True
    assert login_module.match_request(make_req(path_info="/logout")) is True
    assert login_module.match_request(make_req(path_info="/wiki/Foo")) is False


# --- /login flow --------------------------------------------------------- #

def test_login_redirects_anonymous_to_cms_with_base64_return(
        login_module, make_req, RedirectDone):
    req = make_req(path_info="/login")    # authname='anonymous' by default

    with pytest.raises(RedirectDone) as excinfo:
        login_module.process_request(req)

    target = excinfo.value.url
    # Goes to the same host (HTTP_HOST -> help.hubzero.org), /login, with ?return
    assert target.startswith("https://help.hubzero.org/login?return=")
    encoded = target.split("return=", 1)[1]
    decoded = base64.b64decode(encoded).decode("utf-8")
    # ... and the decoded return URL is the env wiki home (path only)
    assert decoded == "/tools/hzforgetest/wiki"


def test_login_skips_cms_redirect_when_already_authenticated(
        login_module, make_req, RedirectDone):
    req = make_req(path_info="/login", authname="jdoe")

    with pytest.raises(RedirectDone) as excinfo:
        login_module.process_request(req)

    # Bounce straight to the wiki -- no CMS round-trip.  Path-form (same
    # origin); _wiki_home strips scheme+host.
    assert excinfo.value.url == "/tools/hzforgetest/wiki"


# --- /logout flow -------------------------------------------------------- #

def test_logout_deletes_auth_cookie_and_clears_trac_auth(
        login_module, env, make_req, RedirectDone):
    env.stage_db([])    # DELETE result-set (irrelevant)
    req = make_req(path_info="/logout", authname="jdoe", cookie="trac_auth=xyz789")
    req.incookie["trac_auth"] = _Morsel("xyz789")

    with pytest.raises(RedirectDone) as excinfo:
        login_module.process_request(req)

    # auth_cookie DELETE happened with the cookie value
    deletion = env.db_dbs[0].executions[0]
    assert deletion[0].startswith("DELETE FROM auth_cookie")
    assert deletion[1] == ("xyz789",)

    # trac_auth cookie cleared on the response (value empty + expires-in-past)
    morsel = req.outcookie["trac_auth"]
    assert morsel.value == ""
    assert morsel["expires"] == "Thu, 01 Jan 1970 00:00:00 GMT"

    # Redirect target is CMS /logout with ?return=<base64>
    target = excinfo.value.url
    assert target.startswith("https://help.hubzero.org/logout?return=")
    decoded = base64.b64decode(target.split("return=", 1)[1]).decode("utf-8")
    assert decoded == "/tools/hzforgetest/wiki"


def test_logout_without_trac_auth_still_redirects(
        login_module, env, make_req, RedirectDone):
    """User clicks logout but has no trac_auth cookie (e.g. session-cookie
    expired).  Just redirect -- no DB op, no cookie clear (nothing to clear).
    """
    req = make_req(path_info="/logout")
    # no incookie['trac_auth'] set

    with pytest.raises(RedirectDone) as excinfo:
        login_module.process_request(req)

    assert excinfo.value.url.startswith("https://help.hubzero.org/logout?return=")
    # No DB call was attempted
    assert env.db_dbs == []


def test_logout_db_delete_error_does_not_block_redirect(
        login_module, env, make_req, RedirectDone):
    """DELETE failed -> still clear the cookie + redirect to CMS logout.
    The user wanted out; getting them to the CMS logout flow matters more
    than perfect DB cleanup."""
    env.stage_db_error(RuntimeError("db locked"))
    req = make_req(path_info="/logout", cookie="trac_auth=xyz")
    req.incookie["trac_auth"] = _Morsel("xyz")

    with pytest.raises(RedirectDone):
        login_module.process_request(req)

    # Cookie was cleared even though DB failed
    assert req.outcookie["trac_auth"].value == ""


# --- different env name preserved in return URL ------------------------- #

def test_login_redirect_carries_correct_env_in_return_url(
        login_module, make_req, RedirectDone):
    """Different env -> different return URL.  We don't hard-code hzforgetest."""
    req = make_req(path_info="/login",
                   abs_base="https://help.hubzero.org/tools/bio3d")

    with pytest.raises(RedirectDone) as excinfo:
        login_module.process_request(req)

    decoded = base64.b64decode(
        excinfo.value.url.split("return=", 1)[1]).decode("utf-8")
    assert decoded == "/tools/bio3d/wiki"


# ============================================================================ #
# helpers
# ============================================================================ #

class _Morsel(object):
    """Mimics http.cookies.Morsel -- just exposes .value (what the plugin
    reads off req.incookie['trac_auth'])."""

    def __init__(self, value):
        self.value = value
