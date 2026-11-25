"""Capture the demo environment and independently verify E1/E2a/E4 evidence.

Snapshot before running, then verify selected summaries against live source data.
All database queries in this verifier are read-only.
"""

import argparse
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import subprocess

from e1_mvcc import ROOT, connect
from e2_support import call, save_json


TABLES = ("warehouse", "district", "customer", "history", "item", "stock",
          "oorder", "new_order", "order_line")


def capture():
    result = {}
    for db in ("postgres", "mysql"):
        connection = connect(db)
        try:
            version = call(connection, "SELECT version()" if db == "postgres" else "SELECT VERSION()")[0][0][0]
            targets = {
                "warehouse": call(connection, "SELECT * FROM warehouse WHERE w_id=1")[0],
                "stock": call(connection, "SELECT * FROM stock WHERE s_w_id=1 AND s_i_id BETWEEN 1 AND 16 ORDER BY s_i_id")[0],
            }
            if len(targets["warehouse"]) != 1 or len(targets["stock"]) != 16:
                raise RuntimeError(f"{db}: missing source rows")
            counts = {table: call(connection, f"SELECT COUNT(*) FROM {table}")[0][0][0]
                      for table in TABLES}
            if db == "postgres":
                settings = dict(call(connection, "SELECT name,setting FROM pg_settings WHERE name IN "
                                     "('shared_buffers','max_connections','fsync','synchronous_commit',"
                                     "'full_page_writes','default_transaction_isolation','deadlock_timeout')")[0])
            else:
                names = ("innodb_buffer_pool_size", "max_connections", "innodb_flush_log_at_trx_commit",
                         "sync_binlog", "log_bin", "transaction_isolation", "innodb_deadlock_detect")
                settings = dict(zip(names, call(connection, "SELECT " + ",".join("@@" + name for name in names))[0][0]))
            container = subprocess.run(["docker", "compose", "ps", "-q", db], cwd=ROOT,
                                       text=True, capture_output=True, check=True).stdout.strip()
            info = json.loads(subprocess.run(["docker", "inspect", container], text=True,
                                            capture_output=True, check=True).stdout)[0]
            host = info["HostConfig"]
            result[db] = dict(version=version, target_rows=targets, row_counts=counts,
                              settings=settings, image=info["Config"]["Image"],
                              nano_cpus=host["NanoCpus"], memory_bytes=host["Memory"],
                              memory_swap_bytes=host["MemorySwap"])
        finally:
            connection.close()
    result["e6_hashes"] = {
        name: hashlib.sha256((ROOT / "benchbase" / "benchmark" / name).read_bytes()).hexdigest()
        for name in ("protocol.json", "formal-report.json", "analysis/summary.csv", "analysis/trials.csv")
    }
    # Use the same JSON representation before and after (Decimal, dates, tuples).
    return json.loads(json.dumps(result, default=str))


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def verify(args, current):
    require(read(args.baseline) == current, "Source rows, environment, counts, or E6 evidence changed")
    for db in ("postgres", "mysql"):
        env = current[db]
        require(env["nano_cpus"] == 2_000_000_000, f"{db}: CPU quota is not 2 vCPU")
        require(env["memory_bytes"] == env["memory_swap_bytes"] == 2 * 1024**3,
                f"{db}: memory/swap quota is not 2 GiB")
    expected = {("postgres", "READ COMMITTED"), ("postgres", "REPEATABLE READ"),
                ("mysql", "READ COMMITTED"), ("mysql", "REPEATABLE READ")}
    all_rows = {}
    for experiment in ("e1", "e2"):
        payload = read(getattr(args, experiment))
        rows = payload["results"]
        require(len(rows) == 4 and {(r["dbms"], r["isolation_level"]) for r in rows} == expected,
                f"{experiment}: incomplete or duplicate case matrix")
        for row in rows:
            require(row["status"] == "PASS" and row["schedule_verified"] and row["cleanup_passed"],
                    f"{experiment}: schedule or cleanup failed")
            rr = row["isolation_level"] == "REPEATABLE READ"
            if experiment == "e1":
                baseline = Decimal(row["baseline"])
                require(Decimal(row["t2_read_1"]) == baseline, "E1: first read is wrong")
                require(Decimal(row["t2_read_2"]) == baseline + (0 if rr else 100), "E1: snapshot is wrong")
                require(Decimal(row["final_db_value"]) == baseline + 100, "E1: committed DB value is wrong")
                require(Decimal(row["restored_value"]) == baseline, "E1: source was not restored")
                require(row["aborts"] == row["retries"] == 0 and not row["error_codes"], "E1: unexpected abort/retry")
            else:
                serialization = rr and row["dbms"] == "postgres"
                baseline = Decimal(row["initial_value"])
                require(row["lock_wait_observed"] and row["observed_blocked_span_ms"] >= 100,
                        "E2a: lock wait was not confirmed")
                require(Decimal(row["after_initial_value"]) == baseline + (1 if serialization else 2), "E2a: rollback state is wrong")
                require(Decimal(row["final_value"]) == baseline + 2, "E2a: final counter is wrong")
                require(row["aborts"] == row["retries"] == int(serialization), "E2a: abort/retry counts are wrong")
                require(row["data_check"]["confirmed_commits"] == 2, "E2a: COMMIT count is wrong")
                require(row["error_codes"] == ([{"sqlstate": "40001", "errno": None}] if serialization else []),
                        "E2a: unexpected SQL error")
        all_rows[experiment] = len(rows)
    rows = read(args.e4)["results"]
    require(len(rows) == 20 and {(r["dbms"], r["mode"], r["round"]) for r in rows} ==
            {(db, mode, n) for db in ("postgres", "mysql") for mode in ("opposite", "ordered") for n in range(1, 6)},
            "E4: incomplete or duplicate case matrix")
    for row in rows:
        require(row["status"] == "PASS" and row["schedule_verified"] and row["cleanup_passed"], "E4: case failed")
        opposite = row["mode"] == "opposite"
        require(row["isolation_level"] == "READ COMMITTED", "E4: isolation changed")
        require(row["aborts"] == row["retries"] == row["deadlocks"] == int(opposite), "E4: counts are wrong")
        require(row["lock_timeouts"] == 0, "E4: timeout is not a deadlock")
        expected_error = {"sqlstate": "40P01", "errno": None} if row["dbms"] == "postgres" else {"sqlstate": "40001", "errno": 1213}
        require(row["error_codes"] == ([expected_error] if opposite else []), "E4: error classification is wrong")
        for item, baseline in row["data_check"]["before"].items():
            if opposite:
                require(Decimal(row["after_detection"][item]) == Decimal(baseline) + 1, "E4: partial victim update survived")
            require(Decimal(row["after_completed_requests"][item]) == Decimal(baseline) + 2, "E4: final counter is wrong")
    all_rows["e4"] = len(rows)
    if args.e2b:
        smoke = read(args.e2b)
        rows = smoke["results"]
        require(smoke["status"] == "PASS" and len(rows) == 12, "E2b: incomplete smoke run")
        require({(r["dbms"], r["scenario"], r["clients"]) for r in rows} ==
                {(db, scenario, clients) for db in ("postgres", "mysql")
                 for scenario in ("distinct", "shared4", "hot1") for clients in (1, 4)}, "E2b: wrong smoke matrix")
        require(all(r["status"] == "PASS" and r["isolation"] == "READ COMMITTED"
                    and r["data_check"]["restored"] and not r["unknown_commit"] for r in rows),
                "E2b: source cleanup or RC regression failed")
        all_rows["e2b_smoke"] = len(rows)
    return dict(status="PASS", checked_cases=all_rows, source_unchanged=True,
                environment_unchanged=True, e6_evidence_unchanged=True,
                selected_summaries={key: Path(getattr(args, key)).resolve().relative_to(ROOT).as_posix()
                                    for key in ("e1", "e2", "e4", "e2b") if getattr(args, key)},
                source_sha256={name: hashlib.sha256((ROOT / "experiments" / name).read_bytes()).hexdigest()
                               for name in ("e1_mvcc.py", "e2_contention.py", "e2_support.py", "e4_deadlock.py", "verify_e124.py")})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--baseline", type=Path)
    for key in ("e1", "e2", "e4", "e2b"):
        parser.add_argument("--" + key, type=Path)
    args = parser.parse_args()
    if args.baseline and not all(getattr(args, key) for key in ("e1", "e2", "e4")):
        parser.error("Verification requires --e1, --e2 and --e4 summaries")
    current = capture()
    report = verify(args, current) if args.baseline else current
    save_json(args.output, report)
    print(f"Evidence saved: {args.output}")


if __name__ == "__main__":
    main()
