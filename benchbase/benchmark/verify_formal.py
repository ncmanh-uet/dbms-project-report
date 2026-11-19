"""Audit the completed 42-run formal benchmark without rerunning workloads."""
import csv, json, math, sys
from collections import Counter
from datetime import datetime, timezone, timedelta
from pathlib import Path
from xml.etree import ElementTree as ET
sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_benchmark as b


def main():
    h=b.HERE
    report=json.loads((h/"formal-report.json").read_text())
    before=json.loads((h/"step17-environment-before.json").read_text())
    assert report["complete"] and len(report["runs"])==42
    assert b.PROTOCOL==before["protocol"]
    expected={(db,c,r) for db in b.DBMS for c in b.PROTOCOL["clients"] for r in range(1,4)}
    seen=set()
    totals=Counter()
    abort_by_type=Counter()
    details=[]
    for entry in report["runs"]:
        d=h/entry["directory"]
        manifest=json.loads((d/"run.json").read_text())
        metrics=json.loads((d/"metrics.json").read_text())
        key=(manifest["dbms"],manifest["clients"],manifest["repetition"])
        assert key in expected and key not in seen, key
        seen.add(key)
        assert manifest["kind"]=="formal" and manifest["status"]=="PASS"
        assert manifest["warmup_seconds"]==15 and manifest["measure_seconds"]==60
        assert manifest["protocol"]==b.PROTOCOL
        assert manifest["workload_sha256"]==b.sha(d/"workload.xml")
        assert manifest["jar_sha256"]==b.sha(h/"runtime"/f"benchbase-{key[0]}-instrumented.jar")
        assert manifest["jar_sha256"]==before["instrumentation"]["profiles"][key[0]]["instrumented_jar_sha256"]
        baseline=json.loads((h/"baseline"/f"{key[0]}-ownership.json").read_text())
        assert manifest["baseline_sha256"]==baseline["archive_sha256"]
        assert json.loads((d/"source-fingerprint-before.json").read_text())==baseline["fingerprint"]
        assert json.loads((d/"source-fingerprint-after.json").read_text())==baseline["fingerprint"]
        assert manifest["generator"]["nano_cpus"]==4_000_000_000
        assert manifest["generator"]["memory_bytes"]==2**30
        root=ET.parse(d/"workload.xml").getroot()
        assert root.findtext("terminals")==str(key[1])
        assert root.findtext("isolation")=="TRANSACTION_READ_COMMITTED"
        assert root.findtext("works/work/time")=="60"
        assert root.findtext("works/work/warmup")=="15"
        assert root.findtext("works/work/weights")=="45,43,4,4,4"
        assert "/tpcc_bench?" in root.findtext("url")
        state=json.loads((d/"container-state.json").read_text())
        assert state["ExitCode"]==0 and not state["OOMKilled"]
        native_paths=list((d/"native").glob("*.summary.json"))
        assert len(native_paths)==1
        native=json.loads(native_paths[0].read_text())
        elapsed=native["Elapsed Time (nanoseconds)"]/1e9
        assert elapsed>=59
        counts=Counter()
        run_aborts=Counter()
        requests=reexecutions=retry_events=0
        success_latencies=[]
        for path in (d/"requests").glob("worker-*.csv"):
            with path.open(newline="",encoding="utf-8") as stream:
                for row in csv.DictReader(stream):
                    requests+=1
                    counts[row["status"]]+=1
                    attempts=int(row["attempts"])
                    assert 1<=attempts<=3
                    assert int(row["end_ns"])>=int(row["start_ns"])
                    if row["status"]=="SUCCESS":
                        success_latencies.append((int(row["end_ns"])-int(row["start_ns"]))/1e6)
                    if row["status"]=="USER_ABORTED":
                        abort_by_type[row["transaction_type"]]+=1
                        run_aborts[row["transaction_type"]]+=1
                    reexecutions+=attempts-1
                    retry_events+=int(row["retry_events"])
        assert requests==native["Measured Requests"]==metrics["measured_requests"]==entry["measured_requests"]
        assert counts==Counter(metrics["status_counts"])
        assert math.isclose(counts["SUCCESS"]/elapsed,metrics["successful_tps"],rel_tol=1e-12)
        assert math.isclose(b.quantile(success_latencies,.95),metrics["latency_success_ms"]["p95"],rel_tol=1e-12)
        assert reexecutions==metrics["reexecutions_started"] and retry_events==metrics["retry_events"]
        assert metrics["native_count_matches"] and all(metrics["consistency_checks"].values())
        with (d/"db-samples.csv").open(newline="",encoding="utf-8") as stream:
            samples=[s for s in csv.DictReader(stream) if s["phase"]=="measure"]
        assert samples and max(int(s["sessions"]) for s in samples)>=key[1]
        resources=[json.loads(line) for line in (d/"resources.jsonl").read_text().splitlines() if line.strip()]
        measured_resources=[r for r in resources if r["phase"]=="measure"]
        assert any(r["stats"]["Name"].startswith("labbench-") for r in measured_resources)
        assert any(not r["stats"]["Name"].startswith("labbench-") for r in measured_resources)
        if key[0]=="mysql":
            journal=json.loads((d/"mysql-observer-permissions.json").read_text())
            assert sorted(journal["temporarily_added_select"])==sorted(journal["revoked"])
            assert not journal["cleanup_errors"]
        totals.update(measured_requests=requests,successful_requests=counts["SUCCESS"],
                      benchmark_user_aborts=counts["USER_ABORTED"],failed_requests=metrics["failed_requests"],
                      requests_with_retry=metrics["requests_with_retry"],reexecutions_started=reexecutions)
        details.append(dict(dbms=key[0],clients=key[1],repetition=key[2],status="PASS",
                            measurement_seconds=elapsed,user_aborted_by_transaction=dict(run_aborts),
                            delivery_conflict_aborts=run_aborts["4"],measurement_db_samples=len(samples),
                            measurement_resource_samples=len(measured_resources),directory=entry["directory"]))
    assert seen==expected
    cleanup=json.loads((h/"last-cleanup.json").read_text())
    assert not cleanup["errors"]
    latest_start=max(datetime.fromisoformat(json.loads((h/x["directory"]/"run.json").read_text())["started_utc"]) for x in report["runs"])
    assert datetime.fromisoformat(cleanup["utc"])>latest_start+timedelta(seconds=75)
    databases={}
    for db in b.DBMS:
        b.ready(db)
        baseline=b.ownership(db)
        assert b.sha(b.baseline_path(db))==baseline["archive_sha256"]
        assert b.fingerprint(db,"tpcc")==baseline["fingerprint"]
        assert b.fingerprint(db,b.TARGET)==baseline["fingerprint"]
        databases[db]={"source_unchanged":True,"benchmark_at_baseline":True}
    assert "performance_schema" not in b.admin("mysql","SHOW GRANTS FOR 'labuser'@'%';").lower()
    after=json.loads(json.dumps(b.environment(),default=str))
    b.save(h/"step17-environment-after.json",after)
    for db in b.DBMS:
        assert after["services"][db]==before["services"][db], db
    verification={"verified_utc":datetime.now(timezone.utc).isoformat(),"status":"PASS",
                  "expected_runs":42,"verified_runs":len(details),"totals":dict(totals),
                  "user_aborted_by_transaction":dict(abort_by_type),
                  "neworder_intentional_aborts":abort_by_type["1"],"delivery_conflict_aborts":abort_by_type["4"],
                  "outcome_classification_note":"Legacy expected_user_aborts in per-run metrics contains all USER_ABORTED. Delivery USER_ABORTED is a concurrency conflict, not an intentionally invalid NewOrder. Raw files and original run metrics are preserved.",
                  "protocol_unchanged":True,"all_request_counts_and_success_p95_rechecked":True,
                  "all_four_business_invariants_passed":True,"databases":databases,
                  "temporary_mysql_metadata_permissions_revoked":True,"cleanup":cleanup,
                  "runs":details,"notes":["Keep all three repetitions, including slower runs.",
                  "PASS verifies execution, evidence and selected invariants; it does not establish statistical significance or full TPC-C compliance.",
                  "Observed performance variation is retained for the later analysis."]}
    b.save(h/"step17-verification.json",verification)
    print(json.dumps({k:v for k,v in verification.items() if k!="runs"},indent=2))


if __name__ == "__main__":
    main()

