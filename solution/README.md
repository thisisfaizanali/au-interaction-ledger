# Interaction ledger and reporting pipeline (Assignment A)

A small Python pipeline that ingests lead CSVs and call-event JSONL files into SQLite, keeps phone
numbers and names out of the database in plaintext, and writes the five report files. Events are
stored append-only and linked to leads at query time, so the reports always reflect everything
ingested so far.

## Layout

```
solution/
├── ledger/
│   ├── parse.py      # pure value parsers: header, phone, date, decimal, enum, timestamp (IST)
│   ├── pii.py        # keyed phone hash (HMAC-SHA256) and Fernet encryption, key handling
│   ├── db.py         # SQLite schema: tables, append-only triggers, event_links and ledger views
│   ├── leads.py      # lead CSV ingest: header mapping, validation, rejection, merging
│   ├── events.py     # event JSONL ingest: malformed and duplicate handling, quality counters
│   ├── reports.py    # the five reports, aggregated in SQL
│   └── __main__.py   # CLI: ingest and report
└── tests/            # 79 pytest tests
```

## Run it from a clean clone

macOS / Linux:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python assignment-a-data/tools/generate_data.py --seed 42 --scale small --out data
cd solution
python -m ledger ingest ../data/drop_1
python -m ledger ingest ../data/drop_2
python -m ledger report --out ../reports
python -m pytest -q
```

Windows PowerShell:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python assignment-a-data/tools/generate_data.py --seed 42 --scale small --out data
cd solution
python -m ledger ingest ../data/drop_1
python -m ledger ingest ../data/drop_2
python -m ledger report --out ../reports
python -m pytest -q
```

`report` writes `batch_summary.csv`, `daily_overview.csv`, `lead_report.csv`, `rejections.csv` and
`data_quality.json` into the `--out` directory.

## Notes

- `--db PATH` (before the sub-command) picks the SQLite file. The default is `ledger.sqlite` in the
  current directory, which is `solution/` if you follow the commands above.
- `ingest` accepts several drop directories and processes them in the order given, for example
  `python -m ledger ingest ../data/drop_1 ../data/drop_2`. Each drop is one transaction.
- Re-running is safe. A file whose content hash was already ingested is skipped, so re-ingesting a
  drop changes nothing, and `report` can be run as often as you like.
- PII key: the key comes from the `AU_PII_KEY` environment variable (a Fernet key) if it is set.
  Otherwise `ingest` creates `<db path>.key` (for example `ledger.sqlite.key`) next to the database on
  first use. Both the database and the key file are covered by the existing `*.sqlite*` rule in
  `.gitignore`. If the key is lost, the stored names and phones can no longer be decrypted and
  new events will not match existing leads by phone. `report` does not need the key and neither loads
  nor creates it.
- Design, decisions and results are in the root `SUBMISSION.md`.
