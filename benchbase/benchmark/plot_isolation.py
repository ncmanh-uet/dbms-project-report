"""Plot E6 with independent panels for PostgreSQL and MySQL InnoDB."""

import argparse
import csv
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
from matplotlib.ticker import FuncFormatter, PercentFormatter


HERE = Path(__file__).resolve().parent
ENGINES = {"postgres": "PostgreSQL", "mysql": "MySQL InnoDB"}
COLORS = {"RC": "#20648A", "RR": "#AC5325"}
MARKERS = {"RC": "o", "RR": "s"}


def read_csv(path):
    with path.open(encoding="utf-8-sig", newline="") as source:
        return list(csv.DictReader(source))


def save(fig, output, name):
    fig.savefig(output / f"{name}.png", dpi=210, bbox_inches="tight", facecolor="white")
    plt.close(fig)


def style(axis, title, ylabel):
    axis.set_title(title, loc="left", fontweight="bold", fontsize=11)
    axis.set_ylabel(ylabel)
    axis.spines[["top", "right"]].set_visible(False)
    axis.spines[["left", "bottom"]].set_color("#A6ADB3")
    axis.grid(axis="y", color="#E1E5E9", linewidth=0.75)
    axis.set_axisbelow(True)


def historical(rows, output):
    fig, axes = plt.subplots(1, 2, figsize=(7.4, 2.75), layout="constrained", sharey=True)
    for axis, (dbms, name) in zip(axes, ENGINES.items()):
        engine_rows = sorted((row for row in rows if row["dbms"] == dbms), key=lambda row: int(row["clients"]))
        baseline = float(engine_rows[0]["successful_tps_mean"])
        normalized = [[float(row[f"successful_tps_{stat}"]) / baseline for row in engine_rows]
                      for stat in ("mean", "min", "max")]
        averages, low, high = normalized
        clients = [int(row["clients"]) for row in engine_rows]
        axis.axhline(1, color="#7D858B", linewidth=0.85, linestyle="--")
        axis.errorbar(clients, averages, yerr=[[a-l for a, l in zip(averages, low)],
                                              [h-a for a, h in zip(averages, high)]],
                      color=COLORS["RC"], marker="o", capsize=3, linewidth=1.7)
        axis.set_xscale("log", base=2)
        axis.set_xticks(clients, labels=clients)
        style(axis, name, "TPS / TPS tại 1 client")
        axis.set_xlabel("Số client")
        axis.yaxis.set_major_formatter(FuncFormatter(lambda value, _: f"{value:g}".replace(".", ",")))
    save(fig, output, "01_rc_scaling")


def comparison(summary, output, name, metrics):
    fig, axes = plt.subplots(len(metrics), 2, figsize=(7.4, 2.9 + 1.9*(len(metrics)-1)),
                             layout="constrained", squeeze=False, sharex=True, sharey="row")
    for row_index, (metric, units, percentage) in enumerate(metrics):
        for axis, (dbms, engine_name) in zip(axes[row_index], ENGINES.items()):
            for mode in COLORS:
                selected = sorted((r for r in summary if r["dbms"] == dbms and r["isolation"] == mode),
                                  key=lambda r: int(r["clients"]))
                y, low, high = [[float(row[f"{metric}_{stat}"]) for row in selected] for stat in ("mean", "min", "max")]
                x = [index + (-.035 if mode == "RC" else .035) for index in range(len(selected))]
                axis.errorbar(x, y, yerr=[[a-l for a, l in zip(y, low)], [h-a for a, h in zip(y, high)]],
                              color=COLORS[mode], marker=MARKERS[mode], capsize=3, linewidth=1.7,
                              linestyle="-" if mode == "RC" else "--")
            style(axis, engine_name if row_index == 0 else "", units)
            axis.set_xticks(range(3), ["4", "16", "64"])
            axis.set_xlim(-.23, 2.23)
            if metric == "db_cpu_quota_pct_mean":
                axis.axhline(100, color="#7D858B", linewidth=0.8, linestyle=":")
            if percentage:
                axis.yaxis.set_major_formatter(PercentFormatter(xmax=100))
            if row_index == len(metrics)-1:
                axis.set_xlabel("Số client")
        upper = axes[row_index, 0].get_ylim()[1]
        axes[row_index, 0].set_ylim(bottom=-0.025*upper)
    handles = [Line2D([0], [0], color=COLORS[mode], marker=MARKERS[mode],
                      linestyle="-" if mode == "RC" else "--", label=mode) for mode in COLORS]
    fig.legend(handles=handles, loc="outside upper right", ncols=2, frameon=False)
    save(fig, output, name)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("campaign", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 10, "text.color": "#202C35",
                        "axes.labelcolor": "#202C35", "xtick.color": "#46535D", "ytick.color": "#46535D"})
    historical(read_csv(HERE / "analysis/summary.csv"), args.output)
    runs = read_csv(args.campaign / "summary.csv")
    comparison(runs, args.output, "02_isolation_tps", [("successful_tps", "TPS thành công", False)])
    comparison(runs, args.output, "03_isolation_p95", [("p95_ms", "p95 (ms)", False)])
    comparison(runs, args.output, "04_isolation_retry_abort", [("retry_pct", "Yêu cầu thử lại", True),
                                                               ("delivery_abort_pct", "Delivery bị hủy", True)])
    comparison(runs, args.output, "05_isolation_cpu_lock", [("db_cpu_quota_pct_mean", "CPU / hạn mức", True),
                                                            ("mean_waiting_session_pct", "Phiên chờ khóa", True)])


if __name__ == "__main__":
    main()
