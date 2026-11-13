# AI Usage Disclosure

AI tools are allowed. We want to know **how** you used them, because using them well is a
skill. Honest answers are never held against you. Discovering undisclosed use is.

## Tools used
- Claude Code (Anthropic), in two separate sessions:
  - a planning session on Claude Opus: wrote the prompt for each milestone and reviewed every diff
    before it was merged;
  - an implementation session on Claude Sonnet: implemented each milestone, ran the tests and
    opened the pull requests.

## What I used them for
- Milestone by milestone, one pull request each:
  - parsers (header, phone, date, decimal, enum, timestamp);
  - lead ingest (validation, rejection, merging, PII protection);
  - the event ledger (append-only table, dedupe, query-time linking);
  - the reports in SQL;
  - these docs.
- Generating the tests in `solution/tests/`.
- An independent reference implementation, written from the assignment README only in plain Python
  with no SQL, used to cross-check the five report files on seeds 42 and 7. It was a throwaway
  script and is not committed.
- Drafting `solution/README.md`, `SUBMISSION.md` and this file.

## What I wrote or decided myself
<FILL: in your own words>

## Where the AI was wrong or unhelpful
- **Report SQL that never finished.** The first version of the report queries did not finish in
  30+ minutes on the large dataset (about 133k leads, 613k events). The first suggested fix, two
  temp tables, was still over 4 minutes. I found the cause by timing the run and reading
  `EXPLAIN QUERY PLAN`: SQLite planned the join to a CTE as a scan per lead. The fix that worked was
  materializing `call_facts`, `lead_calls`, `latest_disp` and `lead_stats` as TEMP tables keyed by
  `lead_id`. Output was byte-identical before and after.
- **`gh pr create` targeted the wrong repository.** It defaulted to the upstream (hiring team) repo
  as the base until `--repo` was passed explicitly, so every `gh` call now names the repo.
- **A false alarm from the reference check.** The reference script wrote CRLF line endings on
  Windows, which showed up as a diff in `data_quality.json` but was only line endings.
- **PII key file creation.** The first version wrote the key file with a plain `write_bytes`
  (default permissions, not atomic). It was changed to an exclusive, owner-only create in commit
  `6202403`.

## How I verified AI-generated code
- 79 pytest tests (parsers, lead ingest, event ledger, reports, and an end-to-end equivalence test).
- The independent reference implementation, diffed against all five report files on two seeds
  (42 and 7); all matched.
- Byte-identical reports across split ingest (`drop_1` then `drop_2`), one-shot ingest and repeated
  ingest, each with a fresh PII key.
- Every pull request diff was reviewed before merge.
- Real runs on generated data before each behaviour-changing commit.
