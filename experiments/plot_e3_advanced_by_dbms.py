"""Create report figures per DBMS from the archived E3-advanced summary.

Read-only with respect to the formal run; write derived PNG/SVG figures under
report_assets/e3_advanced. Run from any working directory with the project venv.
"""

import argparse
import csv
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "experiments/e3_advanced_logs/formal-20261116T061602Z/analysis/summary.csv"
OUTPUT = ROOT / "report_assets/e3_advanced"
DISTRIBUTIONS = ("uniform100k", "shared100", "hot1")
DISTRIBUTION_TITLES = ("Đều 100.000 dòng", "100 dòng chia sẻ", "Một dòng nóng")
METHODS = {"pessimistic": ("Khóa bi quan", "#d65a22", "s"),
           "occ": ("OCC", "#166d89", "o")}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=SOURCE)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()
    with args.source.open(encoding="utf-8", newline="") as source:
        rows = list(csv.DictReader(source))
    assert len(rows) == 48
    assert all(int(row["repetitions"]) == 1 for row in rows)
    args.output.mkdir(parents=True, exist_ok=True)
    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 13})
    groups = (
        ("performance",
         (("successful_tps_mean", "TPS thành công", False),
          ("p95_ms_mean", "p95 (ms, thang log)", True))),
        ("retry",
         (("retry_pct_mean", "Thử lại (%)", False),
          ("exhausted_pct_mean", "Hết lượt thử (%)", False))),
    )
    for dbms in ("postgres", "mysql"):
        for suffix, metrics in groups:
            fig, axes = plt.subplots(2, 3, figsize=(11.5, 6.5), sharex=True, layout="constrained")
            for i, (metric, ylabel, log_y) in enumerate(metrics):
                for j, distribution in enumerate(DISTRIBUTIONS):
                    ax = axes[i, j]
                    for method, (method_label, color, marker) in METHODS.items():
                        selected = sorted(
                            (r for r in rows if r["dbms"] == dbms
                             and r["distribution"] == distribution and r["method"] == method),
                            key=lambda r: int(r["clients"]),
                        )
                        assert [int(r["clients"]) for r in selected] == [1, 8, 32, 128]
                        ax.plot([int(r["clients"]) for r in selected],
                                [float(r[metric]) for r in selected],
                                label=method_label, color=color, marker=marker,
                                markersize=6, linewidth=2)
                    ax.set_xscale("log", base=2)
                    ax.set_xticks([1, 8, 32, 128], ["1", "8", "32", "128"])
                    if log_y:
                        ax.set_yscale("log")
                    elif suffix == "retry":
                        ax.set_ylim(-2, 102)
                    else:
                        ax.set_ylim(bottom=0)
                    if i == 0:
                        ax.set_title(DISTRIBUTION_TITLES[j])
                    if j == 0:
                        ax.set_ylabel(ylabel)
                    ax.grid(alpha=0.22)
            handles, labels = axes[0, 0].get_legend_handles_labels()
            fig.legend(handles, labels, loc="outside upper center", ncol=2, frameon=False)
            fig.supxlabel("Số client đồng thời")
            for extension in ("png", "svg"):
                path = args.output / f"{dbms}_{suffix}.{extension}"
                fig.savefig(path, dpi=165, facecolor="white")
            plt.close(fig)
            print(args.output / f"{dbms}_{suffix}.png")


if __name__ == "__main__":
    main()
