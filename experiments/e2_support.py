"""Database and evidence helpers for the E2 contention experiment."""

from contextlib import contextmanager
from decimal import Decimal
import json
from pathlib import Path
import re
import subprocess
from time import perf_counter

from e1_mvcc import ROOT, connect


UPDATE_SQL = "UPDATE stock SET s_ytd = s_ytd + 1 WHERE s_w_id = 1 AND s_i_id = %s"
MYSQL_OBSERVER_TABLES = ("data_lock_waits", "data_locks", "threads")


def call(connection, sql, params=None):
    cursor = connection.cursor()
    started = perf_counter()
    try:
        cursor.execute(sql, params)
        result = cursor.fetchall() if cursor.description else cursor.rowcount
        return result, (perf_counter() - started) * 1000
    finally:
        cursor.close()


def begin(connection, dbms):
    return call(connection, "BEGIN" if dbms == "postgres" else "START TRANSACTION")


def session(connection, dbms, log, actor):
    if dbms == "postgres":
        call(connection, "SET SESSION CHARACTERISTICS AS TRANSACTION ISOLATION LEVEL READ COMMITTED")
        call(connection, "SET statement_timeout = '8s'")
        call(connection, "SET lock_timeout = '5s'")
        row = call(connection, "SELECT pg_backend_pid(), current_setting('transaction_isolation'), version()")[0][0]
    else:
        call(connection, "SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED")
        call(connection, "SET SESSION innodb_lock_wait_timeout = 5")
        call(connection, "SET SESSION lock_wait_timeout = 5")
        call(connection, "SET SESSION max_execution_time = 5000")
        row = call(connection, "SELECT CONNECTION_ID(), @@session.transaction_isolation, VERSION()")[0][0]
    if row[1].lower().replace("-", " ") != "read committed":
        raise RuntimeError(f"Unexpected isolation: {row[1]}")
    log.record(dbms, actor, "SESSION", connection_id=row[0], isolation=row[1], version=row[2])
    return row[0]


def values(connection, items, lock=False):
    placeholders = ",".join("%s" for _ in items)
    sql = f"SELECT s_i_id, s_ytd FROM stock WHERE s_w_id = 1 AND s_i_id IN ({placeholders}) ORDER BY s_i_id"
    if lock:
        sql += " FOR UPDATE"
    rows = call(connection, sql, tuple(items))[0]
    if len(rows) != len(items):
        raise RuntimeError("Some target stock rows are missing")
    return {int(item): Decimal(str(value)) for item, value in rows}


def save_json(path, payload):
    Path(path).write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")


def baseline(connection, dbms, items, directory):
    begin(connection, dbms)
    try:
        original = values(connection, items, lock=True)
        call(connection, "COMMIT")
    except BaseException:
        connection.rollback()
        raise
    save_json(directory / "baseline.json", original)
    return original


def restore(connection, dbms, original, successful_by_item, log, actor):
    """Validate every delta under row locks before changing any value back."""
    connection.rollback()
    begin(connection, dbms)
    try:
        current = values(connection, sorted(original), lock=True)
        expected = {item: value + successful_by_item.get(item, 0) for item, value in original.items()}
        if current != expected:
            raise RuntimeError(
                f"Counter mismatch; restoration skipped to avoid overwriting unexpected data. "
                f"Actual={current}, expected={expected}"
            )
        for item, old in original.items():
            if current[item] != old:
                count = call(connection,
                    "UPDATE stock SET s_ytd = %s WHERE s_w_id = 1 AND s_i_id = %s AND s_ytd = %s",
                    (old, item, current[item]))[0]
                if count != 1:
                    raise RuntimeError(f"Conditional restoration failed for item {item}")
        call(connection, "COMMIT")
    except BaseException:
        connection.rollback()
        raise
    restored = values(connection, sorted(original))
    if restored != original:
        raise RuntimeError("Post-restoration check failed")
    result = {
        "counter_check": True, "restored": True,
        "before": {str(k): str(v) for k, v in original.items()},
        "after_transactions": {str(k): str(v) for k, v in current.items()},
        "after_restore": {str(k): str(v) for k, v in restored.items()},
        "confirmed_commits": sum(successful_by_item.values()),
    }
    log.record(dbms, actor, "RESTORED", confirmed_commits=result["confirmed_commits"], rows=len(original))
    return result


def waits(connection, dbms, pids):
    """Return live wait edges for this run's client sessions and stock table."""
    if not pids:
        return []
    if dbms == "postgres":
        rows = call(connection,
            "SELECT pid, pg_blocking_pids(pid), state, wait_event_type, wait_event "
            "FROM pg_stat_activity WHERE datname = current_database() "
            "AND pid = ANY(%s) AND wait_event_type = 'Lock'", (list(pids),))[0]
        return [dict(waiting_pid=pid, blocking_pid=blocker, state=state,
                     wait_event_type=kind, wait_event=event)
                for pid, blockers, state, kind, event in rows for blocker in blockers]
    placeholders = ",".join("%s" for _ in pids)
    rows = call(connection,
        "SELECT r.PROCESSLIST_ID, b.PROCESSLIST_ID, l.OBJECT_NAME, l.INDEX_NAME, "
        "l.LOCK_TYPE, l.LOCK_MODE, l.LOCK_STATUS, l.LOCK_DATA "
        "FROM performance_schema.data_lock_waits w "
        "JOIN performance_schema.threads r ON r.THREAD_ID = w.REQUESTING_THREAD_ID "
        "JOIN performance_schema.threads b ON b.THREAD_ID = w.BLOCKING_THREAD_ID "
        "JOIN performance_schema.data_locks l ON l.ENGINE = w.ENGINE "
        "AND l.ENGINE_LOCK_ID = w.REQUESTING_ENGINE_LOCK_ID "
        "WHERE l.OBJECT_SCHEMA = DATABASE() AND l.OBJECT_NAME = 'stock' "
        f"AND r.PROCESSLIST_ID IN ({placeholders})", tuple(pids))[0]
    keys = ("waiting_pid", "blocking_pid", "table", "index", "lock_type", "lock_mode", "lock_status", "lock_data")
    return [dict(zip(keys, row)) for row in rows]


def mysql_admin(sql):
    # Use the existing container environment. Never pass the root password in argv/logs.
    command = ["docker", "compose", "exec", "-T", "mysql", "sh", "-c",
               'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysql --user=root --batch --skip-column-names']
    completed = subprocess.run(command, cwd=ROOT, input=sql, text=True,
                               capture_output=True, timeout=20)
    if completed.returncode:
        raise RuntimeError(f"MySQL observation-permission setup failed: {completed.stderr.strip()}")


@contextmanager
def mysql_observer_access(log, directory):
    """Temporarily grant only missing SELECT privileges, and revoke in finally."""
    connection = connect("mysql")
    missing = []
    try:
        account = call(connection, "SELECT CURRENT_USER()")[0][0][0]
        user, host = account.rsplit("@", 1)
        if not all(re.fullmatch(r"[A-Za-z0-9_%.-]+", part) for part in (user, host)):
            raise RuntimeError("Unexpected account format for observation grants")
        for table in MYSQL_OBSERVER_TABLES:
            try:
                call(connection, f"SELECT * FROM performance_schema.{table} LIMIT 0")
            except Exception as error:
                if getattr(error, "errno", None) != 1142:
                    raise
                missing.append(table)
    finally:
        connection.close()
    target = f"'{user}'@'{host}'"
    state = {"account": account, "temporarily_added_select": missing, "revoked": []}
    state_file = directory / "mysql-observer-permissions.json"
    save_json(state_file, state)
    try:
        if missing:
            mysql_admin("\n".join(f"GRANT SELECT ON performance_schema.{t} TO {target};" for t in missing))
            log.record("mysql", "ADMIN", "TEMPORARY_OBSERVER_ACCESS", tables=missing)
        yield
    finally:
        cleanup_errors = []
        for table in missing:
            try:
                mysql_admin(f"REVOKE SELECT ON performance_schema.{table} FROM {target};")
                state["revoked"].append(table)
            except Exception as error:
                cleanup_errors.append(str(error))
        state["cleanup_errors"] = cleanup_errors
        save_json(state_file, state)
        log.record("mysql", "ADMIN", "OBSERVER_ACCESS_RESTORED", tables=state["revoked"], errors=cleanup_errors)
        if cleanup_errors:
            raise RuntimeError("Could not revoke temporary observer permissions; see permission journal")


def percentile(values_ms, q):
    if not values_ms:
        return None
    ordered = sorted(values_ms)
    position = (len(ordered) - 1) * q
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)
