"""Local runner: the same pipeline without AWS.

    python -m solarops.cli init
    python -m solarops.cli ingest --files 12
    python -m solarops.cli status
    python -m solarops.cli underperformers
"""

import argparse
import logging

from . import config, db, nemweb, pipeline


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="solarops")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="create tables and load the solar farm registry")
    ingest = sub.add_parser("ingest", help="ingest recent NEMWeb files and current weather")
    ingest.add_argument("--files", type=int, default=12, help="how many recent 5-min files")
    sub.add_parser("status", help="show pipeline status")
    under = sub.add_parser("underperformers", help="farms below expected output now")
    under.add_argument("--threshold", type=float, default=0.6)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    conn = db.connect(config.database_url())
    db.migrate(conn)

    if args.command == "init":
        print(f"registry: {pipeline.sync_registry(conn)} solar units")
    elif args.command == "ingest":
        files = pipeline.pending_files(conn, nemweb.list_files(), args.files)
        for name in files:
            print(pipeline.process_file(conn, name))
        print(f"weather: {pipeline.sync_weather(conn)} observations")
    elif args.command == "status":
        for key, value in db.status(conn).items():
            print(f"{key:>16}: {value}")
    elif args.command == "underperformers":
        rows = db.underperformers(conn, args.threshold)
        if not rows:
            print("no farms below threshold at the latest interval (or it is night)")
        for r in rows:
            print(
                f"{r['facility_name']:<40} {r['region']:<5} actual {r['actual_mw']:>6} MW  "
                f"expected {r['expected_mw']:>6} MW  index {r['performance_index']}"
            )


if __name__ == "__main__":
    main()
