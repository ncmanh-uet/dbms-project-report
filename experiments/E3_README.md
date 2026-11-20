# E3 — Lost update → kiểm tra version → retry

## Mục tiêu

Một câu chuyện gồm ba giai đoạn: lỗi đọc–tính–ghi làm mất cập nhật; OCC
phát hiện dữ liệu đã cũ; retry đọc lại để hoàn tất yêu cầu đúng.

## Chạy trong Terminal VS Code

Mở Docker Desktop, đợi hai database sẵn sàng rồi chạy từ thư mục dự án:

```powershell
.\.venv\Scripts\python.exe .\experiments\e3_occ.py --mode demo
```

Các lựa chọn khác:

```powershell
# Demo và 16 lượt OCC sơ bộ, thường khoảng 3 phút
.\.venv\Scripts\python.exe .\experiments\e3_occ.py

# Chỉ phần nhiều client
.\.venv\Scripts\python.exe .\experiments\e3_occ.py --mode load

# Chạy riêng một DBMS
.\.venv\Scripts\python.exe .\experiments\e3_occ.py --db postgres --mode demo
.\.venv\Scripts\python.exe .\experiments\e3_occ.py --db mysql --mode demo

# Smoke test ngắn
.\.venv\Scripts\python.exe .\experiments\e3_occ.py --clients 1 4 --seconds 0.4
```

## Dữ liệu demo và quyền

Mỗi DBMS có một bảng mới tên `e3_occ_<mã ngẫu nhiên>`, chỉ thuộc lượt chạy.
Script chép ID và s_ytd của 16 stock rows (warehouse 1, items 1-16) sang
counter_value, thêm version ban đầu bằng 1. Các connection cùng truy cập
bảng này; nó là bảng thường có vòng đời ngắn, không phải TEMPORARY TABLE
chỉ thuộc một connection.

Không ALTER hay UPDATE bảng stock gốc. Mỗi giai đoạn reset bản sao về
counter ban đầu và version 1. Khi kết thúc, script xóa đúng bảng do lượt
chạy tạo, đối chiếu lại stock nguồn và danh sách bảng ban đầu. Không cần
cấp quyền đọc Performance Schema hoặc dùng root; các thao tác nằm trong
quyền database sẵn có của labuser.

Nếu tiến trình bị tắt cưỡng bức khiến finally không chạy, tên bảng demo
được ghi trong source-before.json để kiểm tra. Không tự xóa các bảng khác.

## Ba giai đoạn demo

Cả hai connection dùng READ COMMITTED, mỗi bên muốn tăng counter thêm 1.
Gọi giá trị nguồn là B. Hai transaction được BEGIN và đều SELECT trước
khi có bên nào ghi. Python gửi SQL theo lịch định trước trên hai connection
độc lập; transaction tồn tại chồng lấn dù phần demo không cần hai luồng.

1. **lost_update:** cả hai đọc B và tự tính B+1. T1 ghi rồi commit; T2 ghi
   giá trị B+1 đã tính trước rồi commit. Hai yêu cầu được báo thành công
   nhưng counter chỉ tăng một đơn vị. PASS ở giai đoạn này nghĩa là tái hiện
   được lỗi mong đợi, không có nghĩa dữ liệu nghiệp vụ đúng.
2. **occ_detect:** T1 và T2 đọc version 1. T1 cập nhật bằng điều kiện version,
   tăng version lên 2 và commit. T2 ghi với version 1 bị trả 0 affected rows,
   rollback và báo conflict. Chỉ một yêu cầu được chấp nhận, counter là B+1.
3. **occ_retry:** sau cùng tình huống conflict, T2 mở transaction mới, đọc
   B+1/version 2, tính lại, ghi có điều kiện và commit. Counter là B+2,
   version 3. Demo cố định có đúng một lần retry sau một conflict.

CAS (compare-and-set) trong cả demo OCC và phần nhiều client:

```sql
UPDATE e3_occ_<run>
SET counter_value = :value_computed_from_read,
    version = version + 1
WHERE s_w_id = 1 AND s_i_id = :item AND version = :expected_version;
```

Version là cột ứng dụng quản lý. Mỗi lần retry rollback transaction cũ,
đọc lại và tính lại; không gửi lại nguyên giá trị/version đã lỗi thời.
UPDATE vẫn có thể chờ khóa vật lý; version check quyết định có chấp nhận
kết quả tính từ dữ liệu đã đọc hay không.

MySQL có thể báo 0 affected rows ở nhánh lost_update khi ghi lại đúng giá
trị đang có. Đây chưa phải bằng chứng OCC. Với CAS, version luôn tăng nếu
điều kiện khớp; vì vậy 0 thể hiện không có row khớp version trong fixture này.

## OCC với nhiều client

Mặc định: PostgreSQL rồi MySQL; hai phân bố `distinct` (mỗi client một
item) và `hot1` (cùng item 1); 1/4/8/16 client, 10 giây/cấu hình. Chỉ OCC
được chạy ở phần này. Mỗi logical operation là một yêu cầu tăng thêm 1.

`--max-retries` áp dụng cho phần nhiều client, mặc định 8: tối đa 1 attempt
ban đầu + 8 lần thử lại. Backoff sau conflict là min(1 ms * 2^k, 20 ms),
nhân jitter 0.5-1.5; seed theo client là 20261109 + client_id. Thời hạn
duration dừng việc nhận logical operation mới; operation đang chạy được
drain đến khi thành công hoặc hết retry. SQL errors và COMMIT không rõ
kết quả là lỗi lượt chạy, được ghi riêng với version conflict.

Metrics:

- successful_tps: logical operations COMMIT thành công / thời gian chạy
  thực tế, gồm drain, không gồm setup/reset/xóa fixture.
- latency logical operation: từ trước attempt đầu đến khi kết thúc, gồm
  các attempt, rollback, backoff và retry. Percentile chỉ tính operation OK.
- attempts: số transaction attempts; version_conflicts: CAS trả 0.
- conflict_rate = version_conflicts / attempts.
- retries_started: số attempt thực sự bắt đầu sau attempt đầu.
- retry_operation_rate: tỷ lệ logical operations có ít nhất một retry.
- retries_per_operation: số retries / logical operations, có thể lớn hơn 1.
- exhausted_operations: yêu cầu hết retry mà chưa thành công; không cộng
  những yêu cầu này vào counter hoặc successful TPS.

Sau mỗi lượt, mọi counter phải bằng B + số logical operations thành công
trên row đó, và version phải bằng 1 + số operation thành công. Vì thế lượt
có exhausted operations vẫn có thể PASS kiểm tra tính đúng đắn; số yêu cầu
không hoàn tất được báo rõ. Script reset bản sao và kiểm chứng trước lượt sau.

Đây là chạy sơ bộ một lần/cấu hình, không warm-up, chưa kiểm soát đầy đủ tài
nguyên/cache/I/O. Không dùng để kết luận hiệu năng chính thức của DBMS.

## Bằng chứng

`experiments/e3_logs/<UTC run id>/` chứa timeline.log/jsonl, summary.json
và trials.csv. Mỗi DBMS có source-before.json và cleanup.json. Mỗi demo
có summary.json; mỗi trial còn có operations.csv và attempts.csv.

Script tái sử dụng kết nối/log từ e1_mvcc.py và SQL/session/percentile từ
e2_support.py. Giữ các file này cùng thư mục experiments.

## Đối chiếu cơ chế

- https://www.postgresql.org/docs/17/transaction-iso.html
- https://dev.mysql.com/doc/refman/8.4/en/innodb-consistent-read.html
