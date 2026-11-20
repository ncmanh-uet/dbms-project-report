"""Offline aggregation of a complete E3-advanced formal report (never runs SQL)."""
import argparse
from collections import defaultdict
import csv
import hashlib
import json
from pathlib import Path
import statistics

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
METRICS = ["successful_tps", "p50_ms", "p95_ms", "p99_ms", "completion_pct", "retry_pct",
           "conflict_pct", "exhausted_pct", "attempts_per_operation", "drain_seconds",
           "generator_cpu_cores_mean", "generator_rss_bytes_mean", "lock_waiters_mean", "db_cpu_quota_pct_mean"]


def save_csv(path, rows):
    with path.open("w", newline="", encoding="utf-8") as f:
        writer=csv.DictWriter(f,fieldnames=list(rows[0]))
        writer.writeheader(); writer.writerows(rows)


def analyze(run):
    report=json.loads((run/"report.json").read_text())
    assert report["mode"]=="formal" and report["status"]=="PASS"
    assert all(v is True for v in report["source_unchanged"].values())
    trials=report["results"]
    assert len(trials)==report["protocol"]["total_formal_trials"] and all(r["status"]=="PASS" for r in trials)
    groups=defaultdict(list)
    hashes={}
    totals=defaultdict(int)
    for r in trials:
        directory=ROOT/r["directory"]
        cleanup=json.loads((directory/"cleanup.json").read_text())
        assert cleanup["dropped"] and cleanup["source_unchanged"] and cleanup["table_list_unchanged"]
        assert r["integrity"]["rows_checked"]==100000 and not r["integrity"]["mismatches"]
        measured=[]
        for p in directory.glob("worker-*.csv"):
            hashes[str(p.relative_to(ROOT))]=hashlib.sha256(p.read_bytes()).hexdigest()
            with p.open(encoding="utf-8") as f:
                measured.extend(x for x in csv.DictReader(f) if x["phase"]=="measure")
        ok=[x for x in measured if x["status"]=="OK"]
        assert len(measured)==r["logical_operations"] and len(ok)==r["successes"]
        assert abs(len(ok)/r["elapsed_seconds"]-r["successful_tps"])<1e-8
        assert abs(float(np.percentile([float(x["latency_ms"]) for x in ok],95))-r["p95_ms"])<1e-8
        assert sum(int(x["attempts"]) for x in measured)==r["attempts"]
        assert sum(int(x["conflicts"]) for x in measured)==r["version_conflicts"]
        assert sum(int(x["attempts"])>1 for x in measured)==r["retried_operations"]
        assert all(int(x["attempts"])<=9 for x in measured)
        groups[(r["dbms"],r["distribution"],r["method"],r["clients"])].append(r)
        for key in ("logical_operations","successes","attempts","version_conflicts","exhausted","deadlocks","timeouts"):
            totals[key]+=r[key]
    summary=[]
    for (db,dist,method,clients),rs in sorted(groups.items()):
        assert sorted(r["repetition"] for r in rs)==list(range(1,report["protocol"]["repetitions"]+1))
        row=dict(dbms=db,distribution=dist,method=method,clients=clients,repetitions=report["protocol"]["repetitions"])
        for metric in METRICS:
            values=[r[metric] for r in rs]
            for label,fn in (("mean",statistics.mean),("min",min),("max",max)):
                row[metric+"_"+label]=fn(values)
            row[metric+"_stdev"]=statistics.stdev(values) if len(values)>1 else 0.0
        for key in ("logical_operations","successes","attempts","version_conflicts","exhausted","deadlocks","timeouts"):
            row[key+"_sum"]=sum(r[key] for r in rs)
        summary.append(row)
    out=run/"analysis"; out.mkdir(exist_ok=True)
    save_csv(out/"summary.csv",summary)
    save_csv(out/"trials.csv",[{k:r[k] for k in ["dbms","distribution","method","clients","repetition","directory"]+METRICS+["logical_operations","successes","attempts","version_conflicts","exhausted","deadlocks","timeouts"]} for r in trials])
    colors={"pessimistic":"#e58727","occ":"#278b57"}
    for metric,title,filename,log_y in [
        ("successful_tps","Successful logical operations / second","01_tps",False),
        ("p95_ms","Successful operation p95 latency (ms), retries included","02_p95",True),
        ("retry_pct","Operations with retry (%)","03_retry",False),
        ("exhausted_pct","Operations exhausting retry budget (%)","04_exhausted",False)]:
        fig,axes=plt.subplots(2,3,figsize=(14,8),sharex=True,constrained_layout=True)
        for i,db in enumerate(["postgres","mysql"]):
            for j,dist in enumerate(["uniform100k","shared100","hot1"]):
                ax=axes[i,j]
                for method in colors:
                    rs=sorted([r for r in summary if r["dbms"]==db and r["distribution"]==dist and r["method"]==method],key=lambda r:r["clients"])
                    x=[r["clients"] for r in rs]; y=[r[metric+"_mean"] for r in rs]
                    ax.errorbar(x,y,yerr=[[r[metric+"_mean"]-r[metric+"_min"] for r in rs],[r[metric+"_max"]-r[metric+"_mean"] for r in rs]],marker="o",capsize=3,label=method,color=colors[method])
                ax.set_title(f"{db} / {dist}"); ax.set_xscale("log",base=2)
                if log_y: ax.set_yscale("log")
                else: ax.set_ylim(bottom=0)
                ax.set_xticks([1,8,32,128],["1","8","32","128"])
                ax.set_xlabel("Concurrent clients"); ax.grid(alpha=.2)
        axes[0,0].legend(fontsize=9)
        fig.suptitle(f"E3-advanced — {title}\n{report['protocol']['repetitions']} run per configuration",fontsize=12)
        fig.savefig(out/(filename+".png"),dpi=170)
        fig.savefig(out/(filename+".svg")); plt.close(fig)
    validation={"status":"PASS","trials":len(trials),"configurations":len(summary),"totals":dict(totals),"raw_request_hashes":hashes,
                "definitions":{"p95":"mean of per-run p95, not pooled","range":"min-max, not confidence interval","tps":"successes / admission plus drain seconds","stock":"full 100000 row clone per trial"}}
    (out/"validation.json").write_text(json.dumps(validation,indent=2),encoding="utf-8")
    print(json.dumps({"output":str(out),"trials":len(trials),"groups":len(summary),"totals":dict(totals)},indent=2))


if __name__=="__main__":
    p=argparse.ArgumentParser(); p.add_argument("run",type=Path)
    analyze(p.parse_args().run)
