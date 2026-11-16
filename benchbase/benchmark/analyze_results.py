"""Step 18: audit saved formal evidence and export CSV/PNG/SVG, entirely offline.

Run with the project venv. No import of the workload runner or database drivers.
Input files are read-only; output is restricted to a separate analysis directory.
"""
import argparse
from collections import Counter
import csv
from datetime import datetime, timezone
import hashlib
from importlib import metadata
import io
import json
import math
import os
from pathlib import Path
import re
import statistics
import sys
from xml.etree import ElementTree as ET

HERE = Path(__file__).resolve().parent
DBMS = ("postgres", "mysql")
NAMES = {"postgres": "PostgreSQL", "mysql": "MySQL InnoDB"}
TRANSACTIONS = {1: "NewOrder", 2: "Payment", 3: "OrderStatus", 4: "Delivery", 5: "StockLevel"}
STATES = {"SUCCESS", "USER_ABORTED", "ERROR", "RETRY", "RETRY_DIFFERENT", "UNKNOWN"}
METRICS = (
    "successful_tps", "latency_p50_success_ms", "latency_p95_success_ms", "latency_p99_success_ms",
    "latency_p95_all_completed_ms", "retry_request_pct", "sql_or_retry_failure_pct",
    "neworder_intentional_abort_pct_of_neworder", "delivery_conflict_abort_pct_of_delivery",
    "delivery_conflict_abort_pct_of_all", "mean_lock_waiting_sessions", "max_lock_waiting_sessions",
    "mean_waiting_session_pct", "db_cpu_docker_pct_mean", "db_cpu_quota_pct_mean",
    "generator_cpu_docker_pct_mean", "generator_cpu_quota_pct_mean",
    "db_memory_mib_mean", "db_memory_mib_max", "generator_memory_mib_mean", "generator_memory_mib_max",
)
COUNTS = ("measured_requests", "successful_requests", "neworder_requests", "delivery_requests",
          "neworder_intentional_aborts", "delivery_conflict_aborts", "sql_or_retry_failed_requests",
          "requests_with_retry", "reexecutions_started", "retry_events")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def quantile(values, probability):
    require(bool(values), "Cannot calculate a percentile of empty data")
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    low, high = math.floor(position), math.ceil(position)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def percent(numerator, denominator):
    require(denominator > 0, "Percentage denominator must be positive")
    return 100.0 * numerator / denominator


def same_number(actual, expected, label):
    require(math.isfinite(actual) and math.isclose(actual, expected, rel_tol=1e-9, abs_tol=1e-8),
            f"Mismatch in {label}: raw={actual}, saved={expected}")


class Inputs:
    def __init__(self):
        self.files = {}

    def read(self, path):
        path = Path(path).resolve()
        require(path.is_relative_to(HERE), f"Input outside benchmark directory: {path}")
        raw = path.read_bytes()
        self.files[path.relative_to(HERE).as_posix()] = {
            "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw),
        }
        return raw.decode("utf-8-sig")

    def json(self, path):
        return json.loads(self.read(path))

    def csv(self, path):
        return csv.DictReader(io.StringIO(self.read(path), newline=""))

    def verify_unchanged(self):
        for name, record in self.files.items():
            with (HERE / name).open("rb") as stream:
                actual = hashlib.file_digest(stream, "sha256").hexdigest()
            require(actual == record["sha256"], f"Input changed during analysis: {name}")


def memory_bytes(value):
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(B|kB|KB|MB|GB|TB|KiB|MiB|GiB|TiB)\s*", value)
    require(match is not None, f"Invalid Docker memory value: {value!r}")
    factors = {"B": 1, "kB": 1000, "KB": 1000, "MB": 1000**2, "GB": 1000**3, "TB": 1000**4,
               "KiB": 1024, "MiB": 1024**2, "GiB": 1024**3, "TiB": 1024**4}
    return float(match[1]) * factors[match[2]]


def measurement_only(records):
    return [record for record in records if record["phase"] == "measure"]


def sample_gaps(records):
    times = sorted(datetime.fromisoformat(record["utc"]) for record in records)
    gaps = [(b - a).total_seconds() for a, b in zip(times, times[1:])]
    return max(gaps, default=0.0)


def lock_metrics(records):
    records = measurement_only(records)
    require(bool(records), "No lock samples in measurement phase")
    sessions = [int(row["sessions"]) for row in records]
    waiters = [int(row["lock_waiting_sessions"]) for row in records]
    require(all(0 <= w <= n and n > 0 for w, n in zip(waiters, sessions)), "Invalid session/waiter sample")
    return {"db_samples_measure": len(records), "max_observed_measure_sessions": max(sessions),
            "mean_lock_waiting_sessions": statistics.mean(waiters), "max_lock_waiting_sessions": max(waiters),
            "mean_waiting_session_pct": statistics.mean(percent(w, n) for w, n in zip(waiters, sessions)),
            "db_sample_gap_max_s": sample_gaps(records),
            "db_sampler_query_ms_mean": statistics.mean(float(row["sample_ms"]) for row in records)}


def resource_metrics(records, db_cpus, gen_cpus):
    records = measurement_only(records)
    result = {}
    for role, cpus in (("db", db_cpus), ("generator", gen_cpus)):
        selected = [r for r in records if r["stats"]["Name"].startswith("labbench-") == (role == "generator")]
        require(bool(selected), f"No {role} resource samples in measurement phase")
        cpu = [float(r["stats"]["CPUPerc"].removesuffix("%")) for r in selected]
        used = [memory_bytes(r["stats"]["MemUsage"].split("/")[0]) / 2**20 for r in selected]
        require(all(math.isfinite(c) and c >= 0 for c in cpu), "Invalid CPU sample")
        require(all(m > 0 and math.isfinite(m) for m in used), "Invalid memory sample")
        result.update({f"{role}_resource_samples_measure": len(selected),
                       f"{role}_cpu_docker_pct_mean": statistics.mean(cpu),
                       f"{role}_cpu_quota_pct_mean": statistics.mean(cpu) / cpus,
                       f"{role}_memory_mib_mean": statistics.mean(used),
                       f"{role}_memory_mib_max": max(used),
                       f"{role}_resource_sample_gap_max_s": sample_gaps(selected)})
    return result


def request_metrics(rows):
    statuses, types, aborted = Counter(), Counter(), Counter()
    successful_latency, all_latency = [], []
    retry_requests = reexecutions = retry_events = 0
    last_errors = Counter()
    for row in rows:
        kind, attempts = int(row["transaction_type"]), int(row["attempts"])
        status = row["status"]
        require(kind in TRANSACTIONS and status in STATES, "Unknown request type/status")
        require(1 <= attempts <= 3 and int(row["phase"]) == 1, "Invalid attempt/phase")
        duration = (int(row["end_ns"]) - int(row["start_ns"])) / 1e6
        require(duration >= 0, "Negative request latency")
        all_latency.append(duration)
        statuses[status] += 1
        types[kind] += 1
        if status == "SUCCESS":
            successful_latency.append(duration)
        if status == "USER_ABORTED":
            aborted[kind] += 1
        retry_requests += attempts > 1
        reexecutions += attempts - 1
        retry_events += int(row["retry_events"])
        if row["sqlstate"]:
            last_errors[f"{row['sqlstate']}/{row['sqlcode']}"] += 1
    require(bool(successful_latency), "No successful requests")
    require(set(aborted) <= {1, 4}, f"USER_ABORTED in unexpected procedure: {aborted}")
    return {"statuses": statuses, "types": types, "aborted": aborted, "last_errors": last_errors,
            "successful_latency": successful_latency, "all_latency": all_latency,
            "retry_requests": retry_requests, "reexecutions": reexecutions, "retry_events": retry_events}


def load_run(inputs, entry, verified, protocol):
    directory = (HERE / entry["directory"].replace("\\", "/")).resolve()
    require(directory.is_relative_to(HERE / "results") and directory.name.startswith("formal-"), "Not a formal input directory")
    manifest = inputs.json(directory / "run.json")
    saved = inputs.json(directory / "metrics.json")
    require(entry["validation"] == saved["validation"] == "PASS" and saved["native_count_matches"], "Saved run validation failed")
    key = (manifest["dbms"], int(manifest["clients"]), int(manifest["repetition"]))
    require(manifest["kind"] == "formal" and manifest["status"] == "PASS", f"Invalid run: {key}")
    require(entry["dbms"] == key[0] and entry["clients"] == key[1], "Report/manifest key mismatch")
    require(manifest["protocol"] == protocol, "Protocol changed between runs")
    require(manifest["warmup_seconds"] == 15 and manifest["measure_seconds"] == 60, "Unexpected measurement duration")
    require(verified["status"] == "PASS" and verified["repetition"] == key[2], "Missing step-17 verification")
    xml = inputs.read(directory / "workload.xml")
    require(hashlib.sha256(xml.encode()).hexdigest() == manifest["workload_sha256"], "Workload XML changed")
    root = ET.fromstring(xml)
    require(root.findtext("isolation") == "TRANSACTION_READ_COMMITTED" and
            root.findtext("works/work/weights") == "45,43,4,4,4", "Unexpected workload configuration")
    native_paths = list((directory / "native").glob("*.summary.json"))
    require(len(native_paths) == 1, "Missing/duplicate native summary")
    native = inputs.json(native_paths[0])
    elapsed = native["Elapsed Time (nanoseconds)"] / 1e9
    require(elapsed >= 59, "Short formal measurement")
    paths = sorted((directory / "requests").glob("worker-*.csv"))
    require(bool(paths), "Missing request outcome CSV")
    data = request_metrics(row for path in paths for row in inputs.csv(path))
    counts = data["statuses"]
    requests = counts.total()
    require(requests == native["Measured Requests"] == saved["measured_requests"] == entry["measured_requests"], "Request-count mismatch")
    require(counts == Counter(saved["status_counts"]), "Outcome-count mismatch")
    require(saved["consistency_checks"] and all(saved["consistency_checks"].values()), "Business invariant failed")
    require(data["aborted"] == Counter({int(k): v for k, v in verified["user_aborted_by_transaction"].items()}), "Abort classification mismatch")
    require(data["retry_requests"] == saved["requests_with_retry"] and
            data["reexecutions"] == saved["reexecutions_started"] and data["retry_events"] == saved["retry_events"], "Retry mismatch")
    row = {"dbms": key[0], "clients": key[1], "repetition": key[2],
           "run_directory": directory.relative_to(HERE).as_posix(), "started_utc": manifest["started_utc"],
           "elapsed_measure_s": elapsed, "measured_requests": requests, "successful_requests": counts["SUCCESS"],
           "successful_tps": counts["SUCCESS"] / elapsed,
           "neworder_requests": data["types"][1], "delivery_requests": data["types"][4],
           "neworder_intentional_aborts": data["aborted"][1], "delivery_conflict_aborts": data["aborted"][4],
           "sql_or_retry_failed_requests": sum(counts[s] for s in ("ERROR", "RETRY", "RETRY_DIFFERENT", "UNKNOWN")),
           "requests_with_retry": data["retry_requests"], "reexecutions_started": data["reexecutions"],
           "retry_events": data["retry_events"], "business_invariants_passed": True}
    same_number(row["successful_tps"], saved["successful_tps"], "successful TPS")
    same_number(row["successful_tps"], entry["successful_tps"], "report TPS")
    for q, name in ((.5, "p50"), (.95, "p95"), (.99, "p99")):
        row[f"latency_{name}_success_ms"] = quantile(data["successful_latency"], q)
        same_number(row[f"latency_{name}_success_ms"], saved["latency_success_ms"][name], name)
    row["latency_p95_all_completed_ms"] = quantile(data["all_latency"], .95)
    row.update({"retry_request_pct": percent(row["requests_with_retry"], requests),
                "sql_or_retry_failure_pct": percent(row["sql_or_retry_failed_requests"], requests),
                "neworder_intentional_abort_pct_of_neworder": percent(data["aborted"][1], data["types"][1]),
                "delivery_conflict_abort_pct_of_delivery": percent(data["aborted"][4], data["types"][4]),
                "delivery_conflict_abort_pct_of_all": percent(data["aborted"][4], requests)})
    row.update(lock_metrics(list(inputs.csv(directory / "db-samples.csv"))))
    require(row["max_observed_measure_sessions"] >= key[1], "Missing client sessions during measurement")
    resources = [json.loads(line) for line in inputs.read(directory / "resources.jsonl").splitlines() if line.strip()]
    row.update(resource_metrics(resources, protocol["db_cpu_limit"], protocol["generator_cpu_limit"]))
    return row


def summarize(runs, clients, repetitions=3):
    expected = {(db, c, r) for db in DBMS for c in clients for r in range(1, repetitions + 1)}
    keys = [(r["dbms"], r["clients"], r["repetition"]) for r in runs]
    require(len(keys) == len(set(keys)) and set(keys) == expected, "Missing or duplicate formal configurations")
    summary = []
    for db in DBMS:
        for clients_count in clients:
            selected = [r for r in runs if r["dbms"] == db and r["clients"] == clients_count]
            row = {"dbms": db, "clients": clients_count, "repetitions": repetitions}
            for metric in METRICS:
                values = [r[metric] for r in selected]
                row.update({f"{metric}_mean": statistics.mean(values), f"{metric}_min": min(values),
                            f"{metric}_max": max(values), f"{metric}_stdev": statistics.stdev(values)})
            for name in COUNTS:
                row[f"{name}_sum"] = sum(r[name] for r in selected)
            row["successful_tps_relative_span_pct"] = percent(
                row["successful_tps_max"] - row["successful_tps_min"], row["successful_tps_mean"])
            summary.append(row)
    return summary


def save_json(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def save_csv(path, rows):
    temporary = path.with_suffix(".csv.tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)


def charts(output, runs, summary, clients):
    os.environ.setdefault("MPLCONFIGDIR", str(output / ".mpl-cache"))
    import matplotlib
    matplotlib.use("Agg")
    from matplotlib import pyplot as plt, ticker
    plt.rcParams.update({"font.family": ["Arial", "DejaVu Sans"], "font.size": 11,
                         "axes.spines.top": False, "axes.spines.right": False,
                         "axes.labelpad": 10, "svg.fonttype": "none", "svg.hashsalt": "dbms-step18",
                         "savefig.facecolor": "white"})
    palette = {"postgres": "#176B87", "mysql": "#CE5A26"}
    markers = {"postgres": "o", "mysql": "s"}
    source_day = datetime.fromisoformat(min(r["started_utc"] for r in runs)).strftime("%d/%m/%Y")
    written = []

    def series(ax, metric, ylabel, log_y=False, bounded_pct=False):
        for db in DBMS:
            selected = [r for r in summary if r["dbms"] == db]
            x = [r["clients"] for r in selected]
            y = [r[f"{metric}_mean"] for r in selected]
            errors = [[r[f"{metric}_mean"] - r[f"{metric}_min"] for r in selected],
                      [r[f"{metric}_max"] - r[f"{metric}_mean"] for r in selected]]
            ax.errorbar(x, y, yerr=errors, label=NAMES[db], color=palette[db], marker=markers[db],
                        linewidth=2, markersize=6, capsize=4, elinewidth=1.3, zorder=4)
            for row in runs:
                if row["dbms"] == db:
                    jitter = 2 ** ((row["repetition"] - 2) * .045)
                    ax.scatter(row["clients"] * jitter, row[metric], color=palette[db],
                               marker=markers[db], s=17, alpha=.55, zorder=3, linewidths=0)
        ax.set_xscale("log", base=2)
        ax.set_xticks(clients, labels=[str(c) for c in clients])
        ax.set_xlim(.83, max(clients) * 1.2)
        ax.set_xlabel("Số client đồng thời (trục log₂)")
        ax.set_ylabel(ylabel)
        if log_y:
            ax.set_yscale("log")
            ax.yaxis.set_major_locator(ticker.LogLocator(base=10, subs=(1, 2, 5)))
            ax.yaxis.set_major_formatter(ticker.FuncFormatter(lambda v, p: f"{v:g}"))
        else:
            ax.set_ylim(bottom=0)
        if bounded_pct:
            ax.set_ylim(0, 105)
        ax.grid(axis="y", color="#DDDDDD", linewidth=.7)
        ax.set_axisbelow(True)
        ax.tick_params(which="minor", length=0)

    def finish(fig, axes, name, title, subtitle, note):
        fig.text(.095, .965, title, fontsize=17, fontweight="bold", va="top")
        fig.text(.095, .915, subtitle, fontsize=10.5, color="#444444", va="top")
        handles, labels = axes[0].get_legend_handles_labels()
        fig.legend(handles, labels, loc="upper right", bbox_to_anchor=(.98, .965), frameon=False, fontsize=10)
        fig.text(.095, .125, "Chấm lớn/đường: trung bình; thanh: min–max; chấm nhỏ: từng lượt (n = 3).", fontsize=10)
        fig.text(.095, .085, note, fontsize=9.5, color="#444444")
        fig.text(.095, .045, f"Nguồn: 42 lượt formal, {source_day} (UTC) · giữ đầy đủ cả 3 lần lặp.", fontsize=9.5, color="#444444")
        for suffix in ("png", "svg"):
            path = output / f"{name}.{suffix}"
            options = {"Date": None} if suffix == "svg" else {"Software": "DBMS step-18 offline analysis"}
            fig.savefig(path, dpi=220, metadata=options)
            written.append(path.name)
        plt.close(fig)

    subtitle = "TPC-C-like · 1 warehouse · READ COMMITTED · 2 vCPU / 2 GiB cho mỗi DBMS"
    for name, metric, title, ylabel, log_y, note in (
        ("01_successful_tps", "successful_tps", "Throughput khi tăng số client", "Successful TPS (request/giây)", False,
         "Chỉ SUCCESS trong cửa sổ đo; warm-up 15 giây, thời gian đo thực tế khoảng 60 giây/lượt."),
        ("02_p95_latency", "latency_p95_success_ms", "Độ trễ đuôi khi tăng số client", "p95 request thành công (ms, trục log)", True,
         "Trung bình của 3 p95 từng lượt, không phải p95 gộp; thời gian bao gồm retry trong request."),
    ):
        fig, ax = plt.subplots(figsize=(11.8, 6.8))
        fig.subplots_adjust(left=.095, right=.975, top=.82, bottom=.26)
        series(ax, metric, ylabel, log_y=log_y)
        finish(fig, [ax], name, title, subtitle, note)
    fig, axes = plt.subplots(1, 2, figsize=(13.6, 6.8))
    fig.subplots_adjust(left=.095, right=.975, top=.82, bottom=.26, wspace=.28)
    series(axes[0], "mean_lock_waiting_sessions", "Session chờ lock (trung bình mẫu)")
    series(axes[1], "mean_waiting_session_pct", "Session chờ / session quan sát (%)", bounded_pct=True)
    finish(fig, axes, "03_lock_wait", "Mức chờ khóa trong pha đo", subtitle,
           "Chỉ mẫu phase=measure; nhãn pha lấy từ log. Đây là mẫu tức thời, không phải tổng số/thời gian chờ.")
    fig, axes = plt.subplots(1, 2, figsize=(13.6, 6.8))
    fig.subplots_adjust(left=.095, right=.975, top=.82, bottom=.26, wspace=.31)
    series(axes[0], "delivery_conflict_abort_pct_of_delivery", "Delivery bị hủy / yêu cầu Delivery (%)")
    series(axes[1], "retry_request_pct", "Request đã retry / mọi request đo (%)")
    finish(fig, axes, "04_abort_retry", "Hủy do tranh chấp và retry", subtitle,
           "Delivery USER_ABORTED tách khỏi NewOrder rollback cố ý; hai ô dùng mẫu số và thang tung khác nhau.")
    fig, axes = plt.subplots(2, 2, figsize=(13.6, 10))
    fig.subplots_adjust(left=.095, right=.975, top=.83, bottom=.21, wspace=.27, hspace=.48)
    series(axes[0, 0], "db_cpu_quota_pct_mean", "CPU DBMS / hạn mức 2 vCPU (%)", bounded_pct=True)
    series(axes[0, 1], "generator_cpu_quota_pct_mean", "CPU BenchBase / hạn mức 4 vCPU (%)", bounded_pct=True)
    series(axes[1, 0], "db_memory_mib_mean", "RAM DBMS (trung bình mẫu, MiB)")
    series(axes[1, 1], "generator_memory_mib_mean", "RAM BenchBase (trung bình mẫu, MiB)")
    finish(fig, list(axes.flat), "05_cpu_memory", "Tài nguyên của DBMS và bộ tạo tải", subtitle,
           "Chỉ mẫu measure. Docker CPU 100% = 1 vCPU; chuẩn hóa theo quota. RAM là Docker stats MemUsage.")
    return written


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=HERE / "analysis")
    args = parser.parse_args()
    output = args.output.resolve()
    require(output != HERE and not any(output.is_relative_to(HERE / n) for n in ("results", "baseline", "runtime")),
            "Output must be separate from raw evidence and runtime")
    output.mkdir(parents=True, exist_ok=True)
    report_path = output / "analysis-validation.json"
    save_json(report_path, {"status": "RUNNING", "offline": True})
    try:
        inputs = Inputs()
        inputs.read(Path(__file__))
        inputs.read(HERE / "analysis-requirements.txt")
        protocol = inputs.json(HERE / "protocol.json")
        report = inputs.json(HERE / "formal-report.json")
        verification = inputs.json(HERE / "step17-verification.json")
        require(report["complete"] and verification["status"] == "PASS", "Incomplete/unverified formal batch")
        require(len(report["runs"]) == verification["verified_runs"] == 42, "Expected 42 formal runs")
        verified = {r["directory"].replace("\\", "/"): r for r in verification["runs"]}
        runs = []
        for entry in report["runs"]:
            key = entry["directory"].replace("\\", "/")
            require(key in verified, f"Run absent from step-17 verification: {key}")
            runs.append(load_run(inputs, entry, verified[key], protocol))
        runs.sort(key=lambda r: (DBMS.index(r["dbms"]), r["clients"], r["repetition"]))
        summary = summarize(runs, protocol["clients"], protocol["repetitions"])
        totals = {name: sum(r[name] for r in runs) for name in COUNTS}
        for name, saved_name in (("measured_requests", "measured_requests"), ("successful_requests", "successful_requests"),
                                 ("requests_with_retry", "requests_with_retry"), ("reexecutions_started", "reexecutions_started")):
            require(totals[name] == verification["totals"][saved_name], f"Batch total mismatch: {name}")
        require(totals["neworder_intentional_aborts"] == verification["neworder_intentional_aborts"] and
                totals["delivery_conflict_aborts"] == verification["delivery_conflict_aborts"], "Batch abort mismatch")
        save_csv(output / "trials.csv", runs)
        save_csv(output / "summary.csv", summary)
        figures = charts(output, runs, summary, protocol["clients"])
        inputs.verify_unchanged()
        dependencies = {name: metadata.version(name) for name in
                        ("matplotlib", "numpy", "pillow", "contourpy", "cycler", "fonttools", "kiwisolver", "packaging", "pyparsing", "python-dateutil", "tzdata")}
        largest_spans = sorted(summary, key=lambda r: r["successful_tps_relative_span_pct"], reverse=True)[:4]
        validation = {"status": "PASS", "offline": True, "docker_or_database_calls": 0,
                      "generated_utc": datetime.now(timezone.utc).isoformat(), "python": sys.version,
                      "dependencies": dependencies, "run_count": len(runs), "configuration_count": len(summary),
                      "repetitions_per_configuration": 3, "totals": totals,
                      "inputs_unchanged": True, "input_files": inputs.files,
                      "largest_tps_relative_spans": [{k: r[k] for k in
                          ("dbms", "clients", "successful_tps_mean", "successful_tps_min", "successful_tps_max", "successful_tps_relative_span_pct")} for r in largest_spans],
                      "definitions": {"error_bars": "min-max across 3 per-run values; not confidence intervals",
                          "p95_mean": "arithmetic mean of the 3 per-run p95 values, not a pooled p95",
                          "rate_aggregation": "arithmetic mean of per-run percentages; totals are sums",
                          "waiting_sessions": "arithmetic mean of instantaneous measure samples; not total wait events or wait time",
                          "phase_labels": "request phase gate is exact; SQL/resource measure labels follow received logs and are approximate",
                          "resource_aggregation": "mean of measure samples within each run, then mean/min/max across 3 runs",
                          "cpu_quota": "Docker CPUPerc divided by configured vCPU quota; host CPU/RAM not included"},
                      "limitations": ["All 3 repetitions retained; only 3 runs per configuration, no significance claim.",
                          "TPC-C-like, one warehouse, READ COMMITTED, instrumented BenchBase; no separate version-column OCC workload.",
                          "Selected invariant checks passed, not a complete TPC-C audit.",
                          "Source TPCCWorker parameter RNG is not fully controlled by configured randomSeed.",
                          "CPU/resource variation does not establish a specific causal explanation.",
                          "Linux Docker CLI MemUsage subtracts file cache; it is not total host RAM or per-process RSS."],
                      "outputs": ["trials.csv", "summary.csv", *figures],
                      "output_files": {name: {"sha256": hashlib.sha256((output / name).read_bytes()).hexdigest(),
                                               "bytes": (output / name).stat().st_size}
                                       for name in ["trials.csv", "summary.csv", *figures]}}
        save_json(report_path, validation)
        print(json.dumps({k: validation[k] for k in ("status", "offline", "run_count", "configuration_count", "totals")}, indent=2))
        print(f"Saved {len(figures)} figure files and 2 CSV tables in {output}")
    except BaseException as error:
        save_json(report_path, {"status": "FAIL", "offline": True, "error": str(error)})
        raise


if __name__ == "__main__":
    main()
