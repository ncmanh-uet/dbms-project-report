# E3-advanced — hiệu năng cập nhật trên bản sao đầy đủ stock

E3-advanced bổ sung cho demo E3 và benchmark E6. Không thay đổi E3/E6 hay gọi TPS của ba workload là tương đương. Đây là microbenchmark cập nhật một dòng trên **đầy đủ bảng stock 100.000 dòng**, không phải năm transaction TPC-C.

## Thiết kế

- Hai engine PostgreSQL/MySQL; hai phương án `pessimistic`, `occ`.
- Clone toàn bộ cột, dữ liệu, khóa/chỉ mục bằng PostgreSQL `LIKE stock INCLUDING ALL` hoặc MySQL `CREATE TABLE ... LIKE stock`; sao chép dữ liệu rồi thêm `version BIGINT NOT NULL DEFAULT 1`. Mỗi lượt tạo một bảng có UUID, kiểm tra clone bằng SHA-256 toàn bộ giá trị theo thứ tự khóa **trước khi thêm version**. Tên chỉ mục có thể khác. Không ghi `stock`, không sửa baseline E6.
- Các dòng đích thuộc warehouse 1. `uniform100k`: chọn đều 1..100000; `shared100`: chọn đều 1..100; `hot1`: chỉ item 1. Các phân bố khác nhau về working set và locality, không chỉ xác suất xung đột.
- Mỗi yêu cầu tăng `s_ytd` thêm 1. Pessimistic: SELECT FOR UPDATE rồi UPDATE giá trị tính ở ứng dụng; OCC: SELECT thường, UPDATE kèm version và tăng version.
- Cùng bảng có version cho cả hai phương án; chỉ OCC cập nhật version. UPDATE vật lý của OCC vẫn có thể chờ khóa.
- READ COMMITTED; DBMS 2 vCPU/2 GiB, không swap thêm; max_connections=200. Lock timeout 5s. PostgreSQL statement timeout 15s, MySQL SELECT timeout 15s (khác phạm vi).
- Generator: tối đa bốn tiến trình Python trên **Windows host, không quota CPU/RAM**, mỗi process chứa một nhóm worker thread. Không dùng generator BenchBase. Thu CPU/RSS cây tiến trình để quan sát giới hạn generator.
- Bước khởi tạo chỉ mở tối đa tám connection cùng lúc để tránh đột biến xác thực; sau khi đủ connection độc lập mới bắt đầu pha tải với toàn bộ số client cấu hình. Đồng bộ bằng Queue/Event/Array của multiprocessing; timeout chuẩn bị 180s, timeout pha gồm thời gian tải + 120s để drain. Lỗi worker được ghi riêng; coordinator kết thúc worker của chính lượt khi có lỗi. Trong khi chạy, Windows nhận yêu cầu tạm thời không tự sleep; không đổi cấu hình nguồn điện lâu dài hay thiết lập màn hình.
- Client 1/8/32/128, một lần đo mỗi cấu hình; warm-up 15s, đo 60s; tổng 48 lượt. Chỉ một DBMS hoạt động trong mỗi lượt.
- Seed lựa chọn dòng theo repetition/client, riêng với seed backoff. Retry giữ nguyên dòng và phép tăng. Cùng seed không bảo đảm cùng chuỗi request trong pha đo vì số request warm-up khác nhau.

## Retry và định nghĩa số đo

Tối đa chín attempt (lần đầu + tám retry). Chỉ version conflict và deadlock đã xác định được retry. Backoff `min(1ms*2^k,20ms)*U(0.5,1.5)`; giá trị sau jitter tối đa 30ms. Lock timeout kết thúc yêu cầu và được báo riêng. Lỗi khác hoặc lỗi COMMIT không xác định khiến lượt chạy FAIL, không retry mù.

Warm-up được drain trước đo; connection giữ nguyên, không reset bảng/cache giữa hai pha. Đến hạn 60s, dừng nhận yêu cầu mới rồi drain các yêu cầu đã nhận. TPS = số yêu cầu thành công / max(60s, thời điểm hoàn tất cuối − thời điểm bắt đầu đo). Báo drain riêng. Không tính số attempt vào TPS. Độ trễ bắt đầu trước attempt đầu, bao gồm rollback/backoff/retry, kết thúc trước ghi CSV. Ghi CSV vẫn có overhead thông lượng.

P50/p95/p99 chính chỉ xét yêu cầu thành công; `all_outcomes_p95_ms` xét mọi kết quả. Tỷ lệ retry/exhausted lấy mẫu số là yêu cầu logic; conflict lấy mẫu số là attempt. Mỗi cấu hình chỉ có một giá trị đo; không ước lượng độ phân tán hoặc khoảng tin cậy.

Mỗi worker ghi CSV riêng, buffer 256 KiB, có cả warm-up và đo. Không tạo request log trong RAM vô hạn. Metadata và Docker stats lấy xấp xỉ 2s/lần, nhãn pha gần đúng; tỷ lệ/giá trị chờ khóa chỉ là mẫu tức thời. MySQL session count bao gồm connection thiết lập/observer; không coi nó là số client chính xác. `sessions.json` ghi riêng ID của đúng các connection workload. MySQL lock wait count có phạm vi engine, trong lượt không chạy workload khác.

## Kiểm tra và an toàn dữ liệu

Mỗi lượt kiểm tra tất cả 100.000 counter bằng giá trị nguồn + số COMMIT được xác nhận, bao gồm warm-up và đo; OCC version bằng 1 + số COMMIT trên dòng, hai phương án khác version giữ 1. So sánh hash đầy đủ stock và danh sách bảng trước/sau, xóa đúng bảng UUID do lượt sở hữu. Chỉ cấp tạm SELECT metadata MySQL nếu còn thiếu, có journal và thu hồi trong finally.

Fingerprint đầu/cuối toàn đợt gồm số dòng chín bảng nguồn và SHA-256 toàn bộ stock. Đây không phải hash đầy đủ mọi bảng TPC-C hoặc kiểm định toàn bộ nghiệp vụ. Không cần restore `tpcc_bench` vì E3-advanced không truy cập database đó.

Nếu bị kill cứng, finally có thể không chạy: kiểm tra `ownership.json`, `cleanup.json` và journal quyền trước khi tiếp tục. Không tự xóa bảng khác hoặc coi lượt dang dở là hợp lệ. Resume chỉ bỏ qua lượt PASS, giữ nguyên lịch và mã nguồn/protocol.

## Lệnh

Chạy trong PowerShell tại thư mục gốc dự án, Docker Desktop đã sẵn sàng:

```powershell
.\.venv\Scripts\python.exe experiments/e3_advanced.py plan
.\.venv\Scripts\python.exe -u experiments/e3_advanced.py smoke
.\.venv\Scripts\python.exe -u experiments/e3_advanced.py formal
# Với PostgreSQL 17.12 và binlog MySQL tắt như E6:
.\.venv\Scripts\python.exe -u experiments/e3_advanced.py formal --e6-config
# Tiếp tục cùng đợt nếu bị gián đoạn sau khi đã kiểm tra cleanup:
.\.venv\Scripts\python.exe -u experiments/e3_advanced.py formal --resume experiments/e3_advanced_logs/formal-<UTC>
# Chỉ phân tích đợt formal hoàn chỉnh, không chạy SQL:
.\.venv\Scripts\python.exe experiments/analyze_e3_advanced.py experiments/e3_advanced_logs/formal-<UTC>
```

Khi tiếp tục một đợt đã tạo với `--e6-config`, truyền lại cờ này cùng `--resume`. Cờ chỉ chọn cấu hình máy chủ như E6. Phép đo vẫn dùng bộ sinh tải Python và cập nhật bộ đếm theo giao thức E3-advanced.

Phụ thuộc thêm: `psutil` (telemetry host); numpy/matplotlib và hai driver đã có trong venv. Protocol: `e3_advanced_protocol.json`. Smoke gồm 12 lượt ngắn, phủ hai phương án, cả hai DBMS, phân bố rộng một client và hot row 8/128 client; không nhập số liệu smoke vào kết quả formal.

Đầu ra: `report.json`, lịch chạy, snapshot môi trường, hash nguồn; từng lượt có ownership/baseline/schema, worker CSV, samples, summary, confirmed increments, cleanup. Script phân tích xác minh lại request CSV và xuất 48 dòng trial, 48 dòng summary, bốn biểu đồ PNG/SVG và validation chứa SHA-256.

Ba kiểm tra offline trong `test_e3_advanced.py` xác nhận: retry/warm-up không bị tính thành SUCCESS trong pha đo; counter thiếu một cập nhật bị đánh FAIL; lỗi connection được chuyển về coordinator và lưu file lỗi. Các log smoke/formal của ma trận ba phương án trước đây đã được dọn. Ma trận hai phương án hiện chưa chạy; chỉ tạo log mới sau khi có lệnh bắt đầu.

## Giới hạn diễn giải

Không xếp hạng MVCC như chế độ độc lập. Không có xử lý nghiệp vụ dài hoặc think time; khác biệt số round-trip và Python/driver/Windows–Docker network thuộc kết quả end-to-end này. Cùng quota DB không đảm bảo durability/cache tương đương tuyệt đối. Cần đọc TPS cùng p95, completion, retry/exhaustion và tài nguyên; không kết luận OCC thắng/thua chỉ từ một chỉ tiêu.


