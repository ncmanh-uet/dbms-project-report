r"""E4: opposite row order -> deadlock -> full retry; ordered rows -> prevention.

    .\.venv\Scripts\python.exe .\experiments\e4_deadlock.py

Keep stock rows (warehouse 1, items 1 and 2) exclusive to this experiment.
The script validates and restores both counters after each controlled pair.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor
import csv
from datetime import datetime, timezone
from queue import Queue
from threading import Barrier, Event
from time import perf_counter

from e1_mvcc import ROOT, Timeline, connect, statement
from e2_support import UPDATE_SQL, baseline, begin, call, save_json, session, values


ITEMS = [1, 2]
REQUESTS = {"T1": [1, 2], "T2": [2, 1]}


def error_kind(dbms, error):
    state, number = getattr(error, "sqlstate", None), getattr(error, "errno", None)
    if (dbms == "postgres" and state == "40P01") or (dbms == "mysql" and number == 1213):
        return "deadlock"
    if (dbms == "postgres" and state == "55P03") or (dbms == "mysql" and number == 1205):
        return "lock_timeout"
    if state == "57014" or number == 3024:
        return "statement_timeout"
    return "other_error"


def detector_settings(connection, dbms):
    if dbms == "postgres":
        amount, unit = call(connection,
            "SELECT setting,unit FROM pg_settings WHERE name='deadlock_timeout'")[0][0]
        if unit != "ms" or float(amount) >= 4000:
            raise RuntimeError("deadlock_timeout must be below the 5s lock-timeout guard for this demo")
        return dict(deadlock_timeout_ms=float(amount), session_lock_timeout_seconds=5)
    enabled = call(connection, "SELECT @@innodb_deadlock_detect")[0][0][0]
    if not enabled:
        raise RuntimeError("InnoDB deadlock detection is disabled; no data was changed")
    return dict(innodb_deadlock_detect=bool(enabled), session_lock_timeout_seconds=5)


def check_pair(connection, original, committed_transactions):
    actual = values(connection, ITEMS)
    expected = {item: value + committed_transactions for item, value in original.items()}
    if actual != expected:
        raise RuntimeError(f"Partial/unexpected update: actual={actual}, expected={expected}")
    return {str(item): str(value) for item, value in actual.items()}


def restore_pair(connection, dbms, original, confirmed_transactions, log, actor):
    """Each committed logical transaction increments TWO rows, not one."""
    connection.rollback()
    begin(connection, dbms)
    try:
        current = values(connection, ITEMS, lock=True)
        expected = {item: value + confirmed_transactions for item, value in original.items()}
        if current != expected:
            raise RuntimeError(f"Unexpected counter delta; restoration skipped: {current}, expected {expected}")
        for item, old in original.items():
            if current[item] != old:
                count = call(connection,
                    "UPDATE stock SET s_ytd=%s WHERE s_w_id=1 AND s_i_id=%s AND s_ytd=%s",
                    (old, item, current[item]))[0]
                if count != 1:
                    raise RuntimeError("Conditional restoration did not update one stock row")
        call(connection, "COMMIT")
    except BaseException:
        connection.rollback()
        raise
    if values(connection, ITEMS) != original:
        raise RuntimeError("Stock counters differ from baseline after restoration")
    log.record(dbms, actor, "RESTORED", confirmed_transactions=confirmed_transactions,
               confirmed_row_increments=2 * confirmed_transactions)
    return dict(counter_check=True, restored=True, confirmed_transactions=confirmed_transactions,
                before={str(k): str(v) for k, v in original.items()},
                after_transactions={str(k): str(v) for k, v in current.items()},
                after_restore={str(k): str(v) for k, v in original.items()})


def retry_victim(dbms, victim, log, case):
    """Replay the victim's entire business request, after both original sessions end."""
    connection = None
    outcome = dict(actor=victim, committed=False)
    started = perf_counter()
    try:
        connection = connect(dbms)
        outcome["connection_id"] = session(connection, dbms, log, f"{case}/RETRY_{victim}")
        log.record(dbms, case, "RETRY_BEGIN", victim=victim, order=REQUESTS[victim])
        statement(connection, log, dbms, f"RETRY_{victim}",
                  "BEGIN" if dbms == "postgres" else "START TRANSACTION")
        for item in REQUESTS[victim]:
            if statement(connection, log, dbms, f"RETRY_{victim}", UPDATE_SQL, (item,))[0] != 1:
                raise RuntimeError("Retry did not update one row")
        statement(connection, log, dbms, f"RETRY_{victim}", "COMMIT")
        outcome["committed"] = True
        log.record(dbms, case, "RETRY_COMMIT", victim=victim)
    except BaseException as error:
        outcome.update(error=str(error) or type(error).__name__, error_kind=error_kind(dbms, error),
                       sqlstate=getattr(error, "sqlstate", None), errno=getattr(error, "errno", None))
    finally:
        if connection is not None:
            try:
                connection.rollback()
            finally:
                connection.close()
    outcome["execution_ms"] = (perf_counter() - started) * 1000
    return outcome


def run_pair(dbms, mode, round_number, log, directory):
    directory.mkdir()
    case = f"{mode}/{round_number}"
    result = dict(dbms=dbms, mode=mode, round=round_number, isolation="READ COMMITTED",
                  isolation_level="READ COMMITTED", status="FAIL", aborts=0,
                  retries=0, error_codes=[], cleanup_passed=False)
    controller = None
    original = None
    ready, go, cancel = Queue(), Event(), Event()
    first_locks = Barrier(2, timeout=3)
    pool = ThreadPoolExecutor(max_workers=2)
    futures, outcomes = [], []
    retry = None
    sequence_start = len(log.events)
    pair_started = None
    log.record(dbms, case, "PAIR_START")

    def worker(actor):
        connection = None
        announced = False
        began = None
        request = REQUESTS[actor]
        order = request if mode == "opposite" else sorted(request)
        outcome = dict(actor=actor, request_order=request, lock_order=order,
                       committed=False, deadlock=False, rollback_done=False)
        try:
            connection = connect(dbms)
            outcome["connection_id"] = session(connection, dbms, log, f"{case}/{actor}")
            statement(connection, log, dbms, actor,
                      "BEGIN" if dbms == "postgres" else "START TRANSACTION")
            ready.put({"ok": True, "pid": outcome["connection_id"]})
            announced = True
            if not go.wait(10) or cancel.is_set():
                raise RuntimeError("Pair start cancelled or timed out")
            began = perf_counter()
            log.record(dbms, actor, "LOCK_ORDER", case=case, request_order=request, lock_order=order)
            for index, item in enumerate(order):
                log.record(dbms, actor, "REQUEST_ROW", case=case, item=item, position=index + 1)
                count, elapsed = statement(connection, log, dbms, actor, UPDATE_SQL, (item,))
                if count != 1:
                    raise RuntimeError("UPDATE did not affect exactly one stock row")
                outcome[f"update_{index + 1}_ms"] = elapsed
                if index == 0:
                    log.record(dbms, actor, "FIRST_ROW_LOCKED", case=case, item=item)
                    if mode == "opposite":
                        # Both different first rows must be held before second requests.
                        first_locks.wait()
                    # Ordered mode MUST NOT wait for both clients to hold their first row.
            if cancel.is_set():
                raise RuntimeError("Pair cancelled before commit")
            statement(connection, log, dbms, actor, "COMMIT")
            outcome["committed"] = True
            log.record(dbms, actor, "TRANSACTION_COMMIT", case=case)
        except BaseException as error:
            kind = error_kind(dbms, error)
            outcome.update(error=str(error) or type(error).__name__, error_kind=kind,
                           deadlock=kind == "deadlock", sqlstate=getattr(error, "sqlstate", None),
                           errno=getattr(error, "errno", None),
                           server_detail=getattr(getattr(error, "diag", None), "message_detail", None))
            log.record(dbms, actor, "TRANSACTION_ERROR", case=case, **{key:outcome[key] for key in
                       ("error", "error_kind", "sqlstate", "errno", "server_detail")})
            if kind != "deadlock":
                cancel.set()
                first_locks.abort()
            if connection is not None:
                try:
                    statement(connection, log, dbms, actor, "ROLLBACK")
                    outcome["rollback_done"] = True
                except Exception as rollback_error:
                    outcome["rollback_error"] = str(rollback_error)
        finally:
            if not announced:
                ready.put({"ok": False, "error": outcome.get("error", "Session setup failed")})
            outcome["execution_ms"] = (perf_counter() - began) * 1000 if began is not None else None
            if connection is not None:
                try:
                    connection.rollback()
                except Exception as error:
                    outcome["close_error"] = str(error)
                try:
                    connection.close()
                except Exception as error:
                    outcome["close_error"] = str(error)
        return outcome

    try:
        controller = connect(dbms)
        session(controller, dbms, log, "CONTROLLER")
        result["detector_settings"] = detector_settings(controller, dbms)
        original = baseline(controller, dbms, ITEMS, directory)
        futures = [pool.submit(worker, actor) for actor in REQUESTS]
        pids = []
        for _ in REQUESTS:
            info = ready.get(timeout=10)
            if not info["ok"]:
                raise RuntimeError(info["error"])
            pids.append(info["pid"])
        if len(set(pids)) != 2:
            raise RuntimeError("Two distinct sessions are required")
        pair_started = perf_counter()
        go.set()
        for future in futures:
            future.result(timeout=12)
    except BaseException as error:
        result["error"] = str(error) or type(error).__name__
        cancel.set()
    finally:
        cancel.set()
        go.set()
        first_locks.abort()
        pool.shutdown(wait=True)
        for future in futures:
            try:
                outcomes.append(future.result())
            except BaseException as error:
                result["error"] = str(error) or type(error).__name__

    result["initial_transactions"] = outcomes
    initial_commits = sum(outcome["committed"] for outcome in outcomes)
    deadlocks = sum(outcome["deadlock"] for outcome in outcomes)
    result.update(initial_commits=initial_commits, deadlocks=deadlocks,
                  lock_timeouts=sum(outcome.get("error_kind")=="lock_timeout" for outcome in outcomes))
    result["aborts"] = sum(bool(outcome.get("error_kind")) for outcome in outcomes)
    result["error_codes"] = [dict(sqlstate=outcome.get("sqlstate"), errno=outcome.get("errno"))
                             for outcome in outcomes if outcome.get("error_kind")]
    if pair_started is not None:
        result["initial_pair_ms"] = (perf_counter() - pair_started) * 1000
    try:
        if "error" in result:
            raise RuntimeError(result["error"])
        if any(outcome.get("close_error") or outcome.get("rollback_error") for outcome in outcomes):
            raise RuntimeError("A transaction did not close/rollback cleanly")
        if mode == "opposite":
            if len(outcomes) != 2 or deadlocks != 1 or initial_commits != 1:
                raise RuntimeError("Expected exactly one DBMS deadlock victim and one committed survivor")
            victim = next(outcome for outcome in outcomes if outcome["deadlock"])
            if not victim["rollback_done"]:
                raise RuntimeError("Victim rollback was not acknowledged")
            result["victim"] = victim["actor"]
            result["after_detection"] = check_pair(controller, original, 1)
            result["rollback_verified"] = True
            log.record(dbms, case, "VICTIM_ROLLBACK_VERIFIED", counters=result["after_detection"])
            events = log.events[sequence_start:]
            first = [event["sequence"] for event in events if event["action"]=="FIRST_ROW_LOCKED"]
            second = [event["sequence"] for event in events if event["action"]=="REQUEST_ROW" and event["position"]==2]
            result["schedule_verified"] = len(first)==2 and len(second)==2 and max(first) < min(second)
            if not result["schedule_verified"]:
                raise RuntimeError("The opposite-order schedule was not established")
            result["retries"] = 1
            result["retry_attempts"] = 1
            retry = retry_victim(dbms, victim["actor"], log, case)
            result["retry"] = retry
            if not retry["committed"]:
                raise RuntimeError("The full victim transaction did not complete on retry")
        else:
            if len(outcomes) != 2 or deadlocks or initial_commits != 2:
                raise RuntimeError("Ordered rows did not complete both transactions without deadlock")
            result["schedule_verified"] = all(outcome["lock_order"]==ITEMS for outcome in outcomes)
            if not result["schedule_verified"]:
                raise RuntimeError("Lock order was not consistent")
        result["after_completed_requests"] = check_pair(controller, original, 2)
        result["logical_successes"] = 2
        result["retry_attempts"] = 1 if retry is not None else 0
        result["status"] = "PASS"
    except BaseException as error:
        result["error"] = str(error) or type(error).__name__
        log.record(dbms, case, "VALIDATION_ERROR", message=result["error"])
    finally:
        if controller is not None:
            try:
                if original is not None:
                    confirmed = initial_commits + int(retry is not None and retry["committed"])
                    result["data_check"] = restore_pair(controller, dbms, original, confirmed, log, case)
                    result["cleanup_passed"] = result["data_check"]["restored"]
            except BaseException as error:
                result["cleanup_error"] = str(error) or type(error).__name__
                result["status"] = "FAIL"
            finally:
                controller.close()
        save_json(directory / "summary.json", result)
        log.record(dbms, case, result["status"], deadlocks=deadlocks, initial_commits=initial_commits,
                   retry_attempts=result.get("retry_attempts",0), restored=result.get("data_check",{}).get("restored",False),
                   isolation_level=result["isolation_level"], aborts=result["aborts"],
                   retries=result["retries"], error_codes=result["error_codes"], cleanup_passed=result["cleanup_passed"])
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", choices=("postgres","mysql","both"), default="both")
    parser.add_argument("--mode", choices=("opposite","ordered","both"), default="both")
    parser.add_argument("--rounds", type=int, default=5)
    args = parser.parse_args()
    if not 1 <= args.rounds <= 20:
        parser.error("Use 1-20 repetitions for this controlled demonstration")
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    directory = ROOT / "experiments" / "e4_logs" / run_id
    directory.mkdir(parents=True)
    log = Timeline(directory)
    report = dict(run_id=run_id, controlled_schedule=True, performance_benchmark=False,
                  rounds_per_mode=args.rounds, results=[], status="FAIL")
    try:
        for dbms in (("postgres","mysql") if args.db=="both" else (args.db,)):
            for mode in (("opposite","ordered") if args.mode=="both" else (args.mode,)):
                for round_number in range(1, args.rounds+1):
                    result = run_pair(dbms, mode, round_number, log, directory / f"{dbms}-{mode}-{round_number}")
                    report["results"].append(result)
                    if result["status"] != "PASS":
                        raise RuntimeError(f"{dbms}/{mode}/{round_number} failed validation")
        report["status"] = "PASS"
    except BaseException as error:
        report["error"] = str(error) or type(error).__name__
        log.record("RUN","MAIN","ERROR",message=report["error"])
    finally:
        save_json(directory / "summary.json", report)
        rows = []
        for result in report["results"]:
            rows.append(dict(dbms=result["dbms"], mode=result["mode"], round=result["round"],
                status=result["status"], deadlocks=result["deadlocks"], initial_commits=result["initial_commits"],
                lock_timeouts=result["lock_timeouts"], retries=result.get("retry_attempts",0),
                logical_successes=result.get("logical_successes",0), initial_pair_ms=result.get("initial_pair_ms"),
                restored=result.get("data_check",{}).get("restored",False)))
        if rows:
            with (directory / "rounds.csv").open("w", newline="", encoding="utf-8") as stream:
                writer = csv.DictWriter(stream,fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)
        log.close()
    print(f"\nE4 {report['status']}; evidence: {directory}")
    return int(report["status"] != "PASS")


if __name__ == "__main__":
    raise SystemExit(main())
