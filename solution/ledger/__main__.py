"""CLI: python -m ledger [--db PATH] ingest DROP_DIR [DROP_DIR ...] | report --out DIR"""

import argparse
import sys
from pathlib import Path

from . import db
from .events import ingest_events, load_dispositions
from .leads import ingest_leads
from .pii import Pii


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="ledger")
    p.add_argument("--db", default="ledger.sqlite", help="SQLite file (default: ledger.sqlite)")
    sub = p.add_subparsers(dest="command", required=True)
    ing = sub.add_parser("ingest", help="ingest one or more drop directories, in the order given")
    ing.add_argument("drops", nargs="+", type=Path, metavar="DROP_DIR")
    rep = sub.add_parser("report", help="write the report files")
    rep.add_argument("--out", required=True, type=Path)
    args = p.parse_args(argv)

    if args.command == "report":
        print("report: not implemented yet", file=sys.stderr)
        return 2

    pii = Pii.load(args.db)
    conn = db.connect(args.db)
    try:
        load_dispositions(conn)
        conn.commit()
        for drop in args.drops:
            print(f"== {drop}")
            try:
                for line in ingest_leads(conn, pii, drop):
                    print(line)
                for line in ingest_events(conn, pii, drop):
                    print(line)
            except BaseException:
                conn.rollback()  # one transaction per drop: nothing half-applied
                raise
            conn.commit()
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
