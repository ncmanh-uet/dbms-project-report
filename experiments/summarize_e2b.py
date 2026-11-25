"""Verify saved E2b transaction CSVs and summarize final states without running load."""

import argparse
from collections import Counter
import csv
from decimal import Decimal
import hashlib
import json
from pathlib import Path


def summarize(source, output):
    rows = []
    hashes = {}
    for path in sorted(source.glob("*/summary.json")):
        result = json.loads(path.read_text(encoding="utf-8"))
        if result.get("kind") != "short_trial":
            continue
        csv_path = path.parent / "transactions.csv"
        with csv_path.open(encoding="utf-8", newline="") as handle:
            attempts = list(csv.DictReader(handle))
        commits = Counter(int(row["item_id"]) for row in attempts if row["status"] == "OK")
        failures = [row for row in attempts if row["status"] != "OK"]
        check = result["data_check"]
        if not (result["status"] == "PASS" and check["counter_check"] and check["restored"]
                and not result["unknown_commit"] and not result["fatal_errors"]
                and sum(commits.values()) == result["successful_transactions"]
                and len(failures) == result["failed_attempts"]):
            raise RuntimeError(f"Invalid E2b evidence: {path}")
        # These historical runs recorded no failed attempts. Do not infer aborts
        # from arbitrary SQL failures or treat a later generated request as a retry.
        if failures or result["retries"] != 0:
            raise RuntimeError(f"Failure/retry classification needs inspection: {path}")
        for item, before in check["before"].items():
            if not (Decimal(check["after_transactions"][item]) == Decimal(before) + commits[int(item)]
                    and Decimal(check["after_restore"][item]) == Decimal(before)):
                raise RuntimeError(f"Counter or restoration mismatch: {path}, item {item}")
        if check["confirmed_commits"] != sum(commits.values()):
            raise RuntimeError(f"COMMIT count mismatch: {path}")
        for evidence in (path, csv_path, path.parent / "confirmed-commits.json"):
            hashes[evidence.relative_to(source).as_posix()] = hashlib.sha256(evidence.read_bytes()).hexdigest()
        recorded = json.loads((path.parent / "confirmed-commits.json").read_text())
        if Counter({int(k): v for k, v in recorded.items()}) != commits:
            raise RuntimeError(f"Per-row COMMIT manifest mismatch: {path}")
        rows.append(dict(dbms=result["dbms"], scenario=result["scenario"], clients=result["clients"],
                         isolation=result["isolation"], successful_commits=sum(commits.values()),
                         failed_attempts=len(failures), aborts=0, retries=result["retries"],
                         max_waiters=result["lock_observation"]["max_observed_blocked_sessions"],
                         before=check["before"], after=check["after_transactions"],
                         restored=check["after_restore"], counter_check=True, cleanup_passed=True,
                         summary_path=path.relative_to(source).as_posix()))
    expected = {(db, scenario, clients) for db in ("postgres", "mysql")
                for scenario in ("distinct", "shared4", "hot1") for clients in (1, 4, 8, 16)}
    if len(rows) != 24 or {(r["dbms"], r["scenario"], r["clients"]) for r in rows} != expected:
        raise RuntimeError("Expected exactly 24 historical E2b trials")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(dict(status="PASS", source_run=source.name,
                                     checked_trials=len(rows), results=rows, source_sha256=hashes),
                                 indent=2) + "\n", encoding="utf-8")
    print(f"Verified {len(rows)} E2b trials: {output}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    summarize(args.source, args.output)
