# Rating sync no longer targets an empty Navidrome row

**Date:** 2026-11-02
**Area:** Navidrome / popularity finalise
**Files:** `services/navidrome/rating_sync_service.py`,
`api_clients/navidrome.py`,
`tests/test_navidrome_rating_sync_user_selection.py` (new)

## Report

All rating syncs aborted after three consecutive failures while playlist
writes to the same server succeeded:

```
Aborting Navidrome rating sync after consecutive failures — Navidrome unreachable?
Navidrome rating syncs failed — check credentials failed=3 attempted=3
```

Playlist syncs to `http://192.168.1.200:4533` succeeded using `moventura`, so
the server was reachable and those credentials worked.

## Root cause

`get_rating_sync_clients` did `users = users[:1]` **before** validating the
entry. A setup-wizard stub — or any row with an empty `user`/`pass` — that
happened to sit first became the rating target, so every `setRating` carried
no credentials. The playlist paths iterate the configured users and skip
unusable rows, which is why they reached the real account.

Separately, a Subsonic `status = "failed"` arrives over HTTP 200 and code 70
("not found" — e.g. a stale Navidrome song ID left behind by ambiguous ID
adoption) is logged at DEBUG, so the only visible symptom was the generic
"check credentials" warning.

## Fix

- `get_rating_sync_clients` filters to entries with a non-empty
  `base_url` **and** `user` **and** `pass` before selecting the primary user.
  A row without credentials is not an account and can never be the target.
- `NavidromeClient.set_rating` logs a failed `setRating` at WARNING with the
  Subsonic error code/message and the username, so a stale ID (70) is
  distinguishable from an auth failure (40/41).

## Tests

`tests/test_navidrome_rating_sync_user_selection.py`:

| Test | Pins |
|---|---|
| `test_a_credential_less_first_row_is_skipped` | primary selection reaches `moventura` |
| `test_a_stub_after_the_primary_changes_nothing` | order does not regress |
| `test_usable_accounts_are_all_kept` | all-users mode keeps only real accounts |
| `test_only_stubs_yields_no_clients` / `test_empty_config_yields_no_clients` | fail closed |
| `test_a_failed_status_is_false` / `test_a_successful_status_is_true` | setRating contract |
