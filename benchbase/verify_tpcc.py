r"""Read-only checks for a fresh, one-warehouse TPC-C-like dataset.

Run from the project directory:
    .\.venv\Scripts\python.exe .\benchbase\verify_tpcc.py

Expected initial counts change after running a workload. This script does not
reset data and is not a complete TPC-C consistency audit.
"""

from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
from xml.etree import ElementTree

import mysql.connector
import psycopg


BASE = Path(__file__).resolve().parent
EXPECTED_COUNTS = {
    "warehouse": 1,
    "district": 10,
    "customer": 30_000,
    "history": 30_000,
    "item": 100_000,
    "stock": 100_000,
    "oorder": 30_000,
    "new_order": 9_000,
}
TABLES = (*EXPECTED_COUNTS, "order_line")


def connect(dbms):
    config = ElementTree.parse(BASE / "config" / f"tpcc-{dbms}.xml").getroot()
    if float(config.findtext("scalefactor")) != 1:
        raise ValueError("This verification script requires scalefactor=1.")
    options = {
        "host": "127.0.0.1",
        "user": config.findtext("username"),
        "password": config.findtext("password"),
        "autocommit": True,
    }
    if dbms == "postgres":
        return psycopg.connect(
            **options, port=5432, dbname="tpcc", connect_timeout=5
        )
    return mysql.connector.connect(
        **options, port=3307, database="tpcc", connection_timeout=5
    )


def verify(dbms):
    errors = []
    counts = {}
    with closing(connect(dbms)) as connection:
        with closing(connection.cursor()) as cursor:
            if dbms == "postgres":
                cursor.execute(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema='public' AND table_type='BASE TABLE'"
                )
                tables = {row[0] for row in cursor.fetchall()}
            else:
                cursor.execute(
                    "SELECT table_name, engine FROM information_schema.tables "
                    "WHERE table_schema=DATABASE() AND table_type='BASE TABLE'"
                )
                engines = dict(cursor.fetchall())
                tables = set(engines)
                for table in TABLES:
                    if table in engines and engines[table] != "InnoDB":
                        errors.append(f"{table}: expected InnoDB, got {engines[table]}")

            if tables != set(TABLES):
                raise ValueError(f"Unexpected table set: {sorted(tables)}")

            for table in TABLES:
                # Identifiers come exclusively from the fixed TABLES list above.
                cursor.execute(f"SELECT COUNT(*) FROM {table}")
                counts[table] = cursor.fetchone()[0]
                expected = EXPECTED_COUNTS.get(table)
                if expected is not None and counts[table] != expected:
                    errors.append(f"{table}: expected {expected}, got {counts[table]}")

            cursor.execute("SELECT COALESCE(SUM(o_ol_cnt), 0) FROM oorder")
            expected_lines = int(cursor.fetchone()[0])
            if counts["order_line"] != expected_lines:
                errors.append("order_line count differs from SUM(oorder.o_ol_cnt)")
            if not 150_000 <= counts["order_line"] <= 450_000:
                errors.append("order_line count outside initial 5-15 lines per order")

            cursor.execute("SELECT COUNT(*) FROM district WHERE d_next_o_id <> 3001")
            if cursor.fetchone()[0] != 0:
                errors.append("Some districts do not start with d_next_o_id=3001")

            cursor.execute(
                "SELECT COUNT(*) FROM stock WHERE s_quantity < 10 OR s_quantity > 100"
            )
            if cursor.fetchone()[0] != 0:
                errors.append("Initial stock quantities are outside 10-100")

    return {
        "status": "FAIL" if errors else "PASS",
        "row_counts": counts,
        "total_rows": sum(counts.values()),
        "order_line_expected_from_orders": expected_lines,
        "errors": errors,
    }


def main():
    report = {"checked_at_utc": datetime.now(timezone.utc).isoformat()}
    failed = False
    for dbms in ("postgres", "mysql"):
        try:
            result = verify(dbms)
        except (psycopg.Error, mysql.connector.Error, ValueError) as error:
            result = {"status": "FAIL", "errors": [str(error)]}
        report[dbms] = result
        failed |= result["status"] != "PASS"
        print(f"\n{dbms}: {result['status']}")
        for table, count in result.get("row_counts", {}).items():
            print(f"  {table}: {count:,}")
        for error in result["errors"]:
            print(f"  ERROR: {error}")

    output = BASE / "logs" / "dataset-verification.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"\nVerification report: {output}")
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
