# Bước 18 — Tổng hợp và biểu đồ từ dữ liệu đã đo

Đã tổng hợp đủ **42 lượt formal → 14 cấu hình DBMS/client**, mỗi cấu hình có **3 lần lặp**. Chỉ sử dụng workload TPC-C-like 1 warehouse, READ COMMITTED, mix 45/43/4/4/4 đã chạy ở bước 17. Không đưa smoke test hoặc kết quả demo E1–E4 vào biểu đồ này.

**Kiểm tra bàn giao:** 5 unit test đạt; đọc lại và kiểm tra SHA-256 của 1.775 file đầu vào; CSV có đúng 42/14 dòng và đủ 3 lần lặp mỗi cấu hình. Đã chạy lại khi chặn tạo subprocess và kết nối mạng: 12 file CSV/PNG/SVG có SHA-256 giống lần trước. Cả 5 PNG đã được xem để kiểm tra nhãn/chú thích; 5 SVG parse hợp lệ. Kết quả kiểm tra được ghi trong `analysis-validation.json`.

## Chạy lại

Tại thư mục gốc dự án, dùng môi trường Python hiện có:

```powershell
.\.venv\Scripts\python.exe -B .\benchbase\benchmark\analyze_results.py
```

Script chỉ đọc file, **không gọi Docker, không kết nối database, không tạo workload**. Không cần chạy `verify_formal.py` trước mỗi lần phân tích: script đó kiểm tra cả database đang chạy, còn `analyze_results.py` đối chiếu dữ liệu đầu vào hoàn toàn offline.

Khi dựng lại môi trường Python (bản đã dùng: Python 3.14.3), cài các phiên bản đã ghi nhận:

```powershell
.\.venv\Scripts\python.exe -m pip install --no-cache-dir -r .\benchbase\benchmark\analysis-requirements.txt
```

Dùng `test_analyze_results.py` để kiểm tra các phép biến đổi quan trọng:

```powershell
.\.venv\Scripts\python.exe -B -m unittest discover -s .\benchbase\benchmark -p test_analyze_results.py -v
```

## File nào dùng cho việc gì?

- `../analyze_results.py`: kiểm tra dữ liệu thô, tính từng lượt, tổng hợp và vẽ hình. Không sửa nguồn bước 17.
- `../analysis-requirements.txt`: phiên bản thư viện vẽ và dependency đã sử dụng.
- `trials.csv`: **42 dòng**, mỗi dòng là một lượt; có DBMS, client, repetition và đường dẫn nguồn để tra lại.
- `summary.csv`: **14 dòng**, mỗi dòng là một DBMS/client; các chỉ số có hậu tố `_mean`, `_min`, `_max`, `_stdev`, các số đếm có hậu tố `_sum`.
- `analysis-validation.json`: trạng thái kiểm tra, tổng số request/outcome, SHA-256 của từng đầu vào/đầu ra, phiên bản thư viện và định nghĩa các phép tổng hợp. Chỉ dùng output khi trạng thái là `PASS`.
- Hai hình chính: `01_successful_tps.png/.svg` và `02_p95_latency.png/.svg`.
- Hình hỗ trợ: `03_lock_wait.png/.svg`, `04_abort_retry.png/.svg`, `05_cpu_memory.png/.svg`.
- `.mpl-cache/`: font cache nhỏ của Matplotlib; có thể tạo lại, đã gitignore.

PNG dùng để chèn nhanh vào slide; SVG giữ đường nét vector để xuất báo cáo. Hình được xuất từ cùng số liệu CSV, không nhập số minh họa bằng tay. Giữ đúng tỷ lệ khi chèn hình.

## Cách đọc hai hình chính

Mỗi màu là một DBMS. Chấm nhỏ là từng lần lặp (dịch nhẹ theo chiều ngang để đọc được các chấm trùng); chấm lớn và đường là trung bình 3 lượt, thanh dọc là **min–max**. Thanh này **không phải khoảng tin cậy**. Không loại lượt chậm hoặc chọn lượt tốt nhất.

Trục client dùng **log₂**, có ghi rõ nhãn 1/4/8/16/32/64/128. Hình TPS có trục tung tuyến tính bắt đầu từ 0. Hình p95 có trục tung log để đọc được cả vùng vài ms và vài nghìn ms; khoảng cách trên trục này biểu thị tỷ lệ, không phải chênh lệch ms bằng nhau.

Successful TPS của một lượt là `số SUCCESS / thời gian đo thực tế trong native summary`. Không dùng goodput gốc đếm lẫn warm-up. Đây là số request thành công của cả mix, **không phải tpmC** (số NewOrder mỗi phút) được chứng nhận.

p50/p95/p99 tính riêng trên latency của request thành công trong mỗi lượt, theo nội suy tuyến tính `(n−1)q`. Thời gian bao gồm retry bên trong `doWork`, không gồm hàng đợi trước worker hoặc thời gian ghi CSV. Điểm p95 tổng hợp là **trung bình của ba p95 từng lượt**, không phải p95 của toàn bộ request gộp lại. Giữ p95 từng lượt trong `trials.csv`.

## Định nghĩa các cột quan trọng

- `successful_tps`, `latency_p50_success_ms`, `latency_p95_success_ms`, `latency_p99_success_ms`: chỉ request cuối cùng SUCCESS; latency có đơn vị ms.
- `measured_requests`, `successful_requests`: số request đã đo, số thành công; không đếm mỗi retry thành request mới.
- `neworder_intentional_aborts`: NewOrder hủy do item cố ý không hợp lệ. `neworder_intentional_abort_pct_of_neworder` dùng **số request NewOrder** làm mẫu số.
- `delivery_conflict_aborts`: Delivery rollback khi đơn hàng đã bị Delivery khác nhận giữa SELECT và DELETE. `delivery_conflict_abort_pct_of_delivery` dùng **số request Delivery** làm mẫu số; `_pct_of_all` dùng mọi request đo. Không gọi nhóm này là NewOrder rollback cố ý.
- `sql_or_retry_failed_requests`: ERROR hoặc trạng thái retry/unknown còn lại khi request kết thúc. Giá trị 0 không có nghĩa Delivery không bị hủy.
- `requests_with_retry`: request bắt đầu nhiều hơn một lần thử; `retry_request_pct` dùng mọi request đo làm mẫu số. `reexecutions_started` đếm số lần thử thêm, `retry_events` đếm sự kiện lỗi được phân loại retryable.
- Tất cả cột có `_pct` dùng **phần trăm 0–100**, không phải tỷ lệ 0–1. Không nhân thêm 100 khi vẽ lại.
- Trong `summary.csv`, trung bình phần trăm là trung bình của **ba tỷ lệ từng lượt**, không phải tỷ lệ tính từ tổng gộp. `_sum` là tổng ba lượt; nếu tính tỷ lệ gộp từ `_sum` thì ghi rõ đó là tỷ lệ gộp.

Khóa cũ `expected_user_aborts` trong output bước 17 chứa toàn bộ USER_ABORTED. Script phân tích không sử dụng khóa đó để suy ra NewOrder/Delivery: nó đọc lại loại transaction và trạng thái trong CSV rồi đối chiếu biên bản bước 17.

## Lock wait và CPU/RAM

Chỉ lấy mẫu có nhãn `phase=measure`; bỏ `startup`, `warmup`, `drain`. Nhãn của SQL/resource sample lấy từ log được nhận ở máy điều phối, nên ranh giới chỉ gần đúng. Request CSV sử dụng điều kiện pha chính xác của BenchBase. Không suy diễn hai nguồn có ranh giới đồng bộ tuyệt đối.

- `mean_lock_waiting_sessions`: trung bình số session chờ lock trong các mẫu của lượt.
- `mean_waiting_session_pct`: trung bình `waiters/sessions × 100` của từng mẫu. Mẫu số là session được quan sát, không mặc định luôn bằng số client.
- Đây là mẫu trạng thái tức thời, **không phải tổng số lock waits hoặc tổng thời gian chờ**. CPU queue, I/O và các loại chờ khác không nằm trong chỉ số này.
- Các cột `*_samples_measure` và `*_sample_gap_max_s` cho phép kiểm tra độ phủ và chu kỳ lấy mẫu thực tế.
- `db_cpu_docker_pct_mean` và `generator_cpu_docker_pct_mean` giữ nguyên cách tính Docker: **100% = 1 vCPU**.
- `*_cpu_quota_pct_mean` chia Docker CPU% cho quota tương ứng: DBMS 2 vCPU, generator 4 vCPU. 100% sau chuẩn hóa tương ứng quota danh nghĩa. Giữ nguyên các giá trị vượt 100% nhẹ trong mẫu, không cắt số liệu.
- `*_memory_mib_mean/max` là Docker stats `MemUsage`, đổi sang MiB (1 MiB = 1.048.576 byte). Docker CLI Linux trừ phần file cache theo cơ chế của nó, nên đây không phải tổng RAM máy Windows hoặc RSS riêng từng tiến trình. Xem [Docker stats](https://docs.docker.com/reference/cli/docker/container/stats/).
- Trung bình CPU/RAM được tính từ mẫu trong từng lượt, rồi mới lấy mean/min/max giữa 3 lượt. Chấm/cột sai số trên hình RAM là phân tán của **trung bình RAM từng lượt**; `*_memory_mib_max` trong CSV giữ thêm peak của từng lượt.

## Quan sát mô tả để chuẩn bị bước 19

- PostgreSQL có TPS trung bình cao nhất trong các mức đã thử ở 4 client: khoảng **660,95 request/s**, min–max **641,77–697,81**. Sau đó TPS trung bình giảm khi tăng client.
- MySQL có TPS trung bình cao nhất trong các mức đã thử ở 8 client: khoảng **238,95 request/s**, min–max **230,57–250,64**. Các mức từ 4 client trở lên cần xem cùng độ phân tán; không gọi 8 client là điểm tối ưu đã được chứng minh.
- Ở 128 client, trung bình p95 từng lượt khoảng **2.545,62 ms** trên PostgreSQL và **1.526,48 ms** trên MySQL. Khoảng TPS ba lượt ở mức này có chồng lấn.
- Mức 16 client dao động rõ ở cả hai DBMS, đặc biệt lần lặp thứ ba. Giữ nguyên toàn bộ lượt; đây chưa phải bằng chứng về một nguyên nhân cụ thể như nhiệt CPU hay ứng dụng nền.

Các quan sát này chỉ áp dụng cho **workload, hardware/quota, isolation và bản BenchBase đã ghi nhận**. Có 3 lần lặp nên chưa tuyên bố ý nghĩa thống kê, chưa quy khác biệt riêng cho MVCC hoặc locking. Đây không phải phép so sánh ba chế độ MVCC/lock/OCC; không có workload OCC bằng cột version riêng. Các invariant đã kiểm tra cũng không thay thế kiểm định đầy đủ TPC-C.

Bước 19 sẽ chọn hình phù hợp thời lượng trình bày và viết phần diễn giải có căn cứ; hai hình chính đủ làm điểm xuất phát.
