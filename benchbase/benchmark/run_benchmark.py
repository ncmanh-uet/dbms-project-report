"""Isolated TPC-C-like benchmark: setup, short smoke, or explicit formal run.

Only the owned tpcc_bench database can be dropped. tpcc is read-only input.
Use the project venv. See README.md for scope, metrics, and limitations.
"""
import argparse
from collections import Counter
from contextlib import closing, nullcontext
import csv
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import time
from xml.etree import ElementTree as ET

import mysql.connector
import psycopg

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "experiments"))
from e1_mvcc import Timeline
from e2_support import call, mysql_observer_access

TARGET = "tpcc_bench"
DBMS = ("postgres", "mysql")
COMPOSE = ["docker", "compose", "-f", str(ROOT / "compose.yaml"),
           "-f", str(HERE / "compose.override.yaml")]
TABLES = ("warehouse", "district", "customer", "history", "item", "stock", "oorder", "new_order", "order_line")
PROTOCOL = json.loads((HERE / "protocol.json").read_text(encoding="utf-8"))
ISOLATIONS = {"rc": "TRANSACTION_READ_COMMITTED", "rr": "TRANSACTION_REPEATABLE_READ"}


def save(path, value):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(value, indent=2, default=str) + "\n", encoding="utf-8")


def command(args, *, sql=None, timeout=120):
    result = subprocess.run(args, cwd=ROOT, input=sql, capture_output=True,
                            text=True, encoding="utf-8", errors="replace", timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"Command failed ({result.returncode}): {' '.join(args[:9])}\n{result.stderr[-5000:]}")
    return result.stdout.strip()


def dc(*args, **kwargs):
    return command(COMPOSE + list(args), **kwargs)


def connect(dbms, database=TARGET, observer=False):
    config = ET.parse(ROOT / "benchbase/config" / f"tpcc-{dbms}.xml").getroot()
    options = dict(host="127.0.0.1", user=config.findtext("username"),
                   password=config.findtext("password"), autocommit=True)
    if dbms == "postgres":
        return psycopg.connect(**options, port=5432, dbname=database, connect_timeout=5,
                               application_name="lab_observer" if observer else "lab_setup",
                               options="-c statement_timeout=15000 -c lock_timeout=5000")
    return mysql.connector.connect(**options, port=3307, database=database,
                                    connection_timeout=5, read_timeout=20, write_timeout=20)


def admin(dbms, sql):
    if dbms == "postgres":
        return dc("exec", "-T", "postgres", "psql", "-X", "-v", "ON_ERROR_STOP=1",
                  "-U", "labuser", "-d", "postgres", "-At", sql=sql)
    return dc("exec", "-T", "mysql", "sh", "-c",
              'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysql --user=root --batch --skip-column-names', sql=sql)


def ready(dbms):
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        try:
            with closing(connect(dbms, "tpcc")) as connection:
                call(connection, "SELECT 1")
            return
        except (psycopg.Error, mysql.connector.Error):
            time.sleep(1)
    raise RuntimeError(f"{dbms} did not become ready within 90s")


def disk_guard():
    free = shutil.disk_usage(HERE).free
    if free < PROTOCOL["minimum_free_disk_gib"] * 2**30:
        raise RuntimeError(f"Only {free / 2**30:.2f} GiB free; at least 2 GiB required")
    return free


def fingerprint(dbms, database):
    # Counts plus numeric aggregates, not a complete TPC-C consistency audit.
    with closing(connect(dbms, database)) as connection:
        result = {"counts": {table: int(call(connection, f"SELECT COUNT(*) FROM {table}")[0][0][0]) for table in TABLES}}
        for name, query in {
            "warehouse": "SELECT SUM(w_ytd), SUM(w_tax) FROM warehouse",
            "district": "SELECT SUM(d_ytd), SUM(d_next_o_id), SUM(d_tax) FROM district",
            "stock": "SELECT SUM(s_quantity), SUM(s_ytd), SUM(s_order_cnt), SUM(s_remote_cnt) FROM stock",
            "customer": "SELECT SUM(c_balance), SUM(c_ytd_payment), SUM(c_payment_cnt), SUM(c_delivery_cnt) FROM customer",
            "orders": "SELECT SUM(o_ol_cnt), SUM(o_id), SUM(o_c_id) FROM oorder",
            "order_lines": "SELECT SUM(ol_amount), SUM(ol_quantity) FROM order_line",
        }.items():
            result[name] = [str(value) for value in call(connection, query)[0][0]]
        return result


def sha(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def baseline_path(dbms):
    return HERE / "baseline" / ("postgres.dump" if dbms == "postgres" else "mysql.sql")


def ownership(dbms):
    path = HERE / "baseline" / f"{dbms}-ownership.json"
    if not path.exists():
        raise RuntimeError(f"No ownership record for {dbms}; run setup first")
    record = json.loads(path.read_text(encoding="utf-8"))
    if record["database"] != TARGET or record["service"] != dbms:
        raise RuntimeError("Ownership record does not match the fixed benchmark target")
    actual_volumes = json.loads(command(["docker", "inspect", dc("ps", "-q", dbms),
                                         "--format", "{{json .Mounts}}"]))
    names = sorted(m["Name"] for m in actual_volumes if m["Type"] == "volume")
    if names != record["volumes"]:
        raise RuntimeError("Database volume changed; refusing to reset an unowned target")
    return record


def restore(dbms):
    disk_guard()
    record = ownership(dbms)
    if sha(baseline_path(dbms)) != record["archive_sha256"]:
        raise RuntimeError("Baseline archive changed; refusing restore")
    if dbms == "postgres":
        admin(dbms, "DROP DATABASE IF EXISTS tpcc_bench WITH (FORCE);\nCREATE DATABASE tpcc_bench OWNER labuser;")
        dc("exec", "-T", dbms, "pg_restore", "-U", "labuser", "--no-owner", "--no-privileges",
           "--exit-on-error", "-d", TARGET, "/lab-bench/baseline/postgres.dump", timeout=240)
        with closing(connect(dbms)) as connection:
            call(connection, "ANALYZE")
    else:
        admin(dbms, "DROP DATABASE IF EXISTS tpcc_bench;\nCREATE DATABASE tpcc_bench;\n"
              "GRANT ALL PRIVILEGES ON tpcc_bench.* TO 'labuser'@'%';")
        # Disable binary logging only for the restore session; workload durability stays unchanged.
        dc("exec", "-T", dbms, "sh", "-c",
           'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysql --user=root --init-command="SET SESSION sql_log_bin=0" tpcc_bench < /lab-bench/baseline/mysql.sql', timeout=240)
        with closing(connect(dbms)) as connection:
            for table in TABLES:
                call(connection, f"ANALYZE TABLE {table}")
    observed = fingerprint(dbms, TARGET)
    if observed != record["fingerprint"]:
        raise RuntimeError(f"{dbms} restoration verification failed")
    print(f"{dbms}: baseline restored and verified", flush=True)
    return observed


def setup():
    disk_guard()
    for dbms in DBMS:
        (HERE / "results" / dbms).mkdir(parents=True, exist_ok=True)
    (HERE / "baseline").mkdir(exist_ok=True)
    dc("up", "-d", "postgres", "mysql")
    for dbms in DBMS:
        ready(dbms)
        marker = HERE / "baseline" / f"{dbms}-ownership.json"
        if marker.exists():
            restore(dbms)
            continue
        exists_sql = ("SELECT datname FROM pg_database WHERE datname='tpcc_bench';" if dbms == "postgres" else
                      "SELECT SCHEMA_NAME FROM information_schema.SCHEMATA WHERE SCHEMA_NAME='tpcc_bench';")
        if admin(dbms, exists_sql):
            raise RuntimeError(f"{dbms}: tpcc_bench already exists without an ownership record; not overwriting")
        before = fingerprint(dbms, "tpcc")
        if before["counts"] != dict(zip(TABLES, [1, 10, 30000, 30000, 100000, 100000, 30000, 9000, 300040])):
            raise RuntimeError("Source dataset is not the expected one-warehouse baseline")
        if dbms == "postgres":
            dc("exec", "-T", dbms, "pg_dump", "-U", "labuser", "-d", "tpcc", "-Fc",
               "-f", "/lab-bench/baseline/postgres.dump", timeout=240)
        else:
            dc("exec", "-T", dbms, "sh", "-c",
               'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysqldump --user=root --single-transaction --no-tablespaces --set-gtid-purged=OFF --column-statistics=0 --hex-blob --skip-comments tpcc > /lab-bench/baseline/mysql.sql', timeout=240)
            with baseline_path(dbms).open(encoding="utf-8") as stream:
                for line in stream:
                    if re.match(r"\s*(USE\s|(?:CREATE|DROP)\s+DATABASE\b)", line, re.I):
                        raise RuntimeError("MySQL archive unexpectedly changes database scope")
        if fingerprint(dbms, "tpcc") != before:
            raise RuntimeError("Source changed while preparing baseline; stop other workloads")
        mounts = json.loads(command(["docker", "inspect", dc("ps", "-q", dbms), "--format", "{{json .Mounts}}"]))
        save(marker, {"database": TARGET, "service": dbms, "created_utc": datetime.now(timezone.utc).isoformat(),
                      "volumes": sorted(m["Name"] for m in mounts if m["Type"] == "volume"),
                      "fingerprint": before, "archive_sha256": sha(baseline_path(dbms)),
                      "archive_bytes": baseline_path(dbms).stat().st_size})
        restore(dbms)
    save(HERE / "environment.json", environment())


def environment(runtime=HERE / "runtime"):
    docker_info = json.loads(command(["docker", "info", "--format", "{{json .}} "]))
    host = json.loads(command(["powershell.exe", "-NoProfile", "-Command",
        "$cpu=Get-CimInstance Win32_Processor; $os=Get-CimInstance Win32_OperatingSystem; "
        "[ordered]@{cpu=$cpu.Name;logical_processors=$cpu.NumberOfLogicalProcessors;"
        "physical_memory_bytes=(Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory;"
        "available_memory_kib=$os.FreePhysicalMemory;os=$os.Caption;build=$os.BuildNumber}|ConvertTo-Json"]))
    result = {"captured_utc": datetime.now(timezone.utc).isoformat(), "free_disk_bytes": disk_guard(),
              "host": host, "docker": {key: docker_info[key] for key in
                   ("ServerVersion", "OSType", "OperatingSystem", "Architecture", "NCPU", "MemTotal", "KernelVersion", "Driver")},
              "instrumentation": json.loads((runtime / "instrumentation.json").read_text()),
              "protocol": PROTOCOL, "services": {}}
    # Do not export Docker Config.Env (contains local demo passwords).
    for dbms in DBMS:
        state = json.loads(command(["docker", "inspect", dc("ps", "-q", dbms)]))[0]
        with closing(connect(dbms)) as connection:
            query = ("SELECT name, setting, unit FROM pg_settings WHERE name IN "
                     "('server_version','max_connections','shared_buffers','work_mem','fsync','synchronous_commit','full_page_writes')"
                     if dbms == "postgres" else
                     "SHOW VARIABLES WHERE Variable_name IN ('version','max_connections','innodb_buffer_pool_size',"
                     "'innodb_flush_log_at_trx_commit','sync_binlog','log_bin','transaction_isolation')")
            settings = call(connection, query)[0]
        result["services"][dbms] = {"image": state["Config"]["Image"], "image_id": state["Image"],
                                           "nano_cpus": state["HostConfig"]["NanoCpus"],
                                           "memory_bytes": state["HostConfig"]["Memory"], "settings": settings}
    return result


def workload(dbms, clients, warmup, seconds, directory, isolation):
    tree = ET.parse(ROOT / "benchbase/config" / f"tpcc-{dbms}.xml")
    root = tree.getroot()
    url = root.find("url")
    url.text = url.text.replace("/tpcc?", "/tpcc_bench?")
    if dbms == "postgres":
        # PostgreSQL startup options survive an early transaction rollback.
        url.text = url.text.replace("ApplicationName=tpcc", "ApplicationName=tpcc_benchmark")
        url.text += "&options=-c%20lock_timeout%3D5000%20-c%20statement_timeout%3D15000&socketTimeout=20"
    else:
        url.text += "&socketTimeout=20000"
        ET.SubElement(root, "sessionsetupfile").text = "/lab-bench/mysql-session.sql"
    root.find("terminals").text = str(clients)
    root.find("isolation").text = isolation
    root.find("scalefactor").text = str(PROTOCOL["warehouses"])
    root.find("randomSeed").text = str(PROTOCOL["random_seed"])
    ET.SubElement(root, "retries").text = str(PROTOCOL["total_attempt_limit"])
    work = root.find("works/work")
    work.find("time").text = str(seconds)
    work.find("rate").text = "unlimited"
    work.find("weights").text = ",".join(map(str, PROTOCOL["weights"]))
    ET.SubElement(work, "warmup").text = str(warmup)
    ET.indent(tree)
    tree.write(directory / "workload.xml", encoding="utf-8", xml_declaration=True)


def sample_sql(dbms, directory, phase, stop, errors):
    try:
        with closing(connect(dbms, observer=True)) as connection, (directory / "db-samples.csv").open("w", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream)
            writer.writerow(["utc", "phase", "sessions", "lock_waiting_sessions", "sample_ms"])
            while not stop.is_set():
                started = time.monotonic()
                if dbms == "postgres":
                    rows = call(connection, "SELECT COUNT(*), COUNT(*) FILTER (WHERE wait_event_type='Lock') "
                                "FROM pg_stat_activity WHERE datname='tpcc_bench' AND application_name='tpcc_benchmark'")[0]
                else:
                    rows = call(connection, "SELECT (SELECT COUNT(*) FROM performance_schema.threads "
                                "WHERE PROCESSLIST_DB='tpcc_bench' AND PROCESSLIST_ID<>CONNECTION_ID()), "
                                "(SELECT COUNT(DISTINCT w.REQUESTING_THREAD_ID) FROM performance_schema.data_lock_waits w "
                                "JOIN performance_schema.data_locks l ON l.ENGINE=w.ENGINE AND l.ENGINE_LOCK_ID=w.REQUESTING_ENGINE_LOCK_ID "
                                "WHERE l.OBJECT_SCHEMA='tpcc_bench')")[0]
                writer.writerow([datetime.now(timezone.utc).isoformat(), phase[0], *rows[0], round((time.monotonic()-started)*1000, 3)])
                stream.flush()
                stop.wait(max(0, 1 - (time.monotonic()-started)))
    except Exception as error:
        errors.append(f"SQL sampler: {error}")


def sample_resources(names, directory, phase, stop, errors):
    try:
        with (directory / "resources.jsonl").open("w", encoding="utf-8") as stream:
            while not stop.is_set():
                started = time.monotonic()
                output = command(["docker", "stats", "--no-stream", "--format", "{{json .}}", *names], timeout=20)
                for line in output.splitlines():
                    stream.write(json.dumps({"utc": datetime.now(timezone.utc).isoformat(), "phase": phase[0],
                                             "collection_ms": round((time.monotonic()-started)*1000, 3), "stats": json.loads(line)}) + "\n")
                stream.flush()
                stop.wait(max(0, 2 - (time.monotonic()-started)))
    except Exception as error:
        errors.append(f"Resource sampler: {error}")


def quantile(values, probability):
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered)-1)*probability
    low = math.floor(position)
    high = math.ceil(position)
    return ordered[low] + (ordered[high]-ordered[low])*(position-low)


def analyze(directory, expected_clients):
    summaries = list((directory / "native").glob("*.summary.json"))
    if len(summaries) != 1:
        raise RuntimeError("Expected one native summary; benchmark may have failed")
    native = json.loads(summaries[0].read_text(encoding="utf-8"))
    elapsed = native["Elapsed Time (nanoseconds)"] / 1e9
    rows = []
    for path in (directory / "requests").glob("worker-*.csv"):
        with path.open(newline="", encoding="utf-8") as stream:
            rows.extend(csv.DictReader(stream))
    if len(rows) != native["Measured Requests"] or not rows or elapsed <= 0:
        raise RuntimeError("Outcome sidecar/native request-count validation failed")
    if any(int(row["attempts"]) < 1 or int(row["end_ns"]) < int(row["start_ns"]) for row in rows):
        raise RuntimeError("Invalid timing or attempt count")
    statuses = Counter(row["status"] for row in rows)
    aborts = Counter(row["transaction_type"] for row in rows if row["status"] == "USER_ABORTED")
    if statuses["SUCCESS"] == 0:
        raise RuntimeError("No successful measured requests")
    sql_errors = Counter(f"{row['sqlstate']}/{row['sqlcode']}" for row in rows if row["sqlstate"])
    allowed = {"40001/0", "40P01/0", "40001/1213", "HY000/1205", "55P03/0", "57014/0"}
    unexpected = set(sql_errors) - allowed
    if unexpected or statuses["UNKNOWN"] or statuses["RETRY_DIFFERENT"]:
        raise RuntimeError(f"Unexpected SQL errors/statuses: {sql_errors}, {statuses}")
    latencies = [(int(row["end_ns"])-int(row["start_ns"]))/1e6 for row in rows]
    successful = [(int(row["end_ns"])-int(row["start_ns"]))/1e6 for row in rows if row["status"] == "SUCCESS"]
    samples = list(csv.DictReader((directory / "db-samples.csv").open(encoding="utf-8")))
    observed_sessions = max((int(row["sessions"]) for row in samples), default=0)
    if observed_sessions < expected_clients:
        raise RuntimeError(f"Only observed {observed_sessions}/{expected_clients} DB sessions")
    result = {"validation": "PASS", "elapsed_measure_seconds": elapsed, "measured_requests": len(rows),
              "native_count_matches": True, "status_counts": dict(statuses), "last_sql_error_counts": dict(sql_errors),
              "successful_tps": statuses["SUCCESS"] / elapsed, "completed_requests_per_second": len(rows)/elapsed,
              "latency_success_ms": {"p50": quantile(successful, .5), "p95": quantile(successful, .95), "p99": quantile(successful, .99)},
              "latency_all_completed_ms": {"p50": quantile(latencies, .5), "p95": quantile(latencies, .95), "p99": quantile(latencies, .99)},
              "requests_with_retry": sum(int(row["attempts"]) > 1 for row in rows),
              "reexecutions_started": sum(max(0, int(row["attempts"])-1) for row in rows),
              "retry_events": sum(int(row["retry_events"]) for row in rows),
              "failed_requests": sum(statuses.get(status, 0) for status in ("ERROR", "RETRY", "RETRY_DIFFERENT", "UNKNOWN")),
              "expected_user_aborts": statuses["USER_ABORTED"], "max_observed_sessions": observed_sessions,
              "max_sampled_lock_waiters": max((int(row["lock_waiting_sessions"]) for row in samples), default=0),
              "native_goodput_is_not_used": native["Goodput (requests/second)"]}
    result["retry_request_rate"] = result["requests_with_retry"] / len(rows)
    result["failure_rate"] = result["failed_requests"] / len(rows)
    result["user_aborts_by_transaction_id"] = dict(aborts)
    result["neworder_intentional_aborts"] = aborts["1"]
    result["delivery_conflict_aborts"] = aborts["4"]
    save(directory / "metrics.json", result)
    return result


def consistency(dbms):
    with closing(connect(dbms)) as connection:
        checks = {
            "warehouse_equals_district_ytd": int(call(connection,
                "SELECT COUNT(*) FROM warehouse w WHERE w.w_ytd <> "
                "(SELECT SUM(d.d_ytd) FROM district d WHERE d.d_w_id=w.w_id)")[0][0][0]) == 0,
            "district_next_order_id": int(call(connection,
                "SELECT COUNT(*) FROM district d WHERE d.d_next_o_id <> "
                "(SELECT MAX(o.o_id)+1 FROM oorder o WHERE o.o_w_id=d.d_w_id AND o.o_d_id=d.d_id)")[0][0][0]) == 0,
            "order_line_total": call(connection, "SELECT COUNT(*) FROM order_line")[0][0][0] ==
                call(connection, "SELECT SUM(o_ol_cnt) FROM oorder")[0][0][0],
            "stock_quantity_range": int(call(connection,
                "SELECT COUNT(*) FROM stock WHERE s_quantity<10 OR s_quantity>100")[0][0][0]) == 0,
        }
        if not all(checks.values()):
            raise RuntimeError(f"Post-workload consistency check failed: {checks}")
        return checks


def trial(dbms, clients, warmup, seconds, kind, repetition, isolation_mode=None):
    disk_guard()
    other = "mysql" if dbms == "postgres" else "postgres"
    dc("stop", other)
    dc("up", "-d", dbms)
    ready(dbms)
    if isolation_mode and dbms == "mysql":
        with closing(connect(dbms, "tpcc")) as connection:
            if call(connection, "SELECT @@GLOBAL.log_bin")[0][0][0] != 0:
                raise RuntimeError("MySQL binary logging must be OFF for E6 isolation measurements")
    original = fingerprint(dbms, "tpcc")
    restore(dbms)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    isolation = ISOLATIONS[isolation_mode] if isolation_mode else PROTOCOL["isolation"]
    runtime = HERE / "runtime" / "isolation" if isolation_mode else HERE / "runtime"
    prefix = f"{kind}-isolation-{isolation_mode}" if isolation_mode else kind
    run_id = f"{prefix}-c{clients}-r{repetition}-{stamp}"
    directory = HERE / "results" / dbms / run_id
    directory.mkdir(parents=True)
    workload(dbms, clients, warmup, seconds, directory, isolation)
    save(directory / "source-fingerprint-before.json", original)
    database_state = json.loads(command(["docker", "inspect", dc("ps", "-q", dbms)]))[0]
    if database_state["HostConfig"]["NanoCpus"] != 2_000_000_000 or database_state["HostConfig"]["Memory"] != 2*2**30:
        raise RuntimeError("Database CPU/RAM limits do not match the protocol")
    name = f"labbench-{dbms}-{stamp.lower()}"
    relative = f"/benchbase/results/{run_id}"
    manifest = {"kind": kind, "dbms": dbms, "clients": clients, "warmup_seconds": warmup,
                "measure_seconds": seconds, "repetition": repetition, "isolation": isolation,
                "protocol": {**PROTOCOL, "isolation": isolation},
                "baseline_sha256": ownership(dbms)["archive_sha256"],
                "image": database_state["Config"]["Image"], "image_id": database_state["Image"],
                "free_disk_bytes": disk_guard(), "workload_sha256": sha(directory / "workload.xml"),
                "instrumentation": json.loads((runtime / "instrumentation.json").read_text()),
                "jar_sha256": sha(runtime / f"benchbase-{dbms}-instrumented.jar"),
                "status": "RUNNING", "started_utc": datetime.now(timezone.utc).isoformat()}
    if isolation_mode and dbms == "mysql":
        manifest["mysql_log_bin"] = False
    save(directory / "run.json", manifest)
    stop = threading.Event()
    phase = ["startup"]
    errors = []
    threads = []
    log = Timeline(directory)
    print(f"{dbms}: {kind}, {isolation}, {clients} clients, warmup={warmup}s, measure={seconds}s", flush=True)
    try:
        with mysql_observer_access(log, directory) if dbms == "mysql" else nullcontext():
            dc("run", "-d", "--no-deps", "--name", name, "-e",
               f"JAVA_TOOL_OPTIONS=-Xms128m -Xmx512m -Dlab.metrics.dir={relative}/requests",
               f"benchbase-{dbms}", "-b", "tpcc", "-c", f"/lab-bench/results/{dbms}/{run_id}/workload.xml",
               "--create=false", "--load=false", "--execute=true", "-d", relative + "/native", "-s", "1")
            generator_state = json.loads(command(["docker", "inspect", name]))[0]
            manifest["generator"] = {"image": generator_state["Config"]["Image"], "image_id": generator_state["Image"],
                                     "nano_cpus": generator_state["HostConfig"]["NanoCpus"],
                                     "memory_bytes": generator_state["HostConfig"]["Memory"]}
            if manifest["generator"]["nano_cpus"] != 4_000_000_000 or manifest["generator"]["memory_bytes"] != 2**30:
                raise RuntimeError("Generator CPU/RAM limits do not match the protocol")
            database_name = dc("ps", "-q", dbms)
            for target, args in ((sample_sql, (dbms, directory, phase, stop, errors)),
                                 (sample_resources, ([database_name, name], directory, phase, stop, errors))):
                thread = threading.Thread(target=target, args=args, daemon=True)
                thread.start()
                threads.append(thread)
            # Container logs provide approximate phase labels only. Request CSV uses the exact native guard.
            def logs():
                with (directory / "benchbase.log").open("w", encoding="utf-8") as stream:
                    process = subprocess.Popen(["docker", "logs", "-f", name], stdout=subprocess.PIPE,
                                               stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
                    for line in process.stdout:
                        stream.write(line)
                        stream.flush()
                        if "Warmup complete" in line:
                            phase[0] = "measure"
                        elif "PHASE START" in line:
                            phase[0] = "warmup"
                        elif "Waiting for all terminals" in line:
                            phase[0] = "drain"
                    process.wait()
            reader = threading.Thread(target=logs, daemon=True)
            reader.start()
            exit_code = int(command(["docker", "wait", name], timeout=warmup+seconds+120))
            reader.join(timeout=10)
            state = json.loads(command(["docker", "inspect", name, "--format", "{{json .State}}"]))
            save(directory / "container-state.json", state)
            stop.set()
            for thread in threads:
                thread.join(timeout=25)
            if exit_code or state["OOMKilled"] or errors:
                raise RuntimeError(f"Generator failed: exit={exit_code}, OOM={state['OOMKilled']}, sampler_errors={errors}")
            result = analyze(directory, clients)
            if isolation_mode:
                expected = "2" if isolation_mode == "rc" else "4"
                observed = re.findall(r"E6 verified isolation worker=(\d+) jdbc=(\d+)",
                                      (directory / "benchbase.log").read_text(encoding="utf-8"))
                if len({worker for worker, _ in observed}) != clients or any(value != expected for _, value in observed):
                    raise RuntimeError("JDBC transaction isolation evidence did not match all workers")
                result["jdbc_isolation_verified_workers"] = clients
            result["consistency_checks"] = consistency(dbms)
            save(directory / "metrics.json", result)
        if fingerprint(dbms, "tpcc") != original:
            raise RuntimeError("Source tpcc fingerprint changed during benchmark")
        save(directory / "source-fingerprint-after.json", original)
        manifest["status"] = "PASS"
        manifest["result"] = result
        print(f"{dbms}: PASS; {result['measured_requests']} requests; {result['max_observed_sessions']} sessions observed", flush=True)
        return {"directory": directory.relative_to(HERE).as_posix(), "dbms": dbms, "clients": clients,
                "isolation": isolation, "repetition": repetition, **result}
    except BaseException as error:
        manifest["status"] = "FAIL"
        manifest["error"] = str(error)
        raise
    finally:
        stop.set()
        for thread in threads:
            thread.join(timeout=25)
        subprocess.run(["docker", "rm", "-f", name], cwd=ROOT, capture_output=True, timeout=30)
        save(directory / "run.json", manifest)
        log.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("setup", "smoke", "run", "plan"))
    parser.add_argument("--dbms", choices=(*DBMS, "both"), default="both")
    parser.add_argument("--clients", nargs="+", type=int)
    parser.add_argument("--warmup", type=int)
    parser.add_argument("--seconds", type=int)
    parser.add_argument("--repetitions", type=int)
    parser.add_argument("--first-repetition", type=int, default=1,
                        help="Repetition number of the first run; preserves the scheduled order")
    parser.add_argument("--compare-isolation", action="store_true",
                        help="Pair RC and RR with MySQL binlog disabled, saving separate results")
    args = parser.parse_args()
    if args.action == "plan":
        print(json.dumps(PROTOCOL, indent=2))
        return
    selected = DBMS if args.dbms == "both" else (args.dbms,)
    if args.action == "setup":
        setup()
        return
    smoke = args.action == "smoke"
    if args.compare_isolation:
        COMPOSE.extend(["-f", str(HERE / "compose.isolation.yaml")])
    default_clients = [4, 16, 64] if args.compare_isolation else PROTOCOL["clients"]
    clients = args.clients or ([4, 64] if smoke and args.compare_isolation else
                               [1, 32, 64, 128] if smoke else default_clients)
    warmup = args.warmup if args.warmup is not None else (3 if smoke else PROTOCOL["warmup_seconds"])
    seconds = args.seconds if args.seconds is not None else (6 if smoke else PROTOCOL["measure_seconds"])
    repetitions = args.repetitions if args.repetitions is not None else (1 if smoke else PROTOCOL["repetitions"])
    if any(c < 1 or c > 128 for c in clients) or min(warmup, seconds, repetitions, args.first_repetition) < 1:
        parser.error("Require 1..128 clients and positive warmup, seconds, repetitions")
    reports = []
    kind = "smoke" if smoke else "formal"
    campaign = HERE
    if args.compare_isolation:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        campaign = HERE / "isolation" / f"{kind}-{stamp}"
        runtime = HERE / "runtime" / "isolation"
        required = [runtime / "instrumentation.json", *(runtime / f"benchbase-{db}-instrumented.jar" for db in selected)]
        if missing := [path.name for path in required if not path.is_file()]:
            parser.error(f"Build the isolation instrumentation first: {', '.join(missing)}")
    report_path = campaign / f"{kind}-report.json"
    try:
        if args.compare_isolation:
            dc("up", "-d", "postgres", "mysql")
            for dbms in DBMS:
                ready(dbms)
                ownership(dbms)
            save(campaign / "environment.json", environment(runtime))
        # Alternate engine order per repetition; never run two loads simultaneously.
        for repetition in range(args.first_repetition, args.first_repetition + repetitions):
            for index, count in enumerate(clients):
                for dbms in selected if repetition % 2 else tuple(reversed(selected)):
                    if args.compare_isolation:
                        modes = tuple(ISOLATIONS) if (repetition + index) % 2 else tuple(reversed(ISOLATIONS))
                    else:
                        modes = (None,)
                    for mode in modes:
                        reports.append(trial(dbms, count, warmup, seconds, kind, repetition, mode))
                        save(report_path, {"kind": kind, "complete": False, "runs": reports})
        save(report_path, {"kind": kind, "complete": True, "runs": reports})
    finally:
        # Leave isolated benchmark databases at baseline and return both demo services.
        dc("up", "-d", "postgres", "mysql")
        cleanup_errors = []
        for dbms in selected:
            try:
                ready(dbms)
                restore(dbms)
            except Exception as error:
                cleanup_errors.append(f"{dbms}: {error}")
        save(campaign / "last-cleanup.json", {"errors": cleanup_errors, "utc": datetime.now(timezone.utc).isoformat()})
        if cleanup_errors:
            raise RuntimeError(f"Benchmark baseline cleanup failed: {cleanup_errors}")


if __name__ == "__main__":
    # One runner at a time; Windows releases the lock even if Python is interrupted.
    import msvcrt
    with (HERE / ".runner.lock").open("a+b") as lock:
        lock.seek(0)
        if lock.read(1) == b"":
            lock.write(b"0")
            lock.flush()
        lock.seek(0)
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        main()
