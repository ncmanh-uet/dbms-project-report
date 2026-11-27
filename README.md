# Tái lập báo cáo kiểm soát đồng thời PostgreSQL và MySQL

Báo cáo dùng E1–E4 để kiểm chứng lịch giao dịch, E6 để so sánh RC/RR trong từng DBMS và E3-advanced để đối chiếu OCC ở tầng ứng dụng với khóa bi quan. Chạy trong môi trường lab với Docker Compose.

## Cài đặt và kiểm chứng lịch giao dịch

1. Mở [cấu hình Compose](compose.yaml), khởi động PostgreSQL và MySQL rồi làm theo [hướng dẫn tạo dữ liệu](benchbase/README.md). Xem [kiểm chứng dữ liệu ban đầu](benchbase/logs/dataset-verification.json) và nhật ký demo của chương 1: [PostgreSQL, giao dịch và phục hồi](experiments/demo_logs/20261126T044320Z.json), [MySQL InnoDB và pg_dump/pg_restore](experiments/demo_logs/20261127_mysql_innodb_va_sao_luu.txt).
2. Chạy theo hướng dẫn [E1](experiments/E1_README.md), [E2](experiments/E2_README.md), [E3](experiments/E3_README.md) và [E4](experiments/E4_README.md).
3. Đối chiếu [E1](experiments/e1_logs/20261125T030706_536131Z), [E2a](experiments/e2_logs/20261125T030708_295092Z), [24 lượt E2b](experiments/e2_logs/20261109T152250_363110Z/trials.csv), [E3 ở RC](experiments/e3_logs/20261109T161429_704849Z), [E3 ở RR](experiments/e3_logs/20261126T044311_200564Z), [E4](experiments/e4_logs/20261123T075540_787330Z) và [kiểm chứng sau lượt](experiments/e124_verification/20261125/verification.json).

## E6: mức cô lập dưới tải mô phỏng TPC-C

Làm theo [giao thức BenchBase](benchbase/benchmark/README.md) để chuẩn bị baseline và bộ đo. Dùng `build_metrics.py --verify-isolation` và chạy smoke với `run_benchmark.py smoke --compare-isolation` trước lệnh `run_benchmark.py run --compare-isolation`. Để chạy lại lượt 3, dùng `--first-repetition 3 --repetitions 1`, rồi tổng hợp với `analyze_isolation.py --replace-third-from` theo hướng dẫn.

- Cấu hình: [PostgreSQL XML](benchbase/config/tpcc-postgres.xml), [MySQL XML](benchbase/config/tpcc-mysql.xml), [Compose benchmark](benchbase/benchmark/compose.isolation.yaml), [phiên MySQL](benchbase/benchmark/mysql-session.sql), [môi trường thực đo](benchbase/benchmark/isolation/formal-20261126T030922514842Z/environment.json).
- Nguồn bảng, hình: [từng lượt](benchbase/benchmark/isolation/formal-20261126T030922514842Z/trials.csv), [tổng hợp](benchbase/benchmark/isolation/formal-20261126T030922514842Z/summary.csv), [ghép cặp](benchbase/benchmark/isolation/formal-20261126T030922514842Z/paired.csv), [theo loại giao dịch](benchbase/benchmark/isolation/formal-20261126T030922514842Z/transaction-mix.csv), [kiểm chứng](benchbase/benchmark/isolation/formal-20261126T030922514842Z/verification.json), [nhật ký yêu cầu](benchbase/benchmark/isolation/formal-20261126T030922514842Z/request-logs.zip).
- Kết quả sử dụng lượt 1–2 từ [đợt gốc ngày 25/11](benchbase/benchmark/isolation/formal-20261125T052730347909Z/trials.csv) và lượt 3 chạy lại ngày 26/11. Bảng ghép cặp của đợt gốc (cả ba lượt gốc) nằm ở [paired.csv](benchbase/benchmark/isolation/formal-20261125T052730347909Z/paired.csv).
- Bản vá BenchBase: [Worker.patch](benchbase/benchmark/isolation/formal-20261126T030922514842Z/instrumentation/Worker.patch), [instrumentation.json](benchbase/benchmark/isolation/formal-20261126T030922514842Z/instrumentation/instrumentation.json) và [RequestMetrics.java](benchbase/benchmark/java/com/oltpbenchmark/lab/RequestMetrics.java).
- Thống kê theo loại giao dịch tạo lại từ `request-logs.zip` bằng [summarize_transaction_mix.py](benchbase/benchmark/summarize_transaction_mix.py): `python benchbase/benchmark/summarize_transaction_mix.py <request-logs.zip> transaction-mix.csv request-status.csv`. Tệp [request-status.csv](benchbase/benchmark/isolation/formal-20261126T030922514842Z/request-status.csv) đếm yêu cầu theo loại giao dịch, trạng thái cuối và SQLSTATE cuối.
- Với 12 lượt chạy lại ngày 26/11, thư mục kết quả chỉ giữ `run.json`, `metrics.json`, `workload.xml`, `db-samples.csv` và `resources.jsonl`. Các tệp `benchbase.log`, `native/`, `timeline` và `source-fingerprint` của 12 lượt này không còn.

## E3-advanced: OCC và khóa bi quan

Làm theo [hướng dẫn thực nghiệm](experiments/E3_ADVANCED_README.md), chạy `e3_advanced.py formal --e6-config`. Báo cáo dùng [giao thức và lịch đợt 26/11](experiments/e3_advanced_logs/formal-20261126T033205Z/report.json), [tổng hợp 48 lượt](experiments/e3_advanced_logs/formal-20261126T033205Z/analysis/summary.csv), [số liệu từng lượt](experiments/e3_advanced_logs/formal-20261126T033205Z/analysis/trials.csv), [kiểm chứng](experiments/e3_advanced_logs/formal-20261126T033205Z/analysis/validation.json) và [nhật ký gốc](experiments/e3_advanced_logs/formal-20261126T033205Z/run-logs.zip).

## Tài liệu nháp

Các tệp Markdown ở thư mục gốc (`Phan_3_Thuc_nghiem_va_so_sanh.md`, `Experimental_Results_and_Discussion.md`, `E3-advanced_Report.md`, `High_Concurrency_Experimental_Plan.md`) là bản nháp và kế hoạch từ các đợt chạy trước. Chúng dùng số liệu của đợt ngày 10/11 và đợt E3-advanced ngày 16/11 (`formal-20261116T061602Z`), và gọi MVCC, khóa, OCC là ba cơ chế. Báo cáo cuối không dùng các số liệu này và không phân loại như vậy. Nguồn của báo cáo cuối là các tệp được liệt kê ở trên.
