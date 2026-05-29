# hubzero-trac-cmsauth

Single sign-on between HUBzero CMS and Trac. Drop-in replacement for the
LDAP-Basic auth that Trac envs use today (the `<LocationMatch login>` in
`/etc/httpd/<hub>.conf.d/00-forge-trac.conf`): a user already logged into
the CMS browses straight into Trac as themselves; an anonymous user hitting
Trac's `/login` is bounced to the CMS login page and lands back on Trac
authenticated.

## How it works

The plugin ships two Trac Components:

- **`HubzeroSessionAuthenticator`** (`IAuthenticator`) — on each request:
  1. **trac_auth cookie present + valid** → look it up in Trac's local
     `auth_cookie` table (the standard Trac auth-cookie store) and use that
     name. *No API call.* This is the steady-state common case.
  2. **No / invalid `trac_auth`, but a `Cookie:` header is present** →
     forward the cookie header to the local CMS at
     `https://127.0.0.1/api/v1.1/members/currentuser` with the right
     `Host:` so Apache's vhost matching routes to the CMS vhost. The CMS
     reads its own session cookie, looks up `jos_session`, and returns
     the profile. We use `profile.username` as the Trac name.
  3. **API success** → INSERT `(cookie, name, ip, time)` into `auth_cookie`,
     set `trac_auth` on the outgoing response, return the name. The user's
     browser now carries `trac_auth`; subsequent requests take path 1.

  Failures are conservative: any error (403/401 from CMS, 5xx, network
  failure, malformed JSON, missing `username` field) → `None` (anonymous).
  We never grant access on an API error.

- **`HubzeroLoginModule`** (`IRequestHandler` + `INavigationContributor`)
  — owns Trac's `/login` and `/logout`. Replaces
  `trac.web.auth.LoginModule`.
  - `/login` (anonymous) → 302 to `<cms_base>/login?return=<base64>`
  - `/login` (already authed) → 302 to env wiki home (no CMS round-trip)
  - `/logout` → `DELETE` the `auth_cookie` row, clear the `trac_auth`
    cookie, 302 to `<cms_base>/logout?return=<base64>`. HUBzero's
    `com_login` does the whole-origin logout (clears `jos_session` +
    its session cookie) then bounces back to Trac, where the user is now
    anonymous.

  The base64 return URL matches HUBzero's `com_login` convention
  (`com_login/site/controllers/auth.php`).

## Why a `trac_auth` cookie?

Without it, every Trac request — including every CSS, image, attachment
sub-request — would re-call the CMS API. The cookie reduces that to
**one API call per browser session**. After the first successful auth
the user's browser carries `trac_auth`; subsequent requests are handled
entirely by Trac's local `auth_cookie` table lookup.

The cookie is session-scoped (no `Expires`/`Max-Age`) so it dies on browser
close. The "user logged out of HUBzero but `trac_auth` still alive in the
same browser session" window is the trade-off; `/logout` from within Trac
closes it cleanly via the CMS logout redirect.

## Configuration

There's deliberately **no host/port/scheme config** for the API call: the
plugin uses the exact same scheme + host + port the browser used to reach
Trac (read from the request's `wsgi.url_scheme` + `HTTP_HOST`). That means:

- nothing to set per-env,
- nothing that can drift between Trac and the CMS — if the user can reach
  Trac at `https://help.hubzero.org`, the API is reachable at the same
  origin by definition,
- whatever Apache vhost + cert + rate-limit policy serves the user also
  serves the API call.

The remaining knobs all live under `[hubzero_cmsauth]` in `trac.ini`:

| key | default | meaning |
|---|---|---|
| `api_path` | `/api/v1.1/members/currentuser` | API endpoint that returns the current user's profile |
| `api_timeout_seconds` | `5` | per-request timeout for the API call |
| `check_auth_ip` | `true` | reject `trac_auth` cookie on IP mismatch |
| `auth_cookie_path` | `""` (= env href) | `Path` attribute for the `trac_auth` cookie |
| `cms_login_url` | `/login` | path on the CMS host that handles login |
| `cms_logout_url` | `/logout` | path on the CMS host that handles logout |
| `cms_base_url` | `""` (= incoming scheme+host) | absolute URL prefix for the CMS host |

To enable the plugin in a Trac env, edit `<env>/conf/trac.ini`:

```ini
[components]
hubzero_cmsauth.* = enabled
trac.web.auth.LoginModule = disabled
```

The `LoginModule = disabled` line is important — Trac's built-in
`LoginModule` also implements `IRequestHandler` for `/login` and
`/logout`, so leaving it enabled would race ours.

## Running the tests

```sh
cd plugins/cmsauth
make            # = make test = make test-py3  -> 24 tests, ~0.3s on Py3.6
make test-py2   # same suite on Py2.7
make test-all   # both
```

The `Makefile` shells out to `python3 -m pytest` / `python2 -m pytest`
(no venv, no tox -- see [`../mysqlauthz/README.md`](../mysqlauthz/README.md#running-the-tests)
for the why-no-tox explanation). One-time setup per host:

```sh
python3 -m pip install --user 'pytest>=7,<7.1'
python2 -m pip install --user 'pytest<5'
```

Trac, the CMS API, and the `auth_cookie` table are all stubbed in
`tests/conftest.py`; no real HTTP or DB. Tests cover the full decision
tree of the authenticator (trac_auth fast path, API slow path, every
error case → anonymous fail-safe), `auth_cookie` issuance and DB-error
behavior, `/login` redirect + `return` URL construction, and `/logout`
DELETE-then-redirect.

## Provenance

- **First release:** `1.0.0` (hzforge, 2026-05-28). No upstream — this
  is a new plugin written for hzforge.
- **`1.0.2`** (2026-05-29) — one security fix in `_call_api`'s
  response handling:
  - **Reserved-username guard.** If the CMS API ever returns
    `profile.username == "anonymous"` (or `"authenticated"`, or any
    case/whitespace variant), the bearer would have been authenticated
    AS Trac's reserved anonymous user — granting them the perms of the
    anonymous group itself and joining the special `authenticated`
    group. The plugin now strips whitespace, rejects reserved names
    case-insensitively, logs a warning, and falls back to anonymous.
    Also returns the stripped form of legitimate usernames so
    downstream `auth_cookie` / `IPermissionStore` comparisons stay
    well-defined.
- **`1.0.1`** (2026-05-29) — two security fixes, both in `_redirect_back`
  / `_b64`, no behavior change for legitimate flows:
  - **Open-redirect guard** on the `?referer=` arg. Stock
    `trac.web.auth.LoginModule` validates that the referer is same-site
    before honoring it; the 1.0.0 override dropped that check, so
    `/login?referer=https://evil.com/phish` would 302 a just-logged-in
    user to evil.com. 1.0.1 introduces `_is_same_origin(req, target)`
    (host-relative paths and same scheme+host absolute URLs are
    honored; cross-origin, protocol-relative, scheme-mismatched, and
    lookalike-host targets fall back to the env wiki home).
  - **URL-safe base64** for the CMS `?return=` value. Standard base64
    can emit `+` and `/`, both of which break query-string parsing
    (the CMS sees space where the browser sent `+`). Switched
    `_b64()` from `base64.b64encode` to `base64.urlsafe_b64encode`
    (PHP's `base64_decode` accepts both alphabets, so it's safe on the
    receiving end).
- **Pairs with:**
  - `hubzero-trac-mysqlauthz` 2.4.1 (`IPermissionStore` /
    `IPermissionGroupProvider`) — provides Trac authorization once cmsauth
    has set the user.
  - The Apache config that hzforge writes via `hzforge install trac` — the
    `<LocationMatch login>` LDAP-Basic block needs to be removed (or
    carved-out per env) once cmsauth is enabled.
