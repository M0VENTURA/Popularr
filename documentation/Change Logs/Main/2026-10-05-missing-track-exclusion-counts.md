# The missing-track response says WHY each track is absent (2026-10-05)

**Report:** after a Lookup MBID, missing rows render for **1–9, 12, 13** but
**10 and 11 are absent** — with nothing in the log or the payload to say why.

## Four gates, no verdict

`get_missing_tracks` drops a release track when:

| gate | condition |
|---|---|
| `in_library` | a library row occupies that **(disc, track)** — *or* the title matches |
| `queued` | the track is queued / searching / downloading / imported / completed |
| `rejected` | the user clicked Ignore (`missing_album_tracks.ignored = TRUE`) |
| — | no title in the MB row at all |

The first gate is the sneaky one: **position counts even when the title
differs**, so a *wrong* track sitting at disc 1 / track 10 makes 10 look
present. That is the gate most likely to have hidden the reported pair — and
the response gave `mb_total`, `library_count` and `missing_count` but never
said which filter removed the rest.

## Change

- `get_missing_tracks` now counts each gate into `excluded`
  (`in_library` / `queued` / `rejected`) and returns it.
- The arithmetic is self-checking by construction:
  `len(missing_tracks) + sum(excluded.values()) == mb_total` — a track missing
  from *both* the list and the counts becomes impossible.
- The album page's **`refresh=1`** path (the Lookup MBID one) logs one INFO
  line with the breakdown:

  ```
  [ALBUM] missing-tracks refresh … mb_total=13 library_count=11
          missing_count=2 excluded={'in_library': 11, 'queued': 0, 'rejected': 0}
  ```

  Page loads stay quiet — they only read the snapshot.

So "why are 10 and 11 absent?" becomes a log line: `in_library` means a
different track occupies that position; `queued` means a previous attempt owns
it; `rejected` means an old Ignore.

## Test-infra note (same commit)

The suite shares one SQLite file and several tests hand-create a *reduced*
`download_queue`, so `CREATE TABLE IF NOT EXISTS` can leave a column behind.
Both queue-table fixtures now **repair** missing columns via `PRAGMA
table_info` + `ALTER TABLE`, and teardown tolerates a table another test has
recreated — without that, running these files together failed on
`no column named priority` / `created_at` and blamed the wrong test.

## Tests

`tests/test_album_lookup_findings.py` — **4 new**: the gate is named, the
arithmetic invariant, a control where nothing is excluded, and the **queued**
gate (the one most likely to hide 10 and 11, since it hides a track someone
already started downloading).

Oracle: stashing the three source files → **6 of 115 fail** — exactly these
four plus the two cover-verdict tests.
