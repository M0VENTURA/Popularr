# Playlist cover upload: the session token went in the wrong header (2026-10-14)

## What was reported
```
[WARNING] [services.playlists.playlist_service] [PLAYLISTS] Cover upload
rejected for Poppy - Essential Collection: http_401
```
…twice per playlist, permanently.

## Root cause — verified against Navidrome's source
Navidrome's native API does **not** read the standard `Authorization` header.
Its constant names the one it does read:

```go
consts.UIAuthorizationHeader = "X-ND-Authorization"      // consts/consts.go
```

and the verifier is wired to that name alone:

```go
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
```

So a token sent as `Authorization: Bearer …` never reaches the request context:
`UsernameFromToken` returns `""`, `authenticateRequest` fails, and
`Authenticator` answers

```go
rest.RespondWithError(w, http.StatusUnauthorized, "Not authenticated")
```

⭐ **The failure mode is misleading by construction.** The login SUCCEEDS, so
the reason was never `no_login` — and the client's own "expired token"
re-login + retry repeated the identical mistake, which is why the log shows the
same 401 twice and never recovers. (The playlist was found fine because
`find_playlist_by_name` goes through the Subsonic API, which has its own
salt/token auth.)

## The fix
* `api_clients/navidrome.py::upload_playlist_cover` — both the first attempt
  and the retry now send **`X-ND-Authorization: Bearer <token>`**. The
  docstring records the upstream constant and the verifier wiring, so the next
  reader does not have to re-derive it.
* Deliberately does **not** also send a standard `Authorization` header: it is
  useless to Navidrome, and behind a reverse proxy it can be validated by the
  PROXY and answered with the proxy's own 401.
* `services/playlists/playlist_service.py` — a 401 that survived the client's
  own re-login + retry is now reported as
  `"unauthorized — the server rejected the session token"` rather than a bare
  `http_401`, because the obvious reading ("expired session") sends an operator
  looking in the wrong place.

## Tests
* **New** `tests/test_playlist_cover_auth_header.py` (11): the upload uses
  `X-ND-Authorization` with the `Bearer ` prefix · it does **not** use the
  standard header · the **retry** uses the correct header too (it had its own
  copy) · a persistent 401 still reports `http_401` · the login posts JSON
  credentials · a failed login still reports `no_login` · the token is reused
  across calls · plus source guards pinning both the header and the new reason.
* **Updated** `tests/test_playlist_cover_native_api.py` — two assertions pinned
  `headers["Authorization"]`, i.e. they encoded the bug. Rewritten to the
  correct header, with the upstream citation.

**Oracle**: reverting the two source files (tests kept) → **7 failed / 4
passed** — the 4 are controls.

## Verification
Baseline-compared: `test_playlist_cover_native_api` **2F/11P at BOTH baseline
and here** (pre-existing `test_a_successful_upload_is_reported_as_such`,
`test_a_rejected_upload_is_reported_with_its_reason`),
`test_navidrome_playlist_post_body` 5F/2P pre-existing. Green:
`test_playlist_cover_auth_header` 11, `test_playlist_sync_auth_and_cap` 34,
`test_playlist_ordering` 67, `test_genre_playlists` 4,
`test_navidrome_config_guard` 7. `py_compile` clean.
