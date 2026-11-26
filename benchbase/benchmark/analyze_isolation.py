"""Audit the E6 RC/RR campaign and export per-trial, per-mode and paired results."""

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
from statistics import mean
from xml.etree import ElementTree as ET

from analyze_results import (
    DBMS, HERE, Inputs, lock_metrics, percent, quantile, request_metrics,
    require, resource_metrics, same_number, save_csv, save_json,
)

MODES = {"TRANSACTION_READ_COMMITTED": ("RC", "2"),
         "TRANSACTION_REPEATABLE_READ": ("RR", "4")}
METRICS = ("successful_tps", "p95_ms", "retry_pct", "delivery_abort_pct",
           "mean_waiting_session_pct", "db_cpu_quota_pct_mean", "generator_cpu_quota_pct_mean",
           "completion_pct", "exhausted_retry_pct")
COUNTS = ("requests", "successes", "with_retry", "delivery_aborts", "neworder_rollbacks",
          "exhausted_retry", "terminal_errors", "pg_40001", "pg_40P01", "mysql_1213", "mysql_1205",
          "pg_55P03", "pg_57014")


def audit_run(inputs, entry, environment):
    directory = HERE / entry["directory"]
    manifest = inputs.json(directory / "run.json")
    metrics = inputs.json(directory / "metrics.json")
    db, clients, repetition, isolation = (manifest[key] for key in ("dbms", "clients", "repetition", "isolation"))
    mode, jdbc = MODES[isolation]
    require(all(entry[key] == manifest[key] for key in ("dbms", "clients", "repetition", "isolation")),
            "Campaign index differs from run manifest")
    require(manifest["kind"] == "formal" and manifest["status"] == metrics["validation"] == "PASS",
            "Run failed validation")
    require(len(metrics["consistency_checks"]) == 4 and all(metrics["consistency_checks"].values()),
            "Post-workload invariant failed")
    require(inputs.json(directory / "source-fingerprint-before.json") ==
            inputs.json(directory / "source-fingerprint-after.json"), "Source data changed")
    require(db != "mysql" or manifest["mysql_log_bin"] is False, "MySQL binlog was enabled")
    require(manifest["image_id"] == environment["services"][db]["image_id"] and
            manifest["jar_sha256"] == environment["instrumentation"]["profiles"][db]["instrumented_jar_sha256"],
            "Engine image or instrumentation changed")
    xml = inputs.read(directory / "workload.xml")
    require(hashlib.sha256(xml.encode()).hexdigest() == manifest["workload_sha256"], "Workload XML changed")
    workload = ET.fromstring(xml)
    expected_xml = {"isolation": isolation, "terminals": str(clients), "scalefactor": "1", "retries": "3",
                    "works/work/warmup": "15", "works/work/time": "60", "works/work/rate": "unlimited",
                    "works/work/weights": "45,43,4,4,4", "randomSeed": "20261023"}
    require(all(workload.findtext(key) == value for key, value in expected_xml.items()), "Unexpected workload setup")
    verified = re.findall(r"E6 verified isolation worker=(\d+) jdbc=(\d+)", inputs.read(directory / "benchbase.log"))
    require(len({worker for worker, _ in verified}) == clients and all(value == jdbc for _, value in verified),
            "JDBC isolation evidence differs from workload XML")

    native_path, = (directory / "native").glob("*.summary.json")
    native = inputs.json(native_path)
    elapsed = native["Elapsed Time (nanoseconds)"] / 1e9
    data = request_metrics(row for path in (directory / "requests").glob("worker-*.csv") for row in inputs.csv(path))
    statuses, errors = data["statuses"], data["last_errors"]
    requests = statuses.total()
    require(elapsed >= 59 and requests == native["Measured Requests"] == metrics["measured_requests"] and
            statuses == Counter(metrics["status_counts"]) and errors == Counter(metrics["last_sql_error_counts"]),
            "Raw measurement totals differ from saved outcomes")
    require(data["retry_requests"] == metrics["requests_with_retry"], "Retry count mismatch")
    tps = statuses["SUCCESS"] / elapsed
    p95 = quantile(data["successful_latency"], .95)
    same_number(tps, metrics["successful_tps"], "successful TPS")
    same_number(p95, metrics["latency_success_ms"]["p95"], "p95")
    lock = lock_metrics(list(inputs.csv(directory / "db-samples.csv")))
    require(lock["max_observed_measure_sessions"] >= clients, "Missing DB sessions during measurement")
    resources = [json.loads(line) for line in inputs.read(directory / "resources.jsonl").splitlines() if line.strip()]
    resources = resource_metrics(resources, manifest["protocol"]["db_cpu_limit"],
                                 manifest["protocol"]["generator_cpu_limit"])
    return {"dbms": db, "clients": clients, "repetition": repetition, "isolation": mode,
            "directory": entry["directory"], "elapsed_seconds": elapsed, "requests": requests,
            "successes": statuses["SUCCESS"], "successful_tps": tps, "p95_ms": p95,
            "completion_pct": percent(statuses["SUCCESS"], requests),
            "exhausted_retry_pct": percent(statuses["RETRY"], requests),
            "with_retry": data["retry_requests"], "retry_pct": percent(data["retry_requests"], requests),
            "delivery_requests": data["types"][4], "delivery_aborts": data["aborted"][4],
            "delivery_abort_pct": percent(data["aborted"][4], data["types"][4]),
            "neworder_rollbacks": data["aborted"][1], "exhausted_retry": statuses["RETRY"],
            "terminal_errors": statuses["ERROR"], "pg_40001": errors["40001/0"],
            "pg_40P01": errors["40P01/0"], "mysql_1213": errors["40001/1213"],
            "mysql_1205": errors["HY000/1205"], "pg_55P03": errors["55P03/0"],
            "pg_57014": errors["57014/0"], **lock, **resources}


def summarize(index):
    summary, pairs = [], []
    for db in DBMS:
        for clients in (4, 16, 64):
            for mode in ("RC", "RR"):
                runs = [index[db, clients, mode, repetition] for repetition in range(1, 4)]
                row = {"dbms": db, "clients": clients, "isolation": mode, "repetitions": len(runs)}
                for metric in METRICS:
                    values = [run[metric] for run in runs]
                    row.update({f"{metric}_mean": mean(values), f"{metric}_min": min(values),
                                f"{metric}_max": max(values)})
                row.update({name: sum(run[name] for run in runs) for name in COUNTS})
                summary.append(row)
            for repetition in range(1, 4):
                rc, rr = (index[db, clients, mode, repetition] for mode in ("RC", "RR"))
                pair = {"dbms": db, "clients": clients, "repetition": repetition}
                for metric in ("successful_tps", "p95_ms"):
                    pair[f"{metric}_rr_minus_rc_pct"] = 100 * (rr[metric] / rc[metric] - 1)
                for metric in ("retry_pct", "delivery_abort_pct", "mean_waiting_session_pct", "db_cpu_quota_pct_mean",
                               "completion_pct", "exhausted_retry_pct"):
                    pair[f"{metric}_rr_minus_rc_pp"] = rr[metric] - rc[metric]
                pairs.append(pair)
    return summary, pairs


def read_campaign(inputs, campaign):
    report = inputs.json(campaign / "formal-report.json")
    environment = inputs.json(campaign / "environment.json")
    cleanup = inputs.json(campaign / "last-cleanup.json")
    require(report["complete"] and not cleanup["errors"], "Campaign incomplete or cleanup failed")
    return report["runs"], environment


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("campaign", type=Path, help="Campaign directory containing formal-report.json")
    parser.add_argument("--replace-third-from", type=Path,
                        help="Use a separately recorded rerun of repetition 3")
    args = parser.parse_args()
    campaign = args.campaign.resolve()
    inputs = Inputs()
    runs, environment = read_campaign(inputs, campaign)
    output = campaign
    if args.replace_third_from:
        rerun = args.replace_third_from.resolve()
        replacement, rerun_environment = read_campaign(inputs, rerun)
        require(all(r["repetition"] == 3 for r in replacement), "Expected repetition 3 only")
        for db in DBMS:
            require(rerun_environment["services"][db] == environment["services"][db],
                    f"{db} DBMS configuration differs from original E6 campaign")
        runs = [r for r in runs if r["repetition"] != 3] + replacement
        output = rerun
    trials = [audit_run(inputs, entry, environment) for entry in runs]
    index = {(r["dbms"], r["clients"], r["isolation"], r["repetition"]): r for r in trials}
    expected = {(db, clients, mode, repetition) for db in DBMS for clients in (4, 16, 64)
                for mode in ("RC", "RR") for repetition in range(1, 4)}
    require(len(trials) == len(index) and index.keys() == expected, "Missing or duplicate trial")
    inputs.verify_unchanged()
    summary, pairs = summarize(index)
    save_csv(output / "trials.csv", trials)
    save_csv(output / "summary.csv", summary)
    save_csv(output / "paired.csv", pairs)
    save_json(output / "verification.json", {"status": "PASS", "trials": len(trials), "pairs": len(pairs),
                                            "inputs": inputs.files})
    print(f"Verified {len(trials)} trials and {len(pairs)} within-engine RC/RR pairs")


if __name__ == "__main__":
    main()
