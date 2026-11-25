r"""E2: observed write blocking and short contention trials on TPC-C stock.

    .\.venv\Scripts\python.exe .\experiments\e2_contention.py

Keep stock rows (warehouse 1, item IDs 1-16) exclusive to this experiment.
These trials are preliminary; they are not the formal repeated benchmark.
"""

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
import csv
from datetime import datetime, timezone
import json
from queue import Queue
from statistics import fmean
from threading import Event
from time import perf_counter

from e1_mvcc import ROOT, Timeline, connect, isolation_cases, statement
from e2_support import (UPDATE_SQL, baseline, begin, call, mysql_observer_access,
                        percentile, restore, save_json, session, values, waits)


def demo(dbms, log, directory, isolation="READ COMMITTED"):
    directory.mkdir()
    result = dict(kind="demo", experiment="E2a", dbms=dbms, status="FAIL",
                  isolation=isolation, isolation_level=isolation, aborts=0, retries=0,
                  error_codes=[], cleanup_passed=False, lock_wait_observed=False)
    expect_serialization = dbms == "postgres" and isolation == "REPEATABLE READ"
    writer = observer = None
    original = None
    cancelled = Event()
    ready = Queue()
    state = {"committed": False, "retry_committed": False}
    pool = ThreadPoolExecutor(max_workers=1)
    future = None
    t1_committed = False
    start_sequence = len(log.events)

    def sql(connection, actor, text, params=None):
        return statement(connection, log, dbms, actor, text, params)

    def t2():
        connection = None
        announced = False
        try:
            connection = connect(dbms)
            pid = session(connection, dbms, log, "DEMO_T2", isolation)
            ready.put(pid)
            announced = True
            sql(connection, "DEMO_T2", "BEGIN" if dbms == "postgres" else "START TRANSACTION")
            count, latency = sql(connection, "DEMO_T2", UPDATE_SQL, (1,))
            if count != 1 or cancelled.is_set():
                raise RuntimeError("T2 cancelled or did not update exactly one row")
            log.record(dbms, "DEMO_T2", "T2_UPDATE_DONE", update_roundtrip_ms=latency)
            sql(connection, "DEMO_T2", "COMMIT")
            state["committed"] = True
            log.record(dbms, "DEMO_T2", "T2_COMMIT_DONE")
            return {"connection_id": pid, "update_roundtrip_ms": latency, "committed": True}
        except Exception as error:
            code = dict(sqlstate=getattr(error, "sqlstate", None), errno=getattr(error, "errno", None))
            result["error_codes"].append(code)
            result["aborts"] += int((code["sqlstate"] or "").startswith("40"))
            if not (expect_serialization and code["sqlstate"] == "40001"):
                raise
            log.record(dbms, "DEMO_T2", "T2_SERIALIZATION_FAILURE", message=str(error), **code)
            sql(connection, "DEMO_T2", "ROLLBACK")
            log.record(dbms, "DEMO_T2", "T2_ROLLBACK_DONE")
            return dict(connection_id=pid, committed=False, rollback_done=True, **code)
        finally:
            if not announced:
                ready.put(None)
            if connection is not None:
                try:
                    connection.rollback()
                finally:
                    connection.close()

    try:
        writer = connect(dbms)
        observer = connect(dbms)
        t1_pid = session(writer, dbms, log, "DEMO_T1", isolation)
        session(observer, dbms, log, "OBSERVER")
        original = baseline(writer, dbms, [1], directory)
        result["initial_value"] = str(original[1])
        sql(writer, "DEMO_T1", "BEGIN" if dbms == "postgres" else "START TRANSACTION")
        if sql(writer, "DEMO_T1", UPDATE_SQL, (1,))[0] != 1:
            raise RuntimeError("T1 did not update exactly one stock row")
        log.record(dbms, "DEMO_T1", "T1_UPDATE_UNCOMMITTED", baseline=original[1])
        future = pool.submit(t2)
        t2_pid = ready.get(timeout=5)
        if t2_pid is None:
            future.result()
        if t2_pid == t1_pid:
            raise RuntimeError("T1 and T2 must use separate sessions")

        first_seen = second_seen = None
        snapshots = []
        deadline = perf_counter() + 3.5
        while perf_counter() < deadline:
            edges = [edge for edge in waits(observer, dbms, [t2_pid])
                     if edge["blocking_pid"] == t1_pid]
            if edges:
                observed = perf_counter()
                if first_seen is None:
                    first_seen = observed
                    snapshots.append(edges)
                    log.record(dbms, "OBSERVER", "BLOCKING_OBSERVED", edges=edges)
                elif observed - first_seen >= 0.1:
                    second_seen = observed
                    snapshots.append(edges)
                    log.record(dbms, "OBSERVER", "BLOCKING_CONFIRMED", edges=edges)
                    break
            if future.done():
                future.result()
                raise RuntimeError("T2 completed before its lock wait was confirmed")
            # Poll spacing only; release depends on observed DBMS lock metadata.
            cancelled.wait(0.02)
        if second_seen is None or future.done():
            raise RuntimeError("Could not confirm T2 blocked by T1")
        result["lock_wait_observed"] = True
        log.record(dbms, "DEMO_T1", "T1_COMMIT_START")
        sql(writer, "DEMO_T1", "COMMIT")
        t1_committed = True
        log.record(dbms, "DEMO_T1", "T1_COMMIT_DONE")
        result.update(t1_connection_id=t1_pid, t2=future.result(timeout=10),
                      lock_snapshots=snapshots,
                      observed_blocked_span_ms=(second_seen - first_seen) * 1000)
        after_initial = values(observer, [1])[1]
        result["after_initial_value"] = str(after_initial)
        expected_initial = original[1] + (1 if expect_serialization else 2)
        if after_initial != expected_initial:
            raise RuntimeError("Initial pair left an unexpected counter value")
        log.record(dbms, "VERIFY", "INITIAL_VALUE_VERIFIED", value=after_initial)
        if expect_serialization:
            if result["aborts"] != 1 or result["t2"]["committed"] or not result["t2"].get("rollback_done"):
                raise RuntimeError("Expected one rolled-back concurrent-update attempt")
            retry_connection = connect(dbms)
            try:
                pid = session(retry_connection, dbms, log, "RETRY_T2", isolation)
                result["retries"] += 1
                result["retry_connection_id"] = pid
                log.record(dbms, "RETRY_T2", "RETRY_BEGIN", isolation_level=isolation)
                sql(retry_connection, "RETRY_T2", "BEGIN")
                if sql(retry_connection, "RETRY_T2", UPDATE_SQL, (1,))[0] != 1:
                    raise RuntimeError("Retry did not update exactly one stock row")
                sql(retry_connection, "RETRY_T2", "COMMIT")
                state["retry_committed"] = True
                log.record(dbms, "RETRY_T2", "RETRY_COMMIT_DONE")
            finally:
                try:
                    retry_connection.rollback()
                finally:
                    retry_connection.close()
        final = values(observer, [1])[1]
        result["final_value"] = str(final)
        if final != original[1] + 2:
            raise RuntimeError("Completed requests did not increment the counter twice")
        log.record(dbms, "VERIFY", "FINAL_VALUE_VERIFIED", value=final)
        result["status"] = "PASS"
    except BaseException as error:
        cancelled.set()
        result["error"] = str(error)
        log.record(dbms, "DEMO", "ERROR", message=str(error), sqlstate=getattr(error, "sqlstate", None))
    finally:
        if writer is not None:
            try:
                writer.rollback()  # Release T1's lock before joining T2 on failures.
            except Exception as error:
                result["rollback_error"] = str(error)
                result["status"] = "FAIL"
        pool.shutdown(wait=True)
        if future is not None:
            try:
                future.result()
            except BaseException as error:
                result["t2_error"] = str(error)
                result["status"] = "FAIL"
        if writer is not None:
            try:
                if original is not None:
                    commits = Counter({1: int(t1_committed) + int(state["committed"]) + int(state["retry_committed"])})
                    result["data_check"] = restore(writer, dbms, original, commits, log, "DEMO_CLEANUP")
                    result["cleanup_passed"] = result["data_check"]["restored"]
                    if result["status"] == "PASS" and commits[1] != 2:
                        raise RuntimeError("Demo did not commit exactly two updates")
            except BaseException as error:
                result["cleanup_error"] = str(error)
                result["status"] = "FAIL"
            finally:
                writer.close()
        if observer is not None:
            observer.close()

    stages = {event["action"]: event["sequence"] for event in log.events[start_sequence:]}
    ordered = ["T1_UPDATE_UNCOMMITTED", "BLOCKING_OBSERVED", "BLOCKING_CONFIRMED", "T1_COMMIT_START"]
    ordered += (["T2_SERIALIZATION_FAILURE", "T2_ROLLBACK_DONE", "INITIAL_VALUE_VERIFIED",
                 "RETRY_BEGIN", "RETRY_COMMIT_DONE"] if expect_serialization else
                ["T2_UPDATE_DONE", "T2_COMMIT_DONE", "INITIAL_VALUE_VERIFIED"])
    ordered += ["FINAL_VALUE_VERIFIED", "RESTORED"]
    # COMMIT acknowledgment can race with T2's completion; do not assert ACK order.
    result["schedule_verified"] = all(key in stages for key in ordered) and all(
        stages[a] < stages[b] for a, b in zip(ordered, ordered[1:]))
    if not result["schedule_verified"]:
        result["status"] = "FAIL"
    save_json(directory / "summary.json", result)
    log.record(dbms, "DEMO", result["status"], evidence=str(directory), isolation_level=isolation,
               aborts=result["aborts"], retries=result["retries"], cleanup_passed=result["cleanup_passed"])
    return result


def trial(dbms, scenario, clients, seconds, log, directory):
    directory.mkdir()
    items = list(range(1, clients + 1)) if scenario == "distinct" else list(range(1, 5)) if scenario == "shared4" else [1]
    result = dict(kind="short_trial", dbms=dbms, scenario=scenario, clients=clients,
                  target_items=items, requested_seconds=seconds, status="FAIL",
                  isolation="READ COMMITTED", retries=0, update_sql=UPDATE_SQL)
    controller = None
    original = None
    ready = Queue()
    start, stop = Event(), Event()
    clock = {}
    samples = []
    pool = ThreadPoolExecutor(max_workers=clients)
    futures = []
    workers = []
    case = f"{scenario}/{clients}"
    log.record(dbms, case, "TRIAL_START", seconds=seconds, target_items=items)

    def worker(client_id):
        connection = None
        announced = False
        output = {"client_id": client_id, "rows": [], "unknown_commit": False}
        try:
            connection = connect(dbms)
            pid = session(connection, dbms, log, f"CLIENT_{client_id}")
            output["connection_id"] = pid
            ready.put({"ok": True, "connection_id": pid})
            announced = True
            if not start.wait(30):
                raise RuntimeError("Timed out waiting for trial start")
            attempt = 0
            while not stop.is_set() and perf_counter() < clock["deadline"]:
                item = client_id + 1 if scenario == "distinct" else 1 + (client_id + attempt) % 4 if scenario == "shared4" else 1
                began = perf_counter()
                row = dict(client_id=client_id, item_id=item,
                           started_ms=(began - clock["started"]) * 1000,
                           status="OK", error_code="", sqlstate="", error="")
                stage = "BEGIN"
                try:
                    begin(connection, dbms)
                    stage = "UPDATE"
                    if call(connection, UPDATE_SQL, (item,))[0] != 1:
                        raise RuntimeError("UPDATE did not affect exactly one row")
                    stage = "COMMIT"
                    call(connection, "COMMIT")
                except Exception as error:
                    row.update(status="FAIL", error_code=getattr(error, "errno", ""),
                               sqlstate=getattr(error, "sqlstate", "") or "", error=str(error))
                    if stage == "COMMIT":
                        output["unknown_commit"] = True
                    connection.rollback()
                    retryable = row["sqlstate"] in ("40P01", "55P03", "57014") or row["error_code"] in (1205, 1213)
                    if not retryable or output["unknown_commit"]:
                        output["fatal_error"] = str(error)
                        stop.set()
                row["latency_ms"] = (perf_counter() - began) * 1000
                output["rows"].append(row)
                attempt += 1
        except BaseException as error:
            output["fatal_error"] = str(error)
            stop.set()
        finally:
            output["finished"] = perf_counter()
            if not announced:
                ready.put({"ok": False, "error": output.get("fatal_error", "Connection setup failed")})
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
        controller = connect(dbms)
        session(controller, dbms, log, "TRIAL_OBSERVER")
        original = baseline(controller, dbms, items, directory)
        futures = [pool.submit(worker, client_id) for client_id in range(clients)]
        pids = []
        for _ in range(clients):
            info = ready.get(timeout=15)
            if not info["ok"]:
                raise RuntimeError(info["error"])
            pids.append(info["connection_id"])
        if len(set(pids)) != clients:
            raise RuntimeError("Client sessions are not distinct")
        result["connection_ids"] = pids
        clock["started"] = perf_counter()
        clock["deadline"] = clock["started"] + seconds
        start.set()
        poll_delay = Event()
        max_blocked = 0
        while any(not future.done() for future in futures):
            edges = waits(controller, dbms, pids)
            now = perf_counter()
            blocked = sorted({edge["waiting_pid"] for edge in edges})
            samples.append(dict(elapsed_ms=(now - clock["started"]) * 1000,
                                blocked_sessions=len(blocked),
                                waiting_pids=";".join(map(str, blocked)),
                                blocking_pids=";".join(map(str, sorted({e["blocking_pid"] for e in edges}))),
                                edges_json=json.dumps(edges, default=str)))
            if len(blocked) > max_blocked:
                max_blocked = len(blocked)
                log.record(dbms, case, "WAIT_SAMPLE", blocked_sessions=max_blocked,
                           edge_count=len(edges), edge_examples=edges[:3])
            if now > clock["deadline"] + 15:
                raise RuntimeError("Trial exceeded its drain deadline")
            poll_delay.wait(0.05)
    except BaseException as error:
        stop.set()
        result["error"] = str(error) or type(error).__name__
        log.record(dbms, case, "ERROR", message=result["error"])
    finally:
        stop.set()
        start.set()
        pool.shutdown(wait=True)
        for future in futures:
            try:
                workers.append(future.result())
            except BaseException as error:
                result["error"] = str(error)
        rows = [row for output in workers for row in output["rows"]]
        successes = [row for row in rows if row["status"] == "OK"]
        successful_by_item = Counter(row["item_id"] for row in successes)
        # Restore before writing potentially large CSVs, including on interrupts.
        if controller is not None:
            try:
                if original is not None:
                    result["data_check"] = restore(controller, dbms, original, successful_by_item, log, case)
            except BaseException as error:
                result["cleanup_error"] = str(error)
            finally:
                controller.close()
        save_json(directory / "confirmed-commits.json", successful_by_item)
        if rows:
            with (directory / "transactions.csv").open("w", newline="", encoding="utf-8") as stream:
                writer_csv = csv.DictWriter(stream, fieldnames=list(rows[0]))
                writer_csv.writeheader()
                writer_csv.writerows(rows)
        if samples:
            with (directory / "lock-samples.csv").open("w", newline="", encoding="utf-8") as stream:
                writer_csv = csv.DictWriter(stream, fieldnames=list(samples[0]))
                writer_csv.writeheader()
                writer_csv.writerows(samples)

    latencies = [row["latency_ms"] for row in successes]
    elapsed = (max((output["finished"] for output in workers), default=clock["started"]) - clock["started"]) if "started" in clock else 0
    fatal = [output["fatal_error"] for output in workers if output.get("fatal_error")]
    result.update(successful_transactions=len(successes), failed_attempts=len(rows) - len(successes),
                  actual_seconds=elapsed, successful_tps=len(successes) / elapsed if elapsed > 0 else 0,
                  abort_rate=(len(rows) - len(successes)) / len(rows) if rows else None,
                  success_latency_ms=dict(mean=fmean(latencies) if latencies else None,
                      p50=percentile(latencies, 0.5), p95=percentile(latencies, 0.95), p99=percentile(latencies, 0.99)),
                  lock_observation=dict(requested_poll_interval_ms=50, samples=len(samples),
                      samples_with_waits=sum(s["blocked_sessions"] > 0 for s in samples),
                      max_observed_blocked_sessions=max((s["blocked_sessions"] for s in samples), default=0)),
                  fatal_errors=fatal,
                  unknown_commit=any(output["unknown_commit"] for output in workers))
    if ("error" not in result and "cleanup_error" not in result and not fatal
            and not result["unknown_commit"] and successes and result.get("data_check", {}).get("restored")):
        result["status"] = "PASS"
    save_json(directory / "summary.json", result)
    log.record(dbms, case, result["status"], successful_transactions=len(successes),
               successful_tps=round(result["successful_tps"], 3),
               p95_ms=result["success_latency_ms"]["p95"],
               max_observed_blocked_sessions=result["lock_observation"]["max_observed_blocked_sessions"])
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", choices=("postgres", "mysql", "both"), default="both")
    parser.add_argument("--mode", choices=("demo", "load", "all"), default="all")
    parser.add_argument("--demo-isolation", choices=("rc", "rr", "both"), default="rc",
                        help="E2a RC/RR for both DBMS; E2b stays at RC")
    parser.add_argument("--clients", type=int, nargs="+", default=[1, 4, 8, 16])
    parser.add_argument("--seconds", type=float, default=10)
    args = parser.parse_args()
    if args.mode == "load" and args.demo_isolation != "rc":
        parser.error("--demo-isolation applies only to demo/all, not E2b load")
    if not 0.1 <= args.seconds <= 60 or any(not 1 <= n <= 16 for n in args.clients):
        parser.error("Use 0.1-60 seconds and 1-16 clients for these preliminary trials")
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    directory = ROOT / "experiments" / "e2_logs" / run_id
    directory.mkdir(parents=True)
    log = Timeline(directory)
    report = dict(run_id=run_id, preliminary=True, warmup_seconds=0, repetitions=1,
                  results=[], status="FAIL")
    try:
        for dbms in (("postgres", "mysql") if args.db == "both" else (args.db,)):
            access = mysql_observer_access(log, directory) if dbms == "mysql" else nullcontext()
            with access:
                if args.mode in ("demo", "all"):
                    for isolation in isolation_cases(dbms, args.demo_isolation):
                        suffix = "rc" if isolation == "READ COMMITTED" else "rr"
                        result = demo(dbms, log, directory / f"{dbms}-demo-{suffix}", isolation)
                        report["results"].append(result)
                        if result["status"] != "PASS":
                            raise RuntimeError(f"{dbms}/{suffix} blocking demo failed")
                if args.mode in ("load", "all"):
                    for scenario in ("distinct", "shared4", "hot1"):
                        for clients in args.clients:
                            result = trial(dbms, scenario, clients, args.seconds, log,
                                           directory / f"{dbms}-{scenario}-{clients}")
                            report["results"].append(result)
                            if result["status"] != "PASS":
                                raise RuntimeError(f"{dbms}/{scenario}/{clients} failed; inspect its summary")
        report["status"] = "PASS"
    except BaseException as error:
        report["error"] = str(error)
        log.record("RUN", "MAIN", "ERROR", message=str(error))
    finally:
        save_json(directory / "summary.json", report)
        flat = []
        for result in report["results"]:
            if result["kind"] == "short_trial":
                flat.append({key: result[key] for key in ("dbms", "scenario", "clients", "status", "successful_transactions", "failed_attempts", "actual_seconds", "successful_tps")} | {
                    "p50_ms": result["success_latency_ms"]["p50"],
                    "p95_ms": result["success_latency_ms"]["p95"],
                    "p99_ms": result["success_latency_ms"]["p99"],
                    "max_observed_blocked_sessions": result["lock_observation"]["max_observed_blocked_sessions"],
                    "restored": result.get("data_check", {}).get("restored", False)})
        if flat:
            with (directory / "trials.csv").open("w", newline="", encoding="utf-8") as stream:
                writer_csv = csv.DictWriter(stream, fieldnames=list(flat[0]))
                writer_csv.writeheader()
                writer_csv.writerows(flat)
        log.close()
    print(f"\nE2 {report['status']}; evidence: {directory}")
    return int(report["status"] != "PASS")


if __name__ == "__main__":
    raise SystemExit(main())
