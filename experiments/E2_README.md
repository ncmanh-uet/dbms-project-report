# E2 — Hot-row contention trên bảng stock

## Phạm vi bước 12

Demo write/write blocking và các lượt chạy sơ bộ để kiểm tra bộ tạo tải.
Các lượt này chưa phải benchmark chính thức: mỗi cấu hình chạy một lần,
không warm-up, chưa kiểm soát đầy đủ CPU/RAM/cache hay background I/O.

E2 dùng stock của warehouse 1, item IDs 1-16, không cần đổi schema TPC-C.
Mỗi transaction thực hiện BEGIN, câu UPDATE sau, rồi COMMIT:

```sql
UPDATE stock
SET s_ytd = s_ytd + 1
WHERE s_w_id = 1 AND s_i_id = ...;
```

Đây là thao tác tùy chỉnh trên dataset TPC-C-like, không phải transaction
mix TPC-C. Không chạy workload khác hoặc sửa 16 bản ghi này trong lúc demo.

## Chạy từ Terminal VS Code

Mở Docker Desktop và bảo đảm PostgreSQL/MySQL đã sẵn sàng. Từ thư mục gốc:

```powershell
docker compose up -d postgres mysql
.\.venv\Scripts\python.exe .\experiments\e2_contention.py
```

Lệnh mặc định chạy lần lượt PostgreSQL rồi MySQL, mỗi hệ gồm một demo
và 12 lượt sơ bộ: 3 phân bố x 4 mức client (1, 4, 8, 16), 10 giây/lượt.
Tổng thời gian thường hơn 4 phút vì có kết nối, drain và khôi phục dữ liệu.

Các lựa chọn:

```powershell
# Chỉ xem demo chờ khóa, trên cả hai hệ
.\.venv\Scripts\python.exe .\experiments\e2_contention.py --mode demo

# Chỉ chạy các lượt sơ bộ
.\.venv\Scripts\python.exe .\experiments\e2_contention.py --mode load

# Chạy riêng một hệ
.\.venv\Scripts\python.exe .\experiments\e2_contention.py --db postgres
.\.venv\Scripts\python.exe .\experiments\e2_contention.py --db mysql

# Smoke test ngắn để kiểm tra script
.\.venv\Scripts\python.exe .\experiments\e2_contention.py --clients 1 4 --seconds 0.4
```

## Demo T1/T2

1. Lưu giá trị s_ytd ban đầu B của stock (1,1).
2. T1 tăng bộ đếm một đơn vị và giữ transaction chưa commit.
3. T2 dùng connection riêng, cũng UPDATE bản ghi này.
4. Connection quan sát lấy metadata xác nhận T2 chờ T1. Hai mẫu phải cách
   nhau ít nhất 100 ms và T2 chưa hoàn tất.
5. Chỉ sau khi xác nhận chờ khóa, T1 mới bắt đầu COMMIT.
6. T2 hoàn tất UPDATE rồi COMMIT. Bộ đếm phải bằng B + 2.
7. Kiểm tra và khôi phục B, sau khi cả hai transaction đã kết thúc.

PostgreSQL quan sát pg_stat_activity và pg_blocking_pids. MySQL quan sát
performance_schema.data_lock_waits, data_locks và threads, gồm ID phiên,
index, loại khóa, trạng thái và dữ liệu khóa của stock.

Độ dài đoạn chờ quan sát được là khoảng giữa hai mẫu có cùng quan hệ
chặn. Nó là bằng chứng về một phần khoảng chờ, không phải bộ đếm chính xác
toàn bộ lock-wait time. Thời gian UPDATE trong demo bao gồm thời gian giữ
khóa có chủ đích này; không dùng nó để so tốc độ hai DBMS.

## Các lượt nhiều client

- `distinct`: mỗi client cập nhật một item riêng (1 đến số client), không
  cùng tranh chấp một stock row với client khác trong lượt này.
- `shared4`: các client luân phiên truy cập nhóm item 1-4 dùng chung.
- `hot1`: tất cả client cập nhật item 1.

Một client không tạo tranh chấp giữa các client; shared4 với một client
chỉ thay đổi working set. Nhãn phân bố không bảo đảm có wait trong mọi mẫu.

Tất cả connection dùng READ COMMITTED. Mỗi client đợi transaction kết thúc
rồi mới gửi transaction kế tiếp. Phần chạy nhiều client không thêm thời gian
giữ khóa hay think time có chủ đích, và không retry transaction bị lỗi.

TPS = số COMMIT được xác nhận thành công / thời gian thực tế từ lúc phát
tín hiệu bắt đầu đến khi client cuối kết thúc. Khoảng thời gian gồm drain
transaction đang chạy lúc hết duration; không gồm setup hoặc khôi phục.

Latency tính từ trước BEGIN đến khi nhận kết quả COMMIT, gồm network và
lock wait. Mean/p50/p95/p99 chỉ tính trên transaction thành công; percentile
dùng nội suy tuyến tính trên vị trí (n-1)*q. Các lần thất bại được lưu riêng
trong CSV; abort_rate là số lần thất bại / tổng attempts, retries bằng 0.

Connection quan sát lấy mẫu chờ khóa với khoảng nghỉ yêu cầu 50 ms; chu kỳ
thực tế còn phụ thuộc thời gian query và lập lịch. Các trường max/samples là
số phiên bị chặn quan sát được, không phải tổng số lock waits hoặc thời gian
chờ chính xác. Một mẫu 0 không chứng minh chưa từng có wait giữa hai mẫu.
Sampler cũng tạo overhead trong các lượt sơ bộ này.

## Tính đúng đắn và dọn dẹp

Trước mỗi lượt, lưu baseline của toàn bộ các row sẽ dùng. Sau khi dừng và
đóng tất cả client, khóa các row theo thứ tự và kiểm tra:

`giá trị hiện tại = baseline + số COMMIT được xác nhận trên row đó`.

Chỉ khi toàn bộ row khớp, script mới khôi phục bằng UPDATE có điều kiện,
COMMIT rồi đọc lại. Nếu có delta lạ hoặc COMMIT chưa xác định được kết quả,
lượt chạy báo FAIL; không tự ghi đè dữ liệu ngoài dự kiến. Mỗi lượt lưu
baseline và confirmed-commits để kiểm tra. Script dừng khi có lượt FAIL.

MySQL labuser ban đầu không có quyền đọc metadata khóa. Script dùng Docker
CLI và tài khoản root sẵn có trong container để cấp tạm SELECT chỉ trên ba
bảng Performance Schema ở trên. Password root được lấy từ môi trường bên
trong container, không đưa vào argv hay log. Các quyền đã có từ trước được
giữ nguyên; các quyền bổ sung được thu hồi trong finally và ghi journal.

## Bằng chứng được lưu

Mỗi lần chạy có thư mục `experiments/e2_logs/<UTC run id>/`:

- `timeline.log`, `timeline.jsonl`: session IDs, SQL demo, lock snapshots,
  các mốc chạy và khôi phục.
- `summary.json`: kết quả toàn lượt và kiểm tra dữ liệu.
- `trials.csv`: TPS, latency và số phiên bị chặn quan sát được mỗi cấu hình.
- `mysql-observer-permissions.json`: quyền SELECT thêm tạm và đã thu hồi.
- Mỗi thư mục demo/trial có `baseline.json`, `summary.json`.
- Mỗi trial còn có `transactions.csv`, `lock-samples.csv`, `confirmed-commits.json`.

Script dùng các helper kết nối/log từ e1_mvcc.py và helper E2 trong
e2_support.py; giữ các file này cùng thư mục experiments.

## Nguồn đối chiếu

- PostgreSQL blocking PID function:
  https://www.postgresql.org/docs/17/functions-info.html
- MySQL lock wait relationships:
  https://dev.mysql.com/doc/refman/8.4/en/performance-schema-data-lock-waits-table.html
- MySQL lock metadata:
  https://dev.mysql.com/doc/refman/8.4/en/performance-schema-data-locks-table.html
