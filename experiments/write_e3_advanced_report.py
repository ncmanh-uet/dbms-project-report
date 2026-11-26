"""Write the E3-advanced report from a verified formal run."""
import argparse
import csv
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LABEL = {"pessimistic": "Khóa bi quan", "occ": "OCC"}
DIST = {"uniform100k": "Phân bố đều 100.000 dòng", "shared100": "Chia sẻ 100 dòng", "hot1": "Một dòng nóng"}


def read_csv(path):
    with path.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def number(row, key, digits=1):
    return f"{float(row[key]):,.{digits}f}"


def table(headers, rows):
    return "| " + " | ".join(headers) + " |\n| " + " | ".join("---" for _ in headers) + " |\n" + "\n".join("| " + " | ".join(map(str, row)) + " |" for row in rows) + "\n\n"


def build(run):
    report = json.loads((run / "report.json").read_text(encoding="utf-8"))
    validation = json.loads((run / "analysis/validation.json").read_text(encoding="utf-8"))
    protocol = report["protocol"]
    expected = len(protocol["engines"]) * len(protocol["methods"]) * len(protocol["distributions"]) * len(protocol["clients"]) * protocol["repetitions"]
    assert report["status"] == validation["status"] == "PASS"
    assert expected == protocol["total_formal_trials"] == validation["trials"] == validation["configurations"] == 48
    assert report["e6_unchanged"] and all(report["source_unchanged"].values())
    rows = read_csv(run / "analysis/summary.csv")
    assert len(rows) == expected and set(r["method"] for r in rows) == set(protocol["methods"])
    ix = {(r["dbms"], r["distribution"], r["method"], int(r["clients"])): r for r in rows}
    totals = validation["totals"]
    rel = run.relative_to(ROOT).as_posix()
    lines = ["# E3-advanced — Báo cáo thực nghiệm OCC và khóa bi quan", "",
             f"**Đợt formal:** `{run.name}` · **Trạng thái:** PASS · **48/48 lượt** · **Mỗi cấu hình đo một lần**", "",
             "## 1. Câu hỏi và thiết kế", "",
             "So sánh cập nhật một bộ đếm `s_ytd` bằng khóa bi quan (`SELECT FOR UPDATE`, sau đó `UPDATE`) và OCC (đọc `version`, `UPDATE` có điều kiện rồi retry khi xung đột) trên PostgreSQL và MySQL. Đây là microbenchmark cập nhật một dòng, không phải toàn bộ giao dịch TPC-C.", "",
             "Ma trận: 2 DBMS × 2 phương pháp × 3 phân bố dòng đích × 4 mức client (1, 8, 32, 128) × 1 lần đo = **48 lượt**. `uniform100k` chọn đều 100.000 dòng, `shared100` chọn đều 100 dòng, `hot1` chỉ chọn một dòng. Mỗi lượt warm-up 15 giây, nhận yêu cầu đo 60 giây rồi drain yêu cầu đã nhận. Hai DBMS chạy lần lượt, mỗi DBMS giới hạn 2 vCPU và 2 GiB RAM; generator Python trên Windows host không có quota tương ứng. Isolation là READ COMMITTED.", "",
             "Mỗi lượt tạo bản sao đầy đủ 100.000 dòng `stock` và thêm cột version; không sửa bảng nguồn. Retry tối đa 8 lần thêm sau attempt đầu, chỉ cho version conflict hoặc deadlock xác định. TPS tính theo yêu cầu logic thành công chia cho thời gian nhận cộng drain; p95 chỉ xét yêu cầu thành công và bao gồm retry/backoff. Tỷ lệ retry và hết retry tính trên số yêu cầu logic.", "",
             "## 2. Kiểm chứng và toàn cảnh", "",
             f"Cả **48/48 lượt PASS**. Kiểm tra offline đối chiếu CSV từng worker với số yêu cầu, số thành công, attempt, xung đột và p95; kiểm tra 100.000 bộ đếm mỗi lượt, cleanup bảng tạm, fingerprint dữ liệu nguồn hai hệ và hash bốn tệp E6. Kết quả: `source_unchanged=true` cho cả PostgreSQL/MySQL và `e6_unchanged=true`.", "",
             f"Toàn bộ ma trận ghi **{totals['logical_operations']:,} yêu cầu đo**, **{totals['successes']:,} thành công**, **{totals['exhausted']:,} hết retry**, **{totals['version_conflicts']:,} xung đột version ở cấp attempt**, **{totals['deadlocks']:,} deadlock** và **{totals['timeouts']:,} timeout**. Đây là tổng trên các cấu hình với tải và thông lượng khác nhau; không dùng tỷ lệ gộp này để xếp hạng hai phương pháp.", "",
             "## 3. Kết quả theo từng cấu hình", "",
             "TPS là yêu cầu thành công/giây; p95 là mili giây; hoàn tất, retry và hết retry là phần trăm yêu cầu logic. Mỗi ô là một lượt đo, không phải trung bình nhiều lần.", ""]
    for db in protocol["engines"]:
        lines += [f"### {db.upper()}", ""]
        for dist in protocol["distributions"]:
            lines += [f"#### {DIST[dist]} (`{dist}`)", ""]
            data = []
            for c in protocol["clients"]:
                for method in protocol["methods"]:
                    r = ix[(db, dist, method, c)]
                    data.append([c, LABEL[method], number(r, "successful_tps_mean"), number(r, "p95_ms_mean"), number(r, "completion_pct_mean", 2), number(r, "retry_pct_mean", 2), number(r, "exhausted_pct_mean", 2)])
            lines.append(table(["Client", "Phương pháp", "TPS", "p95 (ms)", "Hoàn tất (%)", "Retry (%)", "Hết retry (%)"], data))
    lines += ["## 4. Đối chiếu và diễn giải", ""]
    for db in protocol["engines"]:
        wide = max(float(ix[db, "uniform100k", "occ", c]["retry_pct_mean"]) for c in protocol["clients"])
        occ = ix[db, "shared100", "occ", 128]
        locked = ix[db, "shared100", "pessimistic", 128]
        tps_ratio = 100 * float(occ["successful_tps_mean"]) / float(locked["successful_tps_mean"])
        lines += [f"Trên {db}, tỷ lệ thử lại OCC tối đa là {wide:.2f}% ở `uniform100k`. "
                  f"Ở `shared100` với 128 client, tỷ lệ này là {number(occ, 'retry_pct_mean', 2)}% "
                  f"và TPS OCC bằng {tps_ratio:.1f}% TPS khóa bi quan.", ""]
    lines += ["Với `hot1`, tất cả client tranh cùng một dòng. Kết quả ở 128 client được trình bày dưới đây.", ""]
    hot = []
    for db in protocol["engines"]:
        p = ix[(db, "hot1", "pessimistic", 128)]
        o = ix[(db, "hot1", "occ", 128)]
        hot.append([db, number(p, "successful_tps_mean"), number(o, "successful_tps_mean"), number(o, "completion_pct_mean", 2), number(o, "retry_pct_mean", 2), number(o, "exhausted_pct_mean", 2), number(p, "p95_ms_mean"), number(o, "p95_ms_mean")])
    lines.append(table(["DBMS", "TPS khóa", "TPS OCC", "OCC hoàn tất (%)", "OCC retry (%)", "OCC hết retry (%)", "p95 khóa (ms)", "p95 OCC (ms)"], hot))
    lines += ["Tại điểm này, OCC mất nhiều yêu cầu vì hết ngân sách retry; p95 chỉ trên yêu cầu thành công nên phải đọc cùng tỷ lệ hoàn tất. Khóa bi quan giữ tỷ lệ hoàn tất cao hơn nhưng cũng có độ trễ lớn khi nhiều client chờ cùng dòng. So sánh giữa PostgreSQL và MySQL trong bảng là kết quả end-to-end của cả driver, giao tiếp Windows–Docker và generator, không cô lập riêng hiệu năng engine.", "",
              "## 5. Biểu đồ", ""]
    for name, caption in [("01_tps", "Thông lượng thành công"), ("02_p95", "p95 của yêu cầu thành công"), ("03_retry", "Tỷ lệ yêu cầu có retry"), ("04_exhausted", "Tỷ lệ yêu cầu hết retry")]:
        lines += [f"### {caption}", "", f"![{caption}]({rel}/analysis/{name}.png)", ""]
    lines += ["## 6. Giới hạn và khả năng tái lập", "",
              "**Mỗi cấu hình chỉ đo một lần.** Không có ước lượng phương sai, khoảng tin cậy hoặc kiểm định ý nghĩa thống kê; các chênh lệch nhỏ có thể do biến động theo thời gian, nhiệt độ, cache hoặc tải nền. Thứ tự chạy cố định theo lịch lưu trong run. Các phân bố còn thay đổi working set và locality; benchmark không có thao tác nhiều dòng, đọc thuần hay think time. Không suy rộng thứ hạng sang mọi ứng dụng.", "",
              "Bảng dữ liệu nguồn được kiểm tra không đổi, nhưng DBMS/driver và host vẫn là thành phần của phép đo. Raw CSV, metadata môi trường, tài nguyên, bản mã nguồn và cleanup từng lượt được giữ cùng run để kiểm tra lại.", "",
              "## 7. Nguồn dữ liệu", "",
              f"- [Giao thức](experiments/e3_advanced_protocol.json) và [hướng dẫn](experiments/E3_ADVANCED_README.md)",
              f"- [Formal report]({rel}/report.json), [lịch chạy]({rel}/schedule.json), [48 dòng tổng hợp]({rel}/analysis/summary.csv), [48 dòng trial]({rel}/analysis/trials.csv)",
              f"- [Biên bản kiểm chứng và SHA-256 raw CSV]({rel}/analysis/validation.json)", ""]
    out = ROOT / "E3-advanced_Report.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(out)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("run", type=Path)
    build(p.parse_args().run.resolve())
