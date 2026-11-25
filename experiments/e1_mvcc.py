r"""E1: two connections, plain SELECT during an uncommitted UPDATE.

Run while no benchmark or other writer is using warehouse 1:
    .\.venv\Scripts\python.exe .\experiments\e1_mvcc.py
"""

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
from threading import Event, Lock
from time import perf_counter
from xml.etree import ElementTree

import mysql.connector
import psycopg


ROOT = Path(__file__).resolve().parents[1]
WAIT_SECONDS = 15
WAREHOUSE_ID = 1
DELTA = Decimal("100.00")
ISOLATIONS = {"rc": "READ COMMITTED", "rr": "REPEATABLE READ"}


def isolation_cases(dbms, choice):
    """Select explicit session isolation levels for either supported DBMS."""
    return list(ISOLATIONS.values()) if choice == "both" else [ISOLATIONS[choice]]


class Timeline:
    def __init__(self, directory):
        self.events = []
        self.lock = Lock()
        self.started = perf_counter()
        self.text_file = (directory / "timeline.log").open("w", encoding="utf-8")
        self.json_file = (directory / "timeline.jsonl").open("w", encoding="utf-8")

    def record(self, dbms, actor, action, **details):
        with self.lock:
            event = {
                "sequence": len(self.events) + 1,
                "utc": datetime.now(timezone.utc).isoformat(),
                "elapsed_ms": round((perf_counter() - self.started) * 1000, 3),
                "dbms": dbms, "actor": actor, "action": action, **details,
            }
            self.events.append(event)
            payload = json.dumps(details, ensure_ascii=False, default=str)
            line = f"{event['sequence']:03} [{dbms}/{actor}] {action} {payload}"
            print(line, flush=True)
            self.text_file.write(line + "\n")
            self.text_file.flush()
            self.json_file.write(json.dumps(event, default=str) + "\n")
            self.json_file.flush()

    def close(self):
        self.text_file.close()
        self.json_file.close()


def connect(dbms):
    config = ElementTree.parse(
        ROOT / "benchbase" / "config" / f"tpcc-{dbms}.xml"
    ).getroot()
    options = dict(
        host="127.0.0.1", user=config.findtext("username"),
        password=config.findtext("password"), autocommit=True,
    )
    if dbms == "postgres":
        return psycopg.connect(**options, port=5432, dbname="tpcc", connect_timeout=5)
    return mysql.connector.connect(
        **options, port=3307, database="tpcc", connection_timeout=5,
        read_timeout=10, write_timeout=10,
    )


def statement(connection, timeline, dbms, actor, sql, params=None):
    timeline.record(dbms, actor, "SQL_START", sql=sql, params=params)
    started = perf_counter()
    cursor = connection.cursor()
    try:
        cursor.execute(sql, params)
        result = cursor.fetchall() if cursor.description else cursor.rowcount
    finally:
        cursor.close()
    elapsed = round((perf_counter() - started) * 1000, 3)
    timeline.record(dbms, actor, "SQL_DONE", result=result, duration_ms=elapsed)
    return result, elapsed


def setup(connection, timeline, dbms, actor, isolation="READ COMMITTED"):
    if isolation not in ISOLATIONS.values():
        raise ValueError(f"Unsupported isolation: {isolation}")
    def sql(text):
        return statement(connection, timeline, dbms, actor, text)[0]

    if dbms == "postgres":
        sql(f"SET SESSION CHARACTERISTICS AS TRANSACTION ISOLATION LEVEL {isolation}")
        sql("SET statement_timeout = '5s'")
        sql("SET lock_timeout = '5s'")
        row = sql("SELECT pg_backend_pid(), current_setting('transaction_isolation'), version()")[0]
    else:
        sql(f"SET SESSION TRANSACTION ISOLATION LEVEL {isolation}")
        sql("SET SESSION max_execution_time = 5000")
        sql("SET SESSION innodb_lock_wait_timeout = 5")
        sql("SET SESSION lock_wait_timeout = 5")
        row = sql("SELECT CONNECTION_ID(), @@session.transaction_isolation, VERSION()")[0]
    if row[1].lower().replace("-", " ") != isolation.lower():
        raise RuntimeError(f"Unexpected isolation level: {row[1]}")
    timeline.record(dbms, actor, "SESSION", connection_id=row[0], isolation=row[1], version=row[2])
    return row[0]


def run_one(dbms, timeline, isolation="READ COMMITTED"):
    first_read_done, committed, failed = Event(), Event(), Event()
    start_sequence = len(timeline.events)
    report = dict(dbms=dbms, isolation=isolation, isolation_level=isolation,
                  warehouse_id=WAREHOUSE_ID, aborts=0, retries=0,
                  error_codes=[], cleanup_passed=False)
    timeline.record(dbms, "CASE", "CASE_START", isolation_level=isolation)
    writer = None
    baseline = None
    updated = False
    reader_future = None
    pool = ThreadPoolExecutor(max_workers=1)

    def sql(connection, actor, text, params=None):
        return statement(connection, timeline, dbms, actor, text, params)

    def reader():
        connection = None
        try:
            connection = connect(dbms)
            connection_id = setup(connection, timeline, dbms, "T2", isolation)
            begin = "BEGIN READ ONLY" if dbms == "postgres" else "START TRANSACTION READ ONLY"
            sql(connection, "T2", begin)
            rows, before_ms = sql(connection, "T2", "SELECT w_ytd FROM warehouse WHERE w_id = %s", (WAREHOUSE_ID,))
            before = Decimal(str(rows[0][0]))
            if before != baseline:
                raise RuntimeError(f"T2 read {before} before commit; expected {baseline}")
            timeline.record(dbms, "T2", "READ_BEFORE_COMMIT", value=before)
            first_read_done.set()

            if not committed.wait(WAIT_SECONDS) or failed.is_set():
                raise RuntimeError("T2 did not receive a successful T1 commit")
            rows, after_ms = sql(connection, "T2", "SELECT w_ytd FROM warehouse WHERE w_id = %s", (WAREHOUSE_ID,))
            after = Decimal(str(rows[0][0]))
            expected = baseline + DELTA if isolation == "READ COMMITTED" else baseline
            if after != expected:
                raise RuntimeError(f"T2 read {after} after commit; expected {expected}")
            timeline.record(dbms, "T2", "READ_AFTER_COMMIT", value=after)
            sql(connection, "T2", "COMMIT")
            return dict(connection_id=connection_id, before=str(before), after=str(after),
                        before_select_ms=before_ms, after_select_ms=after_ms)
        except Exception as error:
            failed.set()
            code = getattr(error, "sqlstate", None)
            if code:
                report["error_codes"].append(dict(actor="T2", sqlstate=code, errno=getattr(error, "errno", None)))
                report["aborts"] += int(code.startswith("40"))
            timeline.record(dbms, "T2", "ERROR", message=str(error),
                            sqlstate=getattr(error, "sqlstate", None))
            raise
        finally:
            if connection is not None:
                try:
                    connection.rollback()
                finally:
                    connection.close()
            first_read_done.set()  # Release T1 on errors as well.

    try:
        writer = connect(dbms)
        report["writer_connection_id"] = setup(writer, timeline, dbms, "T1", isolation)
        sql(writer, "T1", "BEGIN" if dbms == "postgres" else "START TRANSACTION")
        rows, _ = sql(writer, "T1", "SELECT w_ytd FROM warehouse WHERE w_id = %s FOR UPDATE", (WAREHOUSE_ID,))
        if len(rows) != 1:
            raise RuntimeError("Warehouse 1 does not exist")
        baseline = Decimal(str(rows[0][0]))
        report["baseline"] = str(baseline)
        count, _ = sql(writer, "T1", "UPDATE warehouse SET w_ytd = w_ytd + %s WHERE w_id = %s", (DELTA, WAREHOUSE_ID))
        if count != 1:
            raise RuntimeError("T1 did not update exactly one warehouse")
        updated = True
        timeline.record(dbms, "T1", "UPDATE_UNCOMMITTED", old=baseline, new=baseline + DELTA)

        # Launch T2 only after the UPDATE completes, while T1 still holds its lock.
        reader_future = pool.submit(reader)
        if not first_read_done.wait(WAIT_SECONDS):
            raise RuntimeError("T2 did not finish its first SELECT while T1 remained uncommitted")
        if failed.is_set():
            reader_future.result(timeout=WAIT_SECONDS)
            raise RuntimeError("T2 failed before T1 commit")
        timeline.record(dbms, "T1", "COMMIT_START")
        sql(writer, "T1", "COMMIT")
        timeline.record(dbms, "T1", "COMMIT_DONE")
        committed.set()
        report["reader"] = reader_future.result(timeout=WAIT_SECONDS)
        if report["writer_connection_id"] == report["reader"]["connection_id"]:
            raise RuntimeError("T1 and T2 must use distinct sessions")
        # T1 and T2 have ended: this explicit transaction gets a fresh snapshot.
        sql(writer, "VERIFY", "BEGIN" if dbms == "postgres" else "START TRANSACTION")
        rows, _ = sql(writer, "VERIFY", "SELECT w_ytd FROM warehouse WHERE w_id = %s", (WAREHOUSE_ID,))
        report.update(t2_read_1=report["reader"]["before"], t2_read_2=report["reader"]["after"],
                      final_db_value=str(rows[0][0]))
        if Decimal(str(rows[0][0])) != baseline + DELTA:
            raise RuntimeError("Fresh transaction did not see the committed warehouse value")
        sql(writer, "VERIFY", "COMMIT")
        timeline.record(dbms, "VERIFY", "FINAL_VALUE_VERIFIED", value=rows[0][0])
        report["status"] = "PASS"
    except Exception as error:
        code = getattr(error, "sqlstate", None)
        if code and not failed.is_set():
            report["error_codes"].append(dict(actor="T1", sqlstate=code, errno=getattr(error, "errno", None)))
            report["aborts"] += int(code.startswith("40"))
        failed.set()
        report["status"] = "FAIL"
        report["error"] = str(error)
        timeline.record(dbms, "T1", "ERROR", message=str(error),
                        sqlstate=getattr(error, "sqlstate", None))
    finally:
        # On failure, release T1's lock before waiting for the reader to stop.
        if writer is not None:
            try:
                writer.rollback()
            except Exception as error:
                report["rollback_error"] = str(error)
        committed.set()
        pool.shutdown(wait=True)
        if writer is not None:
            try:
                if baseline is not None and updated:
                    sql(writer, "CLEANUP", "BEGIN" if dbms == "postgres" else "START TRANSACTION")
                    rows, _ = sql(writer, "CLEANUP", "SELECT w_ytd FROM warehouse WHERE w_id = %s FOR UPDATE", (WAREHOUSE_ID,))
                    current = Decimal(str(rows[0][0]))
                    if current == baseline + DELTA:
                        count, _ = sql(writer, "CLEANUP", "UPDATE warehouse SET w_ytd = %s WHERE w_id = %s AND w_ytd = %s", (baseline, WAREHOUSE_ID, baseline + DELTA))
                        if count != 1:
                            raise RuntimeError("Conditional restoration did not update one row")
                    elif current != baseline:
                        raise RuntimeError("Unexpected external change; restoration skipped")
                    sql(writer, "CLEANUP", "COMMIT")
                    rows, _ = sql(writer, "CLEANUP", "SELECT w_ytd FROM warehouse WHERE w_id = %s", (WAREHOUSE_ID,))
                    report["restored_value"] = str(rows[0][0])
                    report["restored"] = Decimal(str(rows[0][0])) == baseline
                    report["cleanup_passed"] = report["restored"]
                    if not report["restored"]:
                        raise RuntimeError("Final value differs from the original baseline")
                    timeline.record(dbms, "CLEANUP", "RESTORED", value=rows[0][0])
            except Exception as error:
                report["status"] = "FAIL"
                report["cleanup_error"] = str(error)
                timeline.record(dbms, "CLEANUP", "ERROR", message=str(error))
            finally:
                try:
                    writer.rollback()
                finally:
                    writer.close()

    events = timeline.events[start_sequence:]
    stages = {event["action"]: event["sequence"] for event in events}
    required = ["UPDATE_UNCOMMITTED", "READ_BEFORE_COMMIT", "COMMIT_START", "COMMIT_DONE", "READ_AFTER_COMMIT", "FINAL_VALUE_VERIFIED", "RESTORED"]
    report["schedule_verified"] = all(name in stages for name in required) and all(
        stages[left] < stages[right] for left, right in zip(required, required[1:])
    )
    if not report["schedule_verified"]:
        report["status"] = "FAIL"
    timeline.record(dbms, "RESULT", report["status"], summary=report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", choices=("postgres", "mysql", "both"), default="both")
    parser.add_argument("--isolation", choices=("rc", "rr", "both"), default="rc",
                        help="RC/RR for PostgreSQL and MySQL")
    args = parser.parse_args()
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    directory = ROOT / "experiments" / "e1_logs" / run_id
    directory.mkdir(parents=True)
    timeline = Timeline(directory)
    reports = []
    try:
        for dbms in (("postgres", "mysql") if args.db == "both" else (args.db,)):
            for isolation in isolation_cases(dbms, args.isolation):
                report = run_one(dbms, timeline, isolation)
                reports.append(report)
                if report["status"] != "PASS":
                    break
            if reports[-1]["status"] != "PASS":
                break
    finally:
        (directory / "summary.json").write_text(
            json.dumps({"run_id": run_id, "results": reports}, indent=2) + "\n",
            encoding="utf-8",
        )
        timeline.close()
    print(f"\nEvidence saved to: {directory}")
    return int(not reports or any(report["status"] != "PASS" for report in reports))


if __name__ == "__main__":
    raise SystemExit(main())
