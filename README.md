# Tái lập báo cáo kiểm soát đồng thời PostgreSQL và MySQL

Báo cáo dùng E1–E4 để kiểm chứng lịch giao dịch, E6 để so sánh RC/RR trong từng DBMS và E3-advanced để đối chiếu OCC ở tầng ứng dụng với khóa bi quan. Chạy trong môi trường lab với Docker Compose.

## Cài đặt và kiểm chứng lịch giao dịch

1. Mở [cấu hình Compose](compose.yaml), khởi động PostgreSQL và MySQL rồi làm theo [hướng dẫn tạo dữ liệu](benchbase/README.md). Xem [kiểm chứng dữ liệu ban đầu](benchbase/logs/dataset-verification.json) và [nhật ký demo SQL và phục hồi](experiments/demo_logs/20261126T044320Z.json).
2. Chạy theo hướng dẫn [E1](experiments/E1_README.md), [E2](experiments/E2_README.md), [E3](experiments/E3_README.md) và [E4](experiments/E4_README.md).
3. Đối chiếu [E1](experiments/e1_logs/20261125T030706_536131Z), [E2a](experiments/e2_logs/20261125T030708_295092Z), [24 lượt E2b](experiments/e2_logs/20261109T152250_363110Z/trials.csv), [E3 ở RC](experiments/e3_logs/20261109T161429_704849Z), [E3 ở RR](experiments/e3_logs/20261126T044311_200564Z), [E4](experiments/e4_logs/20261123T075540_787330Z) và [kiểm chứng sau lượt](experiments/e124_verification/20261125/verification.json).

## E6: mức cô lập dưới tải mô phỏng TPC-C

Làm theo [giao thức BenchBase](benchbase/benchmark/README.md) để chuẩn bị baseline và bộ đo. Dùng `build_metrics.py --verify-isolation` và chạy smoke với `run_benchmark.py smoke --compare-isolation` trước lệnh `run_benchmark.py run --compare-isolation`. Để chạy lại lượt 3, dùng `--first-repetition 3 --repetitions 1`, rồi tổng hợp với `analyze_isolation.py --replace-third-from` theo hướng dẫn.

- Cấu hình: [PostgreSQL XML](benchbase/config/tpcc-postgres.xml), [MySQL XML](benchbase/config/tpcc-mysql.xml), [Compose benchmark](benchbase/benchmark/compose.isolation.yaml), [phiên MySQL](benchbase/benchmark/mysql-session.sql), [môi trường thực đo](benchbase/benchmark/isolation/formal-20261126T030922514842Z/environment.json).
- Nguồn bảng, hình: [từng lượt](benchbase/benchmark/isolation/formal-20261126T030922514842Z/trials.csv), [tổng hợp](benchbase/benchmark/isolation/formal-20261126T030922514842Z/summary.csv), [ghép cặp](benchbase/benchmark/isolation/formal-20261126T030922514842Z/paired.csv), [theo loại giao dịch](benchbase/benchmark/isolation/formal-20261126T030922514842Z/transaction-mix.csv), [kiểm chứng](benchbase/benchmark/isolation/formal-20261126T030922514842Z/verification.json), [nhật ký yêu cầu](benchbase/benchmark/isolation/formal-20261126T030922514842Z/request-logs.zip).
- Kết quả sử dụng lượt 1–2 từ [đợt gốc ngày 25/11](benchbase/benchmark/isolation/formal-20261125T052730347909Z/trials.csv) và lượt 3 chạy lại ngày 26/11.

## E3-advanced: OCC và khóa bi quan

Làm theo [hướng dẫn thực nghiệm](experiments/E3_ADVANCED_README.md), chạy `e3_advanced.py formal --e6-config`. Báo cáo dùng [giao thức và lịch đợt 26/11](experiments/e3_advanced_logs/formal-20261126T033205Z/report.json), [tổng hợp 48 lượt](experiments/e3_advanced_logs/formal-20261126T033205Z/analysis/summary.csv), [số liệu từng lượt](experiments/e3_advanced_logs/formal-20261126T033205Z/analysis/trials.csv), [kiểm chứng](experiments/e3_advanced_logs/formal-20261126T033205Z/analysis/validation.json) và [nhật ký gốc](experiments/e3_advanced_logs/formal-20261126T033205Z/run-logs.zip).
