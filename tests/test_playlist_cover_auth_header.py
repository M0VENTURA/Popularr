"""The playlist cover upload must send Navidrome's OWN auth header.

Reported
--------
```
[WARNING] [services.playlists.playlist_service] [PLAYLISTS] Cover upload
rejected for Poppy - Essential Collection: http_401
```
twice per playlist, permanently.

Root cause — verified against Navidrome's source, not guessed
-------------------------------------------------------------
Navidrome's native API does NOT read the standard ``Authorization`` header.
Its constant names the header it does read:

    consts.UIAuthorizationHeader = "X-ND-Authorization"      // consts/consts.go

and the verifier is wired to that name alone:

    func JWTVerifier(next http.Handler) http.Handler {
        return jwtauth.Verify(auth.TokenAuth, tokenFromHeader,
            jwtauth.TokenFromCookie, jwtauth.TokenFromQuery)(next)
    }

    func tokenFromHeader(r *http.Request) string {
        bearer := r.Header.Get(consts.UIAuthorizationHeader)   // ← X-ND-Authorization
        if len(bearer) > 7 && strings.ToUpper(bearer[0:6]) == "BEARER" {
            return bearer[7:]
        }
        return ""
    }

So a token sent as ``Authorization: Bearer …`` never reaches the request
context. ``UsernameFromToken`` returns "", ``authenticateRequest`` fails and
``Authenticator`` answers:

    rest.RespondWithError(w, http.StatusUnauthorized, "Not authenticated")

⭐ The failure mode is misleading by construction: the LOGIN SUCCEEDS, so the
reason was never ``no_login``, and the client's own "expired token" re-login +
retry repeats the identical mistake — which is why the log shows the same 401
twice and never recovers.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
NAVIDROME_CLIENT = REPO_ROOT / "api_clients" / "navidrome.py"
PLAYLIST_SERVICE = REPO_ROOT / "services" / "playlists" / "playlist_service.py"

#: The header Navidrome actually reads (consts.UIAuthorizationHeader).
ND_HEADER = "X-ND-Authorization"


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = ""

    def json(self):
        return self._payload


class _RecordingSession:
    """Records every POST: URL, headers and files."""

    def __init__(self, *, upload_status: int = 200, login_ok: bool = True):
        self.upload_status = upload_status
        self.login_ok = login_ok
        self.posts: list[dict] = []

    def post(self, url, headers=None, json=None, files=None, timeout=None, **kwargs):
        self.posts.append({
            "url": url, "headers": dict(headers or {}),
            "json": json, "files": files,
        })
        if url.endswith("/auth/login"):
            if not self.login_ok:
                return _FakeResponse(401)
            return _FakeResponse(200, {"token": "tok-123", "id": "u1", "isAdmin": True})
        return _FakeResponse(self.upload_status, {"status": "ok"})

    def get(self, url, **kwargs):  # pragma: no cover - not used here
        return _FakeResponse(200, {})


def _client(session: _RecordingSession):
    from api_clients.navidrome import NavidromeClient

    return NavidromeClient("http://nav.local:4533", "admin", "pw", http_session=session)


def _upload(session: _RecordingSession, **kwargs):
    client = _client(session)
    return client.upload_playlist_cover("pl-1", b"\xff\xd8\xff\xe0jpegbytes", **kwargs)


def _upload_posts(session: _RecordingSession) -> list[dict]:
    return [p for p in session.posts if not p["url"].endswith("/auth/login")]


# ===========================================================================
# 1. The header contract
# ===========================================================================


class TestTheTokenGoesInTheHeaderNavidromeReads:
    def test_the_upload_uses_x_nd_authorization(self):
        """THE REPORTED DEFECT: the token went in ``Authorization``, which
        Navidrome's verifier never reads."""
        session = _RecordingSession()

        ok, reason = _upload(session)

        assert ok is True and reason == ""
        posts = _upload_posts(session)
        assert len(posts) == 1
        assert posts[0]["headers"].get(ND_HEADER) == "Bearer tok-123"

    def test_the_upload_does_not_use_the_standard_header(self):
        """CONTROL — a stray ``Authorization`` header is not merely useless
        here: behind a reverse proxy it can be validated by the PROXY and
        answered with its own 401, so it must not be sent at all."""
        session = _RecordingSession()

        _upload(session)

        assert "Authorization" not in _upload_posts(session)[0]["headers"]

    def test_the_bearer_prefix_is_present(self):
        """``tokenFromHeader`` only accepts the value when it starts with
        "BEARER" (case-insensitively) and is longer than 7 characters."""
        session = _RecordingSession()

        _upload(session)

        value = _upload_posts(session)[0]["headers"][ND_HEADER]
        assert value.upper().startswith("BEARER ")
        assert len(value) > 7

    def test_the_retry_also_uses_the_correct_header(self):
        """The retry path had its own copy of the header — a one-place fix
        would leave the retry still broken."""
        session = _RecordingSession(upload_status=401)

        ok, reason = _upload(session)

        assert ok is False
        posts = _upload_posts(session)
        assert len(posts) == 2, "401 must trigger exactly one re-login + retry"
        for post in posts:
            assert post["headers"].get(ND_HEADER, "").startswith("Bearer ")
            assert "Authorization" not in post["headers"]

    def test_a_persistent_401_still_reports_http_401(self):
        """CONTROL — the reason contract is unchanged; only the cause is."""
        session = _RecordingSession(upload_status=401)

        _ok, reason = _upload(session)

        assert reason == "http_401"


class TestTheLoginContract:
    def test_the_login_posts_json_credentials(self):
        """``getCredentialsFromBody`` decodes ``{username, password}`` from a
        JSON body — form-encoding would fail the parse."""
        session = _RecordingSession()

        _upload(session)

        logins = [p for p in session.posts if p["url"].endswith("/auth/login")]
        assert len(logins) == 1
        assert logins[0]["json"] == {"username": "admin", "password": "pw"}

    def test_a_failed_login_reports_no_login(self):
        """CONTROL — the reason that IS about the login stays distinct."""
        session = _RecordingSession(login_ok=False)

        ok, reason = _upload(session)

        assert ok is False
        assert reason == "no_login"
        assert _upload_posts(session) == [], "no upload without a token"

    def test_the_token_is_reused_across_calls(self):
        """CONTROL — the token is cached per client, so a second upload does
        not re-login."""
        session = _RecordingSession()
        client = _client(session)

        client.upload_playlist_cover("pl-1", b"\xff\xd8\xff\xe0a")
        client.upload_playlist_cover("pl-2", b"\xff\xd8\xff\xe0b")

        logins = [p for p in session.posts if p["url"].endswith("/auth/login")]
        assert len(logins) == 1


# ===========================================================================
# 2. Source guards (the fix cannot silently regress)
# ===========================================================================


class TestTheSourcePinsTheHeader:
    def test_the_client_sends_the_navidrome_header(self):
        source = NAVIDROME_CLIENT.read_text(encoding="utf-8")

        assert f'"{ND_HEADER}": f"Bearer {{token}}"' in source, (
            "the upload must send Navidrome's own auth header"
        )

    def test_the_client_never_sends_a_bare_authorization_header(self):
        source = NAVIDROME_CLIENT.read_text(encoding="utf-8")

        assert 'headers={"Authorization": f"Bearer {token}"}' not in source, (
            "the standard header is never read by Navidrome's verifier — and "
            "can be answered by a reverse proxy with its own 401"
        )

    def test_the_reason_names_the_real_cause(self):
        """A 401 that survived the client's own retry is NOT a stale token —
        the operator message must not send them chasing one."""
        source = PLAYLIST_SERVICE.read_text(encoding="utf-8")

        assert '"http_401": "unauthorized' in source
        assert "the server rejected the session token" in source
