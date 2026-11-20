"""E3-advanced: full-stock, controlled pessimistic/OCC comparison.

Run with the project venv. Uses a uniquely owned table in tpcc, never writes stock.
No E6 baseline/results are changed. Formal execution is resumable by --resume.
"""
import argparse
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from contextlib import closing, nullcontext
import csv
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import multiprocessing as mp
from pathlib import Path
from random import Random
import shutil
import subprocess
import sys
from threading import Event, Thread
import time
from uuid import uuid4

import numpy as np
import psutil

from e1_mvcc import ROOT, connect
from e2_support import call, mysql_observer_access, save_json

HERE = Path(__file__).resolve().parent
PROTOCOL_PATH = HERE / "e3_advanced_protocol.json"
PROTOCOL = json.loads(PROTOCOL_PATH.read_text(encoding="utf-8"))
COMPOSE = ["docker", "compose", "-f", str(ROOT / "compose.yaml"), "-f",
           str(ROOT / "benchbase/benchmark/compose.override.yaml")]
FIELDS = ["phase", "client", "operation", "item", "status", "attempts", "conflicts",
          "deadlocks", "timeouts", "start_s", "end_s", "latency_ms", "error_code"]


def command(args, timeout=120):
    r = subprocess.run(args, cwd=ROOT, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=timeout)
    if r.returncode:
        raise RuntimeError(f"Command failed: {args[:6]}: {r.stderr[-1500:]}")
    return r.stdout.strip()


def dc(*args):
    return command(COMPOSE + list(args))


class Log:
    def record(self, *args, **kw):
        print(*args, json.dumps(kw, default=str), flush=True)


def setup(c, db, client=None):
    if db == "postgres":
        call(c, "SET SESSION CHARACTERISTICS AS TRANSACTION ISOLATION LEVEL READ COMMITTED")
        call(c, "SET statement_timeout='15s'")
        call(c, "SET lock_timeout='5s'")
        call(c, "SET application_name='e3_advanced'")
        row = call(c, "SELECT pg_backend_pid(),current_setting('transaction_isolation')")[0][0]
    else:
        call(c, "SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED")
        call(c, "SET SESSION innodb_lock_wait_timeout=5")
        call(c, "SET SESSION lock_wait_timeout=5")
        call(c, "SET SESSION max_execution_time=15000")
        row = call(c, "SELECT CONNECTION_ID(),@@session.transaction_isolation")[0][0]
    if row[1].lower().replace("-", " ") != "read committed":
        raise RuntimeError("Unexpected isolation")
    return int(row[0])


def ready(db):
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        try:
            with closing(connect(db)) as c:
                call(c, "SELECT 1")
            return
        except Exception:
            time.sleep(1)
    raise RuntimeError(f"{db} unavailable")


def stock_hash(c, table="stock"):
    h = hashlib.sha256()
    cur = c.cursor()
    cur.execute(f"SELECT * FROM {table} ORDER BY s_w_id,s_i_id")
    count = 0
    while True:
        batch = cur.fetchmany(1000)
        if not batch:
            break
        for row in batch:
            h.update(json.dumps(row, default=str, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
            h.update(b"\n")
            count += 1
    cur.close()
    return {"rows": count, "sha256_ordered_values": h.hexdigest()}


def fingerprint(c):
    tables = ("warehouse", "district", "customer", "history", "item", "stock", "oorder", "new_order", "order_line")
    return {"counts": {t: int(call(c, f"SELECT COUNT(*) FROM {t}")[0][0][0]) for t in tables},
            "stock": stock_hash(c)}


def tables(c, db):
    schema = "'public'" if db == "postgres" else "DATABASE()"
    return [r[0] for r in call(c, f"SELECT table_name FROM information_schema.tables WHERE table_schema={schema} AND table_type='BASE TABLE' ORDER BY table_name")[0]]


def schema(c, db, table):
    if db == "postgres":
        return {"columns": call(c, "SELECT column_name,data_type,is_nullable,column_default FROM information_schema.columns WHERE table_schema='public' AND table_name=%s ORDER BY ordinal_position", (table,))[0],
                "indexes": call(c, "SELECT indexdef FROM pg_indexes WHERE schemaname='public' AND tablename=%s ORDER BY indexname", (table,))[0]}
    return {"ddl": call(c, f"SHOW CREATE TABLE {table}")[0]}


def init_shard(q, gates, clock, connection_slots):
    global SHARED
    SHARED = (q, gates, clock, connection_slots)


def worker_shard(ids, db, table, case, directory):
    """Four host processes avoid a single interpreter for all 128 connections."""
    q, gates, clock, connection_slots = SHARED
    def worker(cid):
        c = None
        try:
            # Bound simultaneous authentication only; no admission limit during workload.
            with connection_slots:
                c = connect(db)
                pid = setup(c, db, cid)
            target_rng = Random(PROTOCOL["seed"] + case["repetition"] * 10000 + cid)
            backoff_rng = Random(PROTOCOL["seed"] + case["repetition"] * 10000 + cid + 1000000)
            read = f"SELECT s_ytd,version FROM {table} WHERE s_w_id=1 AND s_i_id=%s"
            update = f"UPDATE {table} SET s_ytd=%s WHERE s_w_id=1 AND s_i_id=%s"
            cas = f"UPDATE {table} SET s_ytd=%s,version=version+1 WHERE s_w_id=1 AND s_i_id=%s AND version=%s"
            atomic = f"UPDATE {table} SET s_ytd=s_ytd+1 WHERE s_w_id=1 AND s_i_id=%s"
            with (Path(directory) / f"worker-{cid}.csv").open("w", newline="", encoding="utf-8", buffering=262144) as f:
                writer = csv.writer(f)
                writer.writerow(FIELDS)
                q.put(("ready", cid, pid))
                for phase in ("warmup", "measure"):
                    if not gates[phase].wait(180):
                        raise RuntimeError("Phase start timeout")
                    index = 0 if phase == "warmup" else 2
                    started, seconds = clock[index], clock[index+1]
                    deadline = started + seconds
                    op = 0
                    last_end = started
                    while time.perf_counter() < deadline:
                        item = target_rng.randint(1, PROTOCOL["distributions"][case["distribution"]])
                        began = time.perf_counter()
                        status, conflicts, deadlocks, timeouts, code = "EXHAUSTED", 0, 0, 0, ""
                        for attempt in range(PROTOCOL["max_retries"] + 1):
                            stage = "BEGIN"
                            retry = False
                            try:
                                call(c, "BEGIN" if db == "postgres" else "START TRANSACTION")
                                if case["method"] == "atomic":
                                    changed = call(c, atomic, (item,))[0]
                                else:
                                    row = call(c, read + (" FOR UPDATE" if case["method"] == "pessimistic" else ""), (item,))[0]
                                    if len(row) != 1:
                                        raise RuntimeError("Missing target row")
                                    old, version = row[0]
                                    value = Decimal(str(old)) + 1
                                    changed = call(c, cas if case["method"] == "occ" else update,
                                        (value, item, version) if case["method"] == "occ" else (value, item))[0]
                                if changed == 0 and case["method"] == "occ":
                                    c.rollback()
                                    conflicts += 1
                                    retry = True
                                elif changed == 1:
                                    stage = "COMMIT"
                                    call(c, "COMMIT")
                                    status = "OK"
                                else:
                                    raise RuntimeError(f"Unexpected affected rows {changed}")
                            except Exception as e:
                                code = str(getattr(e, "sqlstate", "") or getattr(e, "errno", "") or type(e).__name__)
                                errno = getattr(e, "errno", None)
                                sqlstate = getattr(e, "sqlstate", None)
                                if stage == "COMMIT":
                                    status = "UNKNOWN_COMMIT"
                                elif sqlstate == "40P01" or errno == 1213:
                                    deadlocks += 1
                                    retry = True
                                elif sqlstate == "55P03" or errno == 1205:
                                    timeouts += 1
                                    status = "TIMEOUT"
                                else:
                                    status = "ERROR"
                                try:
                                    c.rollback()
                                except Exception:
                                    status = "ERROR"
                                    retry = False
                            if not retry:
                                break
                            if attempt < PROTOCOL["max_retries"]:
                                time.sleep(min(.001 * 2**attempt, .020) * backoff_rng.uniform(.5, 1.5))
                        ended = time.perf_counter()
                        last_end = ended
                        writer.writerow([phase, cid, op, item, status, attempt+1, conflicts, deadlocks,
                                         timeouts, began-started, ended-started, (ended-began)*1000, code])
                        op += 1
                        if status in ("ERROR", "UNKNOWN_COMMIT"):
                            f.flush()
                            raise RuntimeError(f"Fatal operation {status}, code {code}")
                    f.flush()
                    q.put((phase, cid, last_end))
            return cid
        except BaseException as e:
            save_json(Path(directory)/f"worker-{cid}-error.json", {"client":cid,"error":repr(e)})
            q.put(("error", cid, repr(e)))
            raise
        finally:
            if c is not None:
                c.close()
    with ThreadPoolExecutor(max_workers=len(ids)) as pool:
        return list(pool.map(worker, ids))


def sample(db, container, directory, stop, phase_state):
    errors = []
    root = psutil.Process()
    previous = {}
    previous_time = time.perf_counter()
    c = None
    try:
        c = connect(db)
        with (directory / "samples.jsonl").open("w", encoding="utf-8") as f:
            while not stop.is_set():
                tick = time.perf_counter()
                entry = {"monotonic": tick, "phase": phase_state["phase"]}
                try:
                    cpu, rss = 0., 0
                    for proc in [root] + root.children(recursive=True):
                        try:
                            times = proc.cpu_times()
                            total = times.user + times.system
                            if proc.pid in previous:
                                cpu += max(0, total - previous[proc.pid])
                            previous[proc.pid] = total
                            rss += proc.memory_info().rss
                        except psutil.Error:
                            pass
                    entry.update(generator_cpu_cores=cpu/max(tick-previous_time, .001), generator_rss_bytes=rss)
                    previous_time = tick
                    if db == "postgres":
                        active, waiting = call(c, "SELECT COUNT(*),COUNT(*) FILTER (WHERE wait_event_type='Lock') FROM pg_stat_activity WHERE application_name='e3_advanced'")[0][0]
                    else:
                        active = call(c, "SELECT COUNT(*) FROM performance_schema.threads WHERE PROCESSLIST_DB=DATABASE() AND PROCESSLIST_USER=SUBSTRING_INDEX(CURRENT_USER(),'@',1)")[0][0][0]
                        waiting = call(c, "SELECT COUNT(DISTINCT REQUESTING_THREAD_ID) FROM performance_schema.data_lock_waits")[0][0][0]
                    entry.update(sessions=int(active), lock_waiters=int(waiting))
                    entry["docker"] = json.loads(command(["docker", "stats", "--no-stream", "--format", "{{json .}}", container], timeout=15))
                except Exception as e:
                    errors.append(repr(e))
                    entry["error"] = repr(e)
                f.write(json.dumps(entry) + "\n")
                f.flush()
                stop.wait(max(0, 2-(time.perf_counter()-tick)))
    except Exception as e:
        errors.append(repr(e))
    finally:
        if c:
            c.close()
        save_json(directory / "sampler.json", {"errors": errors})


def summarize(directory, case, elapsed, baseline, actual):
    successes, versions = Counter(), Counter()
    measured = []
    for path in sorted(directory.glob("worker-*.csv")):
        with path.open(encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row["status"] == "OK":
                    successes[int(row["item"])] += 1
                if row["phase"] == "measure":
                    measured.append(row)
    mismatches = []
    for item, old in baseline.items():
        expected = (old + successes[item], 1 + (successes[item] if case["method"] == "occ" else 0))
        if actual.get(item) != expected:
            mismatches.append({"item": item, "expected": expected, "actual": actual.get(item)})
    if len(actual) != len(baseline):
        mismatches.append({"row_count": len(actual)})
    ok = [r for r in measured if r["status"] == "OK"]
    latencies = [float(r["latency_ms"]) for r in ok]
    outcomes = Counter(r["status"] for r in measured)
    n = len(measured)
    attempts = sum(int(r["attempts"]) for r in measured)
    conflicts = sum(int(r["conflicts"]) for r in measured)
    retried = sum(int(r["attempts"]) > 1 for r in measured)
    result = dict(**case, status="PASS" if not mismatches and ok and not outcomes["ERROR"] and not outcomes["UNKNOWN_COMMIT"] else "FAIL",
        directory=str(directory.relative_to(ROOT)), elapsed_seconds=elapsed,
        drain_seconds=max(0, elapsed-case["measure_seconds"]), logical_operations=n,
        successes=len(ok), successful_tps=len(ok)/elapsed, outcomes=dict(outcomes),
        attempts=attempts, version_conflicts=conflicts, conflict_pct=100*conflicts/attempts if attempts else 0,
        retried_operations=retried, retry_pct=100*retried/n if n else 0,
        attempts_per_operation=attempts/n if n else 0, completion_pct=100*len(ok)/n if n else 0,
        exhausted=outcomes["EXHAUSTED"], exhausted_pct=100*outcomes["EXHAUSTED"]/n if n else 0,
        deadlocks=sum(int(r["deadlocks"]) for r in measured), timeouts=sum(int(r["timeouts"]) for r in measured),
        p50_ms=float(np.percentile(latencies,50)) if ok else None,
        p95_ms=float(np.percentile(latencies,95)) if ok else None,
        p99_ms=float(np.percentile(latencies,99)) if ok else None,
        all_outcomes_p95_ms=float(np.percentile([float(r["latency_ms"]) for r in measured],95)) if n else None,
        integrity={"rows_checked": len(actual), "successes_including_warmup": sum(successes.values()), "mismatches": mismatches[:20]})
    save_json(directory / "confirmed-increments.json", successes)
    samples = [json.loads(line) for line in (directory / "samples.jsonl").read_text().splitlines()]
    samples = [r for r in samples if r["phase"] == "measure" and "error" not in r]
    result["measure_samples"] = len(samples)
    for key in ("generator_cpu_cores", "generator_rss_bytes", "lock_waiters"):
        result[key + "_mean"] = float(np.mean([r[key] for r in samples])) if samples else None
    result["db_cpu_quota_pct_mean"] = float(np.mean([float(r["docker"]["CPUPerc"].strip("%"))/2 for r in samples])) if samples else None
    result["sampler_errors"] = json.loads((directory / "sampler.json").read_text())["errors"]
    if result["sampler_errors"] or not samples:
        result["status"] = "FAIL"
    save_json(directory / "summary.json", result)
    return result


def trial(db, case, directory, warmup, seconds, container):
    directory.mkdir(parents=True)
    table = "e3adv_" + uuid4().hex[:16]
    c = connect(db)
    created = False
    result = None
    save_json(directory / "ownership.json", {"dbms": db, "table": table, "source": "stock", "created": False})
    try:
        original_tables = tables(c, db)
        source = stock_hash(c)
        if source["rows"] != 100000:
            raise RuntimeError("Expected exactly 100000 stock rows")
        baseline = {int(i): Decimal(str(v)) for i,v in call(c, "SELECT s_i_id,s_ytd FROM stock WHERE s_w_id=1")[0]}
        if set(baseline) != set(range(1,100001)):
            raise RuntimeError("Expected item IDs 1..100000 in warehouse 1")
        if db == "postgres":
            call(c, f"CREATE TABLE {table} (LIKE stock INCLUDING ALL)")
        else:
            call(c, f"CREATE TABLE {table} LIKE stock")
        created = True
        save_json(directory / "ownership.json", {"dbms": db, "table": table, "source": "stock", "created": True})
        count = call(c, f"INSERT INTO {table} SELECT * FROM stock")[0]
        if count != 100000 or stock_hash(c,table) != source:
            raise RuntimeError("Full stock clone mismatch")
        call(c, f"ALTER TABLE {table} ADD COLUMN version BIGINT NOT NULL DEFAULT 1")
        call(c, f"ANALYZE {table}" if db == "postgres" else f"ANALYZE TABLE {table}")
        save_json(directory / "baseline.json", {"source_hash": source, "source_schema": schema(c,db,"stock"), "fixture_schema": schema(c,db,table)})
        context=mp.get_context("spawn")
        q=context.Queue()
        gates={p:context.Event() for p in ("warmup","measure")}
        clock=context.Array("d",4)
        connection_slots=context.BoundedSemaphore(8)
        try:
            processes = min(PROTOCOL["generator_processes"], case["clients"])
            with ProcessPoolExecutor(max_workers=processes,mp_context=context,initializer=init_shard,
                                     initargs=(q,gates,clock,connection_slots)) as pool:
                futures = [pool.submit(worker_shard, list(range(shard,case["clients"],processes)), db, table, case, str(directory)) for shard in range(processes)]
                stop, phase = Event(), {"phase":"setup"}
                sampler = None
                try:
                    pids=[]
                    setup_deadline=time.perf_counter()+180
                    for _ in range(case["clients"]):
                        event,cid,value=q.get(timeout=max(.1,setup_deadline-time.perf_counter()))
                        if event!="ready": raise RuntimeError(str((event,cid,value)))
                        pids.append(value)
                    if len(set(pids)) != case["clients"]: raise RuntimeError("Duplicate sessions")
                    save_json(directory / "sessions.json", pids)
                    sampler = Thread(target=sample,args=(db,container,directory,stop,phase),daemon=True)
                    sampler.start()
                    for label,duration in (("warmup",warmup),("measure",seconds)):
                        started=time.perf_counter()
                        index=0 if label=="warmup" else 2
                        clock[index],clock[index+1]=started,duration
                        phase["phase"]=label
                        gates[label].set()
                        endings=[]
                        phase_deadline=started+duration+120
                        for _ in range(case["clients"]):
                            event,cid,value=q.get(timeout=max(.1,phase_deadline-time.perf_counter()))
                            if event!=label: raise RuntimeError(str((event,cid,value)))
                            endings.append(value)
                        elapsed=max(duration,max(endings)-started)
                        phase["phase"]="drain_complete"
                    for f in futures: f.result(timeout=30)
                except BaseException as e:
                    save_json(directory/"failure.json", {"error":repr(e),"ready_sessions":len(pids),"phase":phase["phase"]})
                    pool.terminate_workers()
                    raise
                finally:
                    for gate in gates.values(): gate.set()
                    stop.set()
                    if sampler: sampler.join(timeout=30)
        finally:
            q.close()
            q.join_thread()
        actual={int(i):(Decimal(str(v)),int(ver)) for i,v,ver in call(c,f"SELECT s_i_id,s_ytd,version FROM {table} WHERE s_w_id=1")[0]}
        result=summarize(directory,case,elapsed,baseline,actual)
        return result
    finally:
        cleanup={"table":table,"dropped":False}
        try:
            c.rollback()
            if created:
                call(c,f"DROP TABLE {table}")
                cleanup["dropped"]=True
            cleanup["source_unchanged"]=stock_hash(c)==source if "source" in locals() else None
            cleanup["table_list_unchanged"]=tables(c,db)==original_tables if "original_tables" in locals() else None
            save_json(directory / "cleanup.json",cleanup)
            if result is not None and not all(cleanup[k] for k in ("dropped","source_unchanged","table_list_unchanged")):
                result["status"]="FAIL"
                save_json(directory / "summary.json",result)
        finally:
            c.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode",choices=["smoke","formal","plan"])
    parser.add_argument("--resume",type=Path)
    args=parser.parse_args()
    if args.mode=="plan":
        print(json.dumps(PROTOCOL,indent=2)); return
    run=args.resume or HERE/"e3_advanced_logs"/(args.mode+"-"+datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    run.mkdir(parents=True,exist_ok=True)
    manifest=run/"report.json"
    report=json.loads(manifest.read_text()) if manifest.exists() else {"mode":args.mode,"protocol":PROTOCOL,"results":[],"status":"RUNNING"}
    if report["mode"]!=args.mode or report["protocol"]!=PROTOCOL:
        raise RuntimeError("Resume protocol mismatch")
    source_sha=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    if report.get("runner_sha256",source_sha)!=source_sha:
        raise RuntimeError("Runner changed; start a new formal run")
    report["runner_sha256"]=source_sha
    for dependency in ("e3_advanced.py", "e3_advanced_protocol.json", "e1_mvcc.py", "e2_support.py"):
        snapshot=run/"code"/dependency
        snapshot.parent.mkdir(exist_ok=True)
        data=(HERE/dependency).read_bytes()
        if snapshot.exists() and snapshot.read_bytes()!=data:
            raise RuntimeError(f"Resume dependency changed: {dependency}")
        snapshot.write_bytes(data)
    e6_files=[ROOT/"benchbase/benchmark"/p for p in ("protocol.json","formal-report.json","analysis/summary.csv","analysis/trials.csv")]
    e6_hashes={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in e6_files}
    if report.get("e6_hashes_before",e6_hashes)!=e6_hashes:
        raise RuntimeError("E6 artifacts changed since initial run")
    report["e6_hashes_before"]=e6_hashes
    # Preserve failed attempts separately; only one PASS per scheduled case enters analysis.
    report.setdefault("failed_attempts",[]).extend(r for r in report["results"] if r["status"]!="PASS")
    report["results"]=[r for r in report["results"] if r["status"]=="PASS"]
    report["status"]="RUNNING"
    report.pop("error",None)
    save_json(manifest,report)
    print(f"RUN_DIRECTORY={run}",flush=True)
    warmup,seconds,repetitions,clients=(2,4,1,[1,8,128]) if args.mode=="smoke" else (PROTOCOL["warmup_seconds"],PROTOCOL["measure_seconds"],PROTOCOL["repetitions"],PROTOCOL["clients"])
    schedule=[]
    for rep in range(1,repetitions+1):
        engines=PROTOCOL["engines"] if rep%2 else list(reversed(PROTOCOL["engines"]))
        for db in engines:
            methods=PROTOCOL["methods"][rep-1:]+PROTOCOL["methods"][:rep-1]
            for distribution in PROTOCOL["distributions"]:
                for n in clients:
                    for method in methods:
                        if args.mode=="smoke" and not ((distribution=="uniform100k" and n==1) or (distribution=="hot1" and n in (8,128))): continue
                        schedule.append(dict(dbms=db,method=method,distribution=distribution,clients=n,repetition=rep,warmup_seconds=warmup,measure_seconds=seconds))
    save_json(run/"schedule.json",schedule)
    def key(case): return f"{case['dbms']}-{case['method']}-{case['distribution']}-c{case['clients']}-r{case['repetition']}"
    done={key(r) for r in report["results"] if r["status"]=="PASS"}
    current=None
    initial={}
    # Temporary per-thread request only: no persistent power settings or display changes.
    if sys.platform=="win32":
        import ctypes
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000001)
    try:
        dc("up","-d","postgres","mysql")
        for db in PROTOCOL["engines"]:
            ready(db)
            container=dc("ps","-q",db)
            info=json.loads(command(["docker","inspect",container]))[0]
            hc=info["HostConfig"]
            if hc["NanoCpus"]!=2000000000 or hc["Memory"]!=2*1024**3 or hc["MemorySwap"]!=2*1024**3:
                raise RuntimeError("DB resource limits differ from protocol")
            with closing(connect(db)) as c:
                observed=fingerprint(c)
                if (run/f"source-before-{db}.json").exists():
                    if json.loads((run/f"source-before-{db}.json").read_text())!=observed: raise RuntimeError("Source changed since original run")
                else: save_json(run/f"source-before-{db}.json",observed)
                initial[db]=observed
                query=("SELECT name,setting,unit FROM pg_settings WHERE name IN ('max_connections','shared_buffers','work_mem','fsync','synchronous_commit','full_page_writes')" if db=="postgres" else "SHOW VARIABLES WHERE Variable_name IN ('max_connections','innodb_buffer_pool_size','innodb_flush_log_at_trx_commit','sync_binlog','log_bin','transaction_isolation')")
                save_json(run/f"environment-{db}.json",{"image":info["Image"],"resources":{k:hc[k] for k in ("NanoCpus","Memory","MemorySwap")},"version":call(c,"SELECT version()")[0],"driver":"psycopg" if db=="postgres" else "mysql.connector", "settings":call(c,query)[0]})
        save_json(run/"host.json",{"platform":sys.platform,"python":sys.version,"logical_cpus":psutil.cpu_count(),"memory":psutil.virtual_memory().total,"generator_processes":4,"generator_has_no_quota":True})
        for case in schedule:
            name=key(case)
            if name in done: continue
            if shutil.disk_usage(ROOT).free<2*1024**3: raise RuntimeError("Free disk below 2 GiB")
            db=case["dbms"]
            if current!=db:
                dc("stop","mysql" if db=="postgres" else "postgres")
                dc("start",db); ready(db); current=db
            container=dc("ps","-q",db)
            directory=run/name
            if directory.exists(): directory=run/(name+"-retry-"+uuid4().hex[:6])
            print(f"START {len(done)+1}/{len(schedule)} {name}",flush=True)
            # Permission journal belongs to this trial; directory itself is created by trial.
            permission_dir=run/(name+"-permissions")
            permission_dir.mkdir(exist_ok=True)
            with mysql_observer_access(Log(),permission_dir) if db=="mysql" else nullcontext():
                result=trial(db,case,directory,warmup,seconds,container)
            report["results"].append(result)
            save_json(manifest,report)
            print(f"DONE {name}: {result['status']} TPS={result['successful_tps']:.2f} p95={result['p95_ms']} retry={result['retry_pct']:.2f}%",flush=True)
            if result["status"]!="PASS": raise RuntimeError(f"Trial failed: {name}")
            done.add(name)
        report["status"]="PASS"
    except BaseException as e:
        report["status"]="FAIL"
        report["error"]=repr(e)
        raise
    finally:
        cleanup={}
        for db in PROTOCOL["engines"]:
            try:
                dc("start",db); ready(db)
                with closing(connect(db)) as c:
                    after=fingerprint(c)
                save_json(run/f"source-after-{db}.json",after)
                cleanup[db]=after==initial.get(db)
            except Exception as e: cleanup[db]=repr(e)
        report["source_unchanged"]=cleanup
        report["e6_hashes_after"]={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in e6_files}
        report["e6_unchanged"]=report["e6_hashes_after"]==e6_hashes
        if not report["e6_unchanged"]: report["status"]="FAIL"
        if not all(v is True for v in cleanup.values()): report["status"]="FAIL"
        save_json(manifest,report)
        print(f"FINAL {report['status']} {manifest}",flush=True)
        if sys.platform=="win32":
            ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)


if __name__=="__main__":
    mp.freeze_support()
    main()
