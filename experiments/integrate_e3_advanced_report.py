"""Insert verified E3-advanced findings into the two project summary reports."""
import argparse
import csv
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MARKER = "<!-- E3-advanced formal 48 trials -->"


def fmt(value, digits=1):
    return f"{float(value):,.{digits}f}".replace(",", "_").replace(".", ",").replace("_", ".")


def markdown_table(headers, rows):
    return "| " + " | ".join(headers) + " |\n| " + " | ".join("---" for _ in headers) + " |\n" + "\n".join("| " + " | ".join(map(str, row)) + " |" for row in rows) + "\n"


def insert(path, before, section, backup):
    original = path.read_text(encoding="utf-8")
    if MARKER in original:
        raise RuntimeError(f"E3-advanced already integrated in {path}")
    if original.count(before) != 1:
        raise RuntimeError(f"Expected one insertion point in {path}: {before}")
    backup.write_text(original, encoding="utf-8")
    path.write_text(original.replace(before, section + "\n" + before, 1), encoding="utf-8")


def integrate(run):
    report = json.loads((run / "report.json").read_text(encoding="utf-8"))
    validation = json.loads((run / "analysis/validation.json").read_text(encoding="utf-8"))
    if not (report["status"] == validation["status"] == "PASS" and validation["trials"] == 48 and
            validation["configurations"] == 48 and report["e6_unchanged"] and all(report["source_unchanged"].values())):
        raise RuntimeError("Formal E3-advanced verification did not pass")
    if not (ROOT / "E3-advanced_Report.md").exists():
        raise RuntimeError("Detailed E3-advanced report is missing")
    with (run / "analysis/summary.csv").open(encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    data = {(r["dbms"], r["distribution"], r["method"], int(r["clients"])): r for r in rows}
    if len(data) != 48:
        raise RuntimeError("Expected 48 unique summary rows")
    rel = run.relative_to(ROOT).as_posix()
    chapter_rows = []
    cross_rows = []
    for dist in ("uniform100k", "shared100", "hot1"):
        for method, label in (("pessimistic", "Khóa bi quan"), ("occ", "OCC")):
            pg = data[("postgres", dist, method, 128)]
            my = data[("mysql", dist, method, 128)]
            chapter_rows.append([dist, label, fmt(pg["successful_tps_mean"]), fmt(pg["p95_ms_mean"]), fmt(pg["completion_pct_mean"], 2), fmt(pg["retry_pct_mean"], 2)])
            cross_rows.append([dist, label, fmt(pg["successful_tps_mean"]), fmt(my["successful_tps_mean"]), fmt(pg["completion_pct_mean"], 2), fmt(my["completion_pct_mean"], 2)])
    chapter = f"""{MARKER}
### 3.1.4. E3-advanced: OCC và khóa bi quan trên bản sao đầy đủ stock

E3-advanced so sánh cùng thao tác tăng bộ đếm `s_ytd` bằng OCC và `SELECT FOR UPDATE` trên PostgreSQL/MySQL. Ma trận có **2 DBMS × 2 phương pháp × 3 phân bố × 4 mức client × 1 lần đo = 48 lượt**. Mỗi lượt dùng bản sao 100.000 dòng `stock`, warm-up 15 giây, đo 60 giây rồi drain; mỗi DBMS giới hạn 2 vCPU và 2 GiB RAM. Phân bố `uniform100k` chọn đều 100.000 dòng, `shared100` chọn 100 dòng và `hot1` chỉ một dòng. Đây là microbenchmark riêng, không gộp TPS với E6.

**Bảng E3A.1. Kết quả E3-advanced trên PostgreSQL tại 128 client (một lượt mỗi cấu hình).**

{markdown_table(["Phân bố", "Phương pháp", "TPS", "p95 thành công (ms)", "Hoàn tất (%)", "Retry (%)"], chapter_rows)}
Cả **48/48 lượt PASS** sau kiểm tra counter của 100.000 dòng, cleanup bảng tạm và đối chiếu raw CSV. Fingerprint nguồn PostgreSQL/MySQL và hash kết quả E6 không đổi. Khi chuyển từ `uniform100k` sang `shared100` rồi `hot1`, OCC gặp nhiều xung đột hơn. Ở `hot1`/128 client, OCC hoàn tất **39,97%** yêu cầu trên PostgreSQL và **7,35%** trên MySQL; phần lớn yêu cầu còn lại hết ngân sách retry. P95 chỉ tính trên yêu cầu thành công nên phải đọc cùng tỷ lệ hoàn tất.

**Bảng E3A.2. Đối chiếu hai DBMS tại 128 client (TPS và tỷ lệ hoàn tất).**

{markdown_table(["Phân bố", "Phương pháp", "TPS PostgreSQL", "TPS MySQL", "Hoàn tất PostgreSQL (%)", "Hoàn tất MySQL (%)"], cross_rows)}

Mỗi cấu hình chỉ được đo một lần; không có ước lượng biến động hoặc kiểm định ý nghĩa thống kê. Workload chỉ cập nhật một dòng, và phép đo end-to-end còn gồm driver, generator Python và giao tiếp Windows–Docker. Kết quả này không xếp hạng chung OCC với khóa bi quan cho mọi ứng dụng hoặc xếp hạng hai DBMS độc lập với môi trường.

Nguồn: [báo cáo chi tiết](E3-advanced_Report.md), [48 lượt formal]({rel}/report.json), [48 cấu hình tổng hợp]({rel}/analysis/summary.csv), [biên bản kiểm chứng]({rel}/analysis/validation.json).
"""
    discussion = f"""{MARKER}
### 4.4. E3-advanced: cùng thao tác cập nhật, mức tranh chấp khác nhau

Đợt E3-advanced đã hoàn tất **48/48 lượt**, gồm PostgreSQL/MySQL, OCC/khóa bi quan, ba phân bố truy cập và 1/8/32/128 client; mỗi cấu hình đo một lần. Cả hai nguồn stock giữ nguyên sau cleanup. Tập số liệu này đo thao tác tăng một bộ đếm trên bản sao đầy đủ 100.000 dòng, tách khỏi workload hỗn hợp E6.

Ở `uniform100k`, retry OCC dưới 0,1% ở mọi mức client. Khi 128 client chia sẻ 100 dòng, retry OCC tăng lên **37,93%** trên PostgreSQL và **38,98%** trên MySQL. Với `hot1`/128 client, OCC chỉ hoàn tất **39,97%** yêu cầu trên PostgreSQL và **7,35%** trên MySQL, do hết ngân sách retry; khóa bi quan hoàn tất gần như toàn bộ yêu cầu ở điểm này nhưng độ trễ chờ khóa cao. Vì chỉ đo một lần mỗi cấu hình, các chênh lệch nhỏ không phải bằng chứng ổn định về hiệu năng.

Xem [báo cáo E3-advanced](E3-advanced_Report.md), [kết quả 48 lượt]({rel}/analysis/trials.csv) và [kiểm chứng raw CSV]({rel}/analysis/validation.json). Không gộp TPS hoặc số yêu cầu của E3-advanced với E6.
"""
    insert(ROOT / "Phan_3_Thuc_nghiem_va_so_sanh.md", "## 3.2. So sánh với hệ quản trị cơ sở dữ liệu truyền thống", chapter, run / "chapter-before-integration.md")
    insert(ROOT / "Experimental_Results_and_Discussion.md", "## 5. Trả lời câu hỏi nghiên cứu và kết luận có giới hạn", discussion, run / "discussion-before-integration.md")
    print("Updated both summary reports")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("run", type=Path)
    integrate(p.parse_args().run.resolve())
