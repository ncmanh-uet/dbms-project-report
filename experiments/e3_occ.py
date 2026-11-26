r"""E3: lost update -> version conflict -> optimistic retry.

    .\.venv\Scripts\python.exe .\experiments\e3_occ.py

Only a uniquely named, run-owned copy of stock rows is modified.
"""

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import csv
from datetime import datetime, timezone
from decimal import Decimal
from queue import Queue
from random import Random
from statistics import fmean
from threading import Event
from time import perf_counter
from uuid import uuid4

from e1_mvcc import ROOT, Timeline, connect, statement
from e2_support import begin, call, percentile, save_json, session


class Fixture:
    """Own one disposable table; never reset or write the source stock table."""
    def __init__(self, dbms, log, directory):
        self.dbms, self.log, self.directory = dbms, log, directory
        self.table = "e3_occ_" + uuid4().hex[:16]
        self.connection = None
        self.created = False
        self.original = None
        self.original_tables = None
        self.cleanup = {}

    def tables(self):
        schema = "'public'" if self.dbms == "postgres" else "DATABASE()"
        return [row[0] for row in call(self.connection,
            f"SELECT table_name FROM information_schema.tables WHERE table_schema={schema} "
            "AND table_type='BASE TABLE' ORDER BY table_name")[0]]

    def source(self):
        return {int(item): Decimal(str(value)) for item, value in call(self.connection,
            "SELECT s_i_id, s_ytd FROM stock WHERE s_w_id=1 AND s_i_id BETWEEN 1 AND 16 ORDER BY s_i_id")[0]}

    def open(self):
        self.connection = connect(self.dbms)
        session(self.connection, self.dbms, self.log, "FIXTURE")
        self.original_tables = self.tables()
        self.original = self.source()
        if len(self.original) != 16 or self.table in self.original_tables:
            raise RuntimeError("Unexpected source rows or fixture-name collision")
        save_json(self.directory / "source-before.json", dict(table_names=self.original_tables,
                  stock_s_ytd=self.original, demo_table=self.table))
        suffix = " ENGINE=InnoDB" if self.dbms == "mysql" else ""
        call(self.connection, f"CREATE TABLE {self.table} (s_w_id INT NOT NULL, s_i_id INT NOT NULL, "
             "counter_value DECIMAL(18,2) NOT NULL, version BIGINT NOT NULL, "
             f"PRIMARY KEY (s_w_id,s_i_id)){suffix}")
        self.created = True
        count = call(self.connection, f"INSERT INTO {self.table} "
            "(s_w_id,s_i_id,counter_value,version) SELECT s_w_id,s_i_id,s_ytd,1 FROM stock "
            "WHERE s_w_id=1 AND s_i_id BETWEEN 1 AND 16")[0]
        if count != 16:
            raise RuntimeError("Fixture did not copy exactly 16 source rows")
        self.log.record(self.dbms, "FIXTURE", "CREATED", table=self.table, rows=count)

    def read(self):
        return {int(item): (Decimal(str(value)), int(version)) for item, value, version in
                call(self.connection, f"SELECT s_i_id,counter_value,version FROM {self.table} "
                     "WHERE s_w_id=1 ORDER BY s_i_id")[0]}

    def reset(self):
        begin(self.connection, self.dbms)
        try:
            for item, value in self.original.items():
                call(self.connection, f"UPDATE {self.table} SET counter_value=%s,version=1 "
                     "WHERE s_w_id=1 AND s_i_id=%s", (value, item))
            call(self.connection, "COMMIT")
        except BaseException:
            self.connection.rollback()
            raise
        if self.read() != {item: (value, 1) for item, value in self.original.items()}:
            raise RuntimeError("Fixture reset check failed")

    def check(self, increments, versions=True):
        actual = self.read()
        expected = {item: (value + increments.get(item, 0),
                    1 + increments.get(item, 0) if versions else 1)
                    for item, value in self.original.items()}
        if actual != expected:
            raise RuntimeError(f"Counter/version check failed: actual={actual}, expected={expected}")
        return {str(item): dict(counter=str(value), version=version) for item, (value, version) in actual.items()}

    def close(self):
        if self.connection is None:
            return
        try:
            self.connection.rollback()
            if self.created:
                call(self.connection, f"DROP TABLE {self.table}")
                self.created = False
                self.cleanup["demo_table_dropped"] = True
            if self.original is not None and self.original_tables is not None:
                self.cleanup["source_unchanged"] = self.source() == self.original
                self.cleanup["original_table_set_restored"] = self.tables() == self.original_tables
                if not all(self.cleanup.values()):
                    raise RuntimeError("Source or table inventory differs after E3")
            self.log.record(self.dbms, "FIXTURE", "CLEANUP", **self.cleanup)
        finally:
            save_json(self.directory / "cleanup.json", self.cleanup)
            self.connection.close()


def sql_read(table):
    return f"SELECT counter_value,version FROM {table} WHERE s_w_id=1 AND s_i_id=%s"


def sql_cas(table):
    return f"UPDATE {table} SET counter_value=%s,version=version+1 WHERE s_w_id=1 AND s_i_id=%s AND version=%s"


def demo(fixture, phase, log, directory, *, isolation="READ COMMITTED"):
    directory.mkdir()
    fixture.reset()
    dbms, table = fixture.dbms, fixture.table
    result = dict(kind="demo", dbms=dbms, phase=phase, isolation=isolation, requested_operations=2, status="FAIL")
    connections = []
    completed = False
    first_sequence = len(log.events)

    def sql(connection, actor, query, params=None):
        return statement(connection, log, dbms, actor, query, params)[0]

    try:
        for actor in ("T1", "T2"):
            connection = connect(dbms)
            connections.append(connection)
            pid = session(connection, dbms, log, f"{phase}/{actor}", isolation=isolation)
            result[f"{actor.lower()}_connection_id"] = pid
            sql(connection, actor, "BEGIN" if dbms == "postgres" else "START TRANSACTION")
        t1, t2 = connections
        if result["t1_connection_id"] == result["t2_connection_id"]:
            raise RuntimeError("Two independent connections are required")
        old1, version1 = sql(t1, "T1", sql_read(table), (1,))[0]
        old2, version2 = sql(t2, "T2", sql_read(table), (1,))[0]
        old1, old2 = Decimal(str(old1)), Decimal(str(old2))
        if old1 != fixture.original[1] or old1 != old2 or version1 != 1 or version2 != 1:
            raise RuntimeError("Both transactions must read the same initial value/version")
        log.record(dbms, phase, "BOTH_READ_BASELINE", counter=old1, version=version1)

        if phase == "lost_update":
            unsafe_sql = f"UPDATE {table} SET counter_value=%s WHERE s_w_id=1 AND s_i_id=%s"
            sql(t1, "T1", unsafe_sql, (old1 + 1, 1))
            sql(t1, "T1", "COMMIT")
            log.record(dbms, phase, "T1_COMMIT")
            reject_stale_write = dbms == "postgres" and isolation == "REPEATABLE READ"
            try:
                result["t2_affected_rows"] = sql(t2, "T2", unsafe_sql, (old2 + 1, 1))
            except Exception as error:
                if not reject_stale_write or getattr(error, "sqlstate", None) != "40001":
                    raise
                result["t2_sqlstate"] = error.sqlstate
                sql(t2, "T2", "ROLLBACK")
                log.record(dbms, phase, "STALE_WRITE_REJECTED", sqlstate=error.sqlstate)
            else:
                if reject_stale_write:
                    raise RuntimeError("PostgreSQL RR accepted a stale-snapshot write")
                sql(t2, "T2", "COMMIT")
            observed = fixture.check(Counter({1: 1}), versions=False)
            if not reject_stale_write:
                log.record(dbms, phase, "LOST_UPDATE_CONFIRMED", observed=observed["1"], correct_counter=old1 + 2)
            commits = 1 if reject_stale_write else 2
            result.update(successful_operations_reported=commits, confirmed_commits=commits,
                          actual=observed["1"], correct_counter=str(old1 + 2),
                          lost_increments=commits - 1, expected_anomaly_observed=not reject_stale_write)
        else:
            if sql(t1, "T1", sql_cas(table), (old1 + 1, 1, version1)) != 1:
                raise RuntimeError("T1 CAS did not succeed")
            sql(t1, "T1", "COMMIT")
            log.record(dbms, phase, "T1_COMMIT")
            affected = sql(t2, "T2", sql_cas(table), (old2 + 1, 1, version2))
            if affected != 0:
                raise RuntimeError("The stale-version write was not rejected")
            sql(t2, "T2", "ROLLBACK")
            log.record(dbms, phase, "VERSION_CONFLICT", affected_rows=affected, stale_version=version2)
            successes, retries = 1, 0
            if phase == "occ_retry":
                sql(t2, "T2", "BEGIN" if dbms == "postgres" else "START TRANSACTION")
                fresh_value, fresh_version = sql(t2, "T2", sql_read(table), (1,))[0]
                fresh_value = Decimal(str(fresh_value))
                log.record(dbms, phase, "RETRY_READ", counter=fresh_value, version=fresh_version)
                if fresh_value != old1 + 1 or fresh_version != 2:
                    raise RuntimeError("Retry did not read the updated state")
                if sql(t2, "T2", sql_cas(table), (fresh_value + 1, 1, fresh_version)) != 1:
                    raise RuntimeError("The retry CAS did not succeed")
                sql(t2, "T2", "COMMIT")
                log.record(dbms, phase, "RETRY_COMMIT")
                successes, retries = 2, 1
            observed = fixture.check(Counter({1: successes}))
            result.update(successful_operations=successes, version_conflicts=1,
                          retry_attempts=retries, actual=observed["1"], integrity_ok=True)
        stages = {event["action"]: event["sequence"] for event in log.events[first_sequence:]}
        required = ["BOTH_READ_BASELINE", "T1_COMMIT"]
        if phase == "lost_update":
            required.append("STALE_WRITE_REJECTED" if reject_stale_write else "LOST_UPDATE_CONFIRMED")
        else:
            required.append("VERSION_CONFLICT")
        if phase == "occ_retry":
            required += ["RETRY_READ", "RETRY_COMMIT"]
        result["schedule_verified"] = all(key in stages for key in required) and all(
            stages[a] < stages[b] for a, b in zip(required, required[1:]))
        if not result["schedule_verified"]:
            raise RuntimeError("Demonstration schedule did not match the intended order")
        completed = True
    except BaseException as error:
        result["error"] = str(error) or type(error).__name__
        log.record(dbms, phase, "ERROR", message=result["error"], sqlstate=getattr(error, "sqlstate", None))
    finally:
        for connection in connections:
            try:
                connection.rollback()
            finally:
                connection.close()
        try:
            fixture.reset()
            result["fixture_reset"] = True
        except BaseException as error:
            result["cleanup_error"] = str(error) or type(error).__name__
            completed = False
        result["status"] = "PASS" if completed else "FAIL"
        save_json(directory / "summary.json", result)
        log.record(dbms, phase, result["status"], actual=result.get("actual"))
    return result


def write_csv(path, rows):
    if rows:
        with path.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)


def trial(fixture, scenario, clients, seconds, max_retries, log, directory):
    directory.mkdir()
    fixture.reset()
    dbms, table = fixture.dbms, fixture.table
    result = dict(kind="occ_trial", dbms=dbms, scenario=scenario, clients=clients,
                  requested_seconds=seconds, max_retries=max_retries, status="FAIL")
    ready, start, stop = Queue(), Event(), Event()
    clock = {}
    pool = ThreadPoolExecutor(max_workers=clients)
    futures, outputs = [], []
    case = f"{scenario}/{clients}"
    log.record(dbms, case, "TRIAL_START", seconds=seconds, max_retries=max_retries)

    def worker(client_id):
        connection = None
        announced = False
        output = dict(operations=[], attempts=[], unknown_commit=False)
        random = Random(20261109 + client_id)
        try:
            connection = connect(dbms)
            pid = session(connection, dbms, log, f"OCC_CLIENT_{client_id}")
            ready.put({"ok": True, "pid": pid})
            announced = True
            if not start.wait(30):
                raise RuntimeError("Timed out waiting for the run start")
            operation_id = 0
            while not stop.is_set() and perf_counter() < clock["deadline"]:
                item = client_id + 1 if scenario == "distinct" else 1
                began = perf_counter()
                operation = dict(client_id=client_id, operation_id=operation_id, item_id=item,
                    status="EXHAUSTED", attempts=0, conflicts=0, retries=0, latency_ms=0.0)
                for attempt_index in range(max_retries + 1):
                    if stop.is_set():
                        operation["status"] = "CANCELLED"
                        break
                    if attempt_index:
                        operation["retries"] += 1
                    attempted = perf_counter()
                    entry = dict(client_id=client_id, operation_id=operation_id, item_id=item,
                                 attempt=attempt_index + 1, expected_version="", value_read="",
                                 affected_rows="", status="ERROR", latency_ms=0.0,
                                 sqlstate="", error_code="", error="")
                    stage = "BEGIN"
                    try:
                        begin(connection, dbms)
                        old, version = call(connection, sql_read(table), (item,))[0][0]
                        old = Decimal(str(old))
                        entry.update(expected_version=version, value_read=str(old))
                        count = call(connection, sql_cas(table), (old + 1, item, version))[0]
                        entry["affected_rows"] = count
                        if count == 1:
                            stage = "COMMIT"
                            call(connection, "COMMIT")
                            entry["status"] = "COMMITTED"
                            operation["status"] = "OK"
                        elif count == 0:
                            connection.rollback()
                            entry["status"] = "CONFLICT"
                            operation["conflicts"] += 1
                        else:
                            raise RuntimeError("CAS updated an unexpected number of rows")
                    except Exception as error:
                        entry.update(error=str(error) or type(error).__name__,
                            sqlstate=getattr(error, "sqlstate", "") or "", error_code=getattr(error, "errno", ""))
                        output["unknown_commit"] |= stage == "COMMIT"
                        operation["status"] = "ERROR"
                        output["fatal_error"] = entry["error"]
                        stop.set()
                        connection.rollback()
                    entry["latency_ms"] = (perf_counter() - attempted) * 1000
                    output["attempts"].append(entry)
                    operation["attempts"] += 1
                    if entry["status"] != "CONFLICT":
                        break
                    if attempt_index < max_retries:
                        delay = min(0.001 * 2 ** attempt_index, 0.020) * random.uniform(0.5, 1.5)
                        stop.wait(delay)
                operation["latency_ms"] = (perf_counter() - began) * 1000
                output["operations"].append(operation)
                operation_id += 1
        except BaseException as error:
            output["fatal_error"] = str(error) or type(error).__name__
            stop.set()
        finally:
            output["finished"] = perf_counter()
            if not announced:
                ready.put({"ok": False, "error": output.get("fatal_error", "Session setup failed")})
            if connection is not None:
                try:
                    connection.rollback()
                except Exception as error:
                    output["fatal_error"] = str(error)
                try:
                    connection.close()
                except Exception as error:
                    output["fatal_error"] = str(error)
        return output

    try:
        futures = [pool.submit(worker, client_id) for client_id in range(clients)]
        pids = []
        for _ in range(clients):
            info = ready.get(timeout=15)
            if not info["ok"]:
                raise RuntimeError(info["error"])
            pids.append(info["pid"])
        if len(set(pids)) != clients:
            raise RuntimeError("Expected distinct client sessions")
        result["connection_ids"] = pids
        clock.update(started=perf_counter())
        clock["deadline"] = clock["started"] + seconds
        start.set()
        for future in futures:
            future.result(timeout=seconds + 30)
    except BaseException as error:
        stop.set()
        result["error"] = str(error) or type(error).__name__
    finally:
        stop.set()
        start.set()
        pool.shutdown(wait=True)
        for future in futures:
            try:
                outputs.append(future.result())
            except BaseException as error:
                result["error"] = str(error) or type(error).__name__
        operations = [row for output in outputs for row in output["operations"]]
        attempts = [row for output in outputs for row in output["attempts"]]
        successes = [row for row in operations if row["status"] == "OK"]
        increments = Counter(row["item_id"] for row in successes)
        try:
            result["final_rows"] = fixture.check(increments)
            result["integrity_ok"] = True
        except BaseException as error:
            result["integrity_error"] = str(error) or type(error).__name__
        finally:
            try:
                fixture.reset()
                result["fixture_reset"] = True
            except BaseException as error:
                result["reset_error"] = str(error) or type(error).__name__
        write_csv(directory / "operations.csv", operations)
        write_csv(directory / "attempts.csv", attempts)

    elapsed = (max((out["finished"] for out in outputs), default=clock["started"]) - clock["started"]) if "started" in clock else 0
    conflicts = sum(row["status"] == "CONFLICT" for row in attempts)
    retries = sum(row["retries"] for row in operations)
    latencies = [row["latency_ms"] for row in successes]
    fatal = [output["fatal_error"] for output in outputs if output.get("fatal_error")]
    result.update(logical_operations=len(operations), successful_operations=len(successes),
        exhausted_operations=sum(row["status"] == "EXHAUSTED" for row in operations),
        cancelled_operations=sum(row["status"] == "CANCELLED" for row in operations),
        attempts=len(attempts), version_conflicts=conflicts, retries_started=retries,
        conflict_rate=conflicts / len(attempts) if attempts else None,
        operations_with_retry=sum(row["retries"] > 0 for row in operations),
        retry_operation_rate=sum(row["retries"] > 0 for row in operations) / len(operations) if operations else None,
        retries_per_operation=retries / len(operations) if operations else None,
        db_error_operations=sum(row["status"] == "ERROR" for row in operations),
        actual_seconds=elapsed, successful_tps=len(successes) / elapsed if elapsed else 0,
        success_latency_ms=dict(mean=fmean(latencies) if latencies else None,
            p50=percentile(latencies, 0.5), p95=percentile(latencies, 0.95), p99=percentile(latencies, 0.99)),
        fatal_errors=fatal, unknown_commit=any(output["unknown_commit"] for output in outputs))
    if ("error" not in result and not fatal and not result["unknown_commit"] and successes
            and result.get("integrity_ok") and result.get("fixture_reset") and "reset_error" not in result):
        result["status"] = "PASS"
    save_json(directory / "summary.json", result)
    log.record(dbms, case, result["status"], successful_operations=len(successes),
               conflicts=conflicts, retries=retries, exhausted=result["exhausted_operations"])
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", choices=("postgres", "mysql", "both"), default="both")
    parser.add_argument("--mode", choices=("demo", "load", "all", "lost-update-rr"), default="all")
    parser.add_argument("--clients", type=int, nargs="+", default=[1, 4, 8, 16])
    parser.add_argument("--seconds", type=float, default=10)
    parser.add_argument("--max-retries", type=int, default=8)
    args = parser.parse_args()
    if not 0.1 <= args.seconds <= 60 or any(not 1 <= n <= 16 for n in args.clients) or not 0 <= args.max_retries <= 20:
        parser.error("Use 0.1-60 seconds, 1-16 clients, and 0-20 maximum retries")
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    directory = ROOT / "experiments" / "e3_logs" / run_id
    directory.mkdir(parents=True)
    log = Timeline(directory)
    isolation = "REPEATABLE READ" if args.mode == "lost-update-rr" else "READ COMMITTED"
    report = dict(run_id=run_id, preliminary=True, isolation=isolation,
                  warmup_seconds=0, repetitions=1, results=[], cleanup={}, status="FAIL")
    try:
        for dbms in (("postgres", "mysql") if args.db == "both" else (args.db,)):
            db_directory = directory / dbms
            db_directory.mkdir()
            fixture = Fixture(dbms, log, db_directory)
            try:
                fixture.open()
                if args.mode in ("demo", "all", "lost-update-rr"):
                    phases = ("lost_update",) if args.mode == "lost-update-rr" else ("lost_update", "occ_detect", "occ_retry")
                    for phase in phases:
                        result = demo(fixture, phase, log, db_directory / phase, isolation=isolation)
                        report["results"].append(result)
                        if result["status"] != "PASS":
                            raise RuntimeError(f"{dbms}/{phase} did not match the expected behavior")
                if args.mode in ("load", "all"):
                    for scenario in ("distinct", "hot1"):
                        for clients in args.clients:
                            result = trial(fixture, scenario, clients, args.seconds, args.max_retries,
                                           log, db_directory / f"{scenario}-{clients}")
                            report["results"].append(result)
                            if result["status"] != "PASS":
                                raise RuntimeError(f"{dbms}/{scenario}/{clients} failed validation")
            finally:
                fixture.close()
                report["cleanup"][dbms] = fixture.cleanup
        report["status"] = "PASS"
    except BaseException as error:
        report["error"] = str(error) or type(error).__name__
        log.record("RUN", "MAIN", "ERROR", message=report["error"])
    finally:
        save_json(directory / "summary.json", report)
        flat = []
        for result in report["results"]:
            if result["kind"] == "occ_trial":
                flat.append({key: result[key] for key in ("dbms", "scenario", "clients", "status",
                    "successful_operations", "exhausted_operations", "attempts", "version_conflicts",
                    "retries_started", "actual_seconds", "successful_tps")} | {
                    "p95_logical_ms": result["success_latency_ms"]["p95"], "integrity_ok": result.get("integrity_ok", False)})
        write_csv(directory / "trials.csv", flat)
        log.close()
    print(f"\nE3 {report['status']}; evidence: {directory}")
    return int(report["status"] != "PASS")


if __name__ == "__main__":
    raise SystemExit(main())
