"""Read-only final checks for the step-16 preparation (requires Docker running)."""
from datetime import datetime, timezone
import json
from pathlib import Path

import run_benchmark as bench


def main():
    here = Path(__file__).resolve().parent
    smoke = json.loads((here / "smoke-report.json").read_text())
    expected = {(dbms, clients) for dbms in bench.DBMS for clients in (1, 32, 64, 128)}
    assert smoke["complete"] and {(r["dbms"], r["clients"]) for r in smoke["runs"]} == expected
    checked_runs = []
    for run in smoke["runs"]:
        directory = here / run["directory"]
        manifest = json.loads((directory / "run.json").read_text())
        assert manifest["status"] == "PASS" and manifest["kind"] == "smoke"
        assert run["native_count_matches"] and all(run["consistency_checks"].values())
        assert manifest["jar_sha256"] == bench.sha(here / "runtime" / f"benchbase-{run['dbms']}-instrumented.jar")
        assert json.loads((directory / "source-fingerprint-before.json").read_text()) == json.loads((directory / "source-fingerprint-after.json").read_text())
        assert (directory / "resources.jsonl").stat().st_size > 0
        if run["dbms"] == "mysql":
            journal = json.loads((directory / "mysql-observer-permissions.json").read_text())
            assert sorted(journal["temporarily_added_select"]) == sorted(journal["revoked"])
            assert not journal["cleanup_errors"]
        checked_runs.append({"dbms": run["dbms"], "clients": run["clients"],
                             "sessions": run["max_observed_sessions"], "status": "PASS"})
    cleanup = json.loads((here / "last-cleanup.json").read_text())
    assert not cleanup["errors"]
    targets = {}
    for dbms in bench.DBMS:
        bench.ready(dbms)
        baseline = bench.ownership(dbms)
        source = bench.fingerprint(dbms, "tpcc")
        target = bench.fingerprint(dbms, bench.TARGET)
        assert source == baseline["fingerprint"] == target
        targets[dbms] = {"source_unchanged": True, "benchmark_at_baseline": True,
                         "row_counts": target["counts"]}
    grants = bench.admin("mysql", "SHOW GRANTS FOR 'labuser'@'%';")
    assert "performance_schema" not in grants.lower(), "Observer privilege remains; inspect permission journals"
    assert not list((here / "results").glob("*/formal-*")), "Formal runs already exist; distinguish step 17"
    # Capture final, actual environment after both services have returned.
    bench.save(here / "environment.json", bench.environment())
    report = {"verified_utc": datetime.now(timezone.utc).isoformat(), "status": "PASS",
              "smoke_runs": checked_runs, "databases": targets,
              "temporary_metadata_permissions_revoked": True, "formal_benchmark_started": False,
              "limitations": ["Short smoke tests do not establish sustained performance.",
                              "Baseline fingerprints and four invariants are not a full TPC-C audit."]}
    bench.save(here / "step16-verification.json", report)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
