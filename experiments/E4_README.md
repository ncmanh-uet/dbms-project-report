# E4 — Deadlock, rollback, retry và thứ tự khóa

## Mục tiêu và dữ liệu

Mỗi logical transaction tăng s_ytd của cả hai stock rows: warehouse 1,
item 1 (A) và item 2 (B). Kịch bản ngược thứ tự tạo vòng chờ; kịch bản cùng
thứ tự loại bỏ vòng chờ này. Cả hai DBMS dùng READ COMMITTED cho các session
thí nghiệm. Bảng/cột gốc không bị thay đổi cấu trúc.

Giữ riêng hai bản ghi cho E4: không chạy workload khác hoặc sửa chúng từ
Workbench trong lúc demo. Script lưu baseline trước từng cặp transaction,
kiểm tra các mức tăng rồi khôi phục bằng UPDATE có điều kiện.

## Chạy trong Terminal VS Code

Mở Docker Desktop và đợi hai database sẵn sàng. Từ thư mục gốc dự án:

```powershell
# Demo ngắn: mỗi lịch một lượt trên mỗi DBMS
.\.venv\Scripts\python.exe .\experiments\e4_deadlock.py --rounds 1

# Mặc định: mỗi lịch 5 lượt, lần lượt PostgreSQL rồi MySQL
.\.venv\Scripts\python.exe .\experiments\e4_deadlock.py

# Chạy riêng một DBMS hoặc một lịch
.\.venv\Scripts\python.exe .\experiments\e4_deadlock.py --db postgres --mode opposite --rounds 1
.\.venv\Scripts\python.exe .\experiments\e4_deadlock.py --db mysql --mode ordered --rounds 1
```

Mỗi run mặc định có 20 cặp: 2 DBMS x 2 lịch x 5 lượt. --rounds nhận 1-20.
Không cần cấp quyền Performance Schema hay dùng root. Bằng chứng gồm lịch
SQL có kiểm soát, connection IDs và lỗi do chính DBMS trả về.

## Lịch opposite: tạo deadlock

1. Lưu baseline của A/B; mở hai connection độc lập và BEGIN cả hai.
2. Sau tín hiệu bắt đầu, T1 UPDATE A; T2 UPDATE B. Mỗi bên giữ khóa đầu tiên.
3. Barrier chỉ mở khi hai UPDATE đầu đã hoàn tất và cả hai đang giữ khóa.
4. T1 yêu cầu B, T2 yêu cầu A. Không có COMMIT/ROLLBACK tự nguyện trước đó.
5. DBMS phát hiện deadlock, hủy một transaction; bên còn lại ghi đủ hai row
   rồi COMMIT. Script không giả định trước T1 hay T2 sẽ là nạn nhân.
6. Connection lỗi thực hiện ROLLBACK rõ ràng để kết thúc trạng thái lỗi.
7. Sau khi hai worker đã kết thúc, A và B phải đều bằng baseline + 1.
   Mức tăng dở dang của transaction nạn nhân không được tồn tại.
8. Chạy lại toàn bộ request của nạn nhân trong transaction mới, gồm cả hai
   UPDATE theo thứ tự request ban đầu. Retry diễn ra sau khi survivor đã
   kết thúc; A và B phải đều bằng baseline + 2.
9. Kiểm tra và khôi phục cả hai về baseline.

Script chỉ công nhận PostgreSQL SQLSTATE 40P01 hoặc MySQL errno 1213 là
deadlock. PostgreSQL 55P03/MySQL 1205 là lock timeout, không được thay thế
cho bằng chứng deadlock. Nếu không có đúng một deadlock victim và một
survivor COMMIT, lượt opposite báo FAIL.

Retry là retry toàn transaction, không chỉ chạy lại câu UPDATE cuối bị lỗi.
Trong demo nó là một lần thử có kiểm soát sau khi tranh chấp đã kết thúc.

## Lịch ordered: phòng tránh

Hai request vẫn có thứ tự đầu vào khác nhau, nhưng trước khi chạy SQL,
cả hai đều sắp xếp row IDs tăng dần: A trước, B sau. Hai transaction BEGIN
và đợi chung tín hiệu bắt đầu; không có barrier sau khi giữ khóa đầu tiên.

Nếu T2 chờ A của T1, T1 vẫn lấy được B và hoàn tất. T2 sau đó tiếp tục.
Cả hai phải COMMIT, không deadlock và không retry; mỗi counter tăng 2 trước
khi được khôi phục. Kết luận phòng tránh giới hạn trong kịch bản hai row
và các transaction tuân thủ thứ tự này.

## Giới hạn thời gian và số đo

Script đọc cấu hình detector trước khi sửa dữ liệu: PostgreSQL deadlock_timeout
phải dưới 4 giây; MySQL innodb_deadlock_detect phải bật. Các session có guard
lock timeout 5 giây (statement timeout PostgreSQL 8 giây); không đổi cấu hình
toàn server. Barrier có timeout và được giải phóng khi có lỗi điều phối.

execution_ms của worker gồm UPDATE, chờ điều phối/detection và COMMIT hoặc
ROLLBACK. initial_pair_ms tính từ phát tín hiệu chạy đến khi cả hai worker
kết thúc, gồm đóng connection, không gồm retry/dọn dẹp. Retry ghi thời gian
riêng, gồm cả tạo connection mới. PostgreSQL có thể chờ deadlock_timeout
trước khi phát hiện, nên không dùng các latency này để xếp hạng tốc độ DBMS.
Đây là kiểm chứng lịch và cơ chế, không phải benchmark tải hiệu năng.

## Kiểm tra dữ liệu và dọn dẹp

Chỉ tính các COMMIT được xác nhận. Mỗi COMMIT thành công tương ứng hai
row increments. Trước khi khôi phục, script khóa A/B theo thứ tự cố định,
kiểm tra từng row bằng baseline + số transaction COMMIT đã xác nhận rồi mới
UPDATE về baseline và COMMIT. Sau đó SELECT lại kiểm chứng.

Các lỗi bất thường dừng run. Nếu delta không khớp, script báo FAIL và bỏ
qua ghi đè dữ liệu không dự kiến. Bản ghi baseline và log vẫn được lưu.
finally thực hiện dọn dẹp khi Python còn chạy; tắt cưỡng bức hoặc mất điện
có thể bỏ qua finally, khi đó cần đối chiếu baseline trước khi chạy lại.

## Bằng chứng

Mỗi run tạo `experiments/e4_logs/<UTC run id>/`:

- timeline.log/jsonl: BEGIN/UPDATE/COMMIT/ROLLBACK, thứ tự yêu cầu, connection
  IDs, SQLSTATE/errno và chi tiết deadlock nếu driver cung cấp.
- summary.json: toàn bộ kết quả, nạn nhân, retry, các trạng thái dữ liệu.
- rounds.csv: số deadlock, timeout, COMMIT, retry và kết quả khôi phục mỗi lượt.
- Mỗi cặp còn có baseline.json và summary.json.

Script dùng helper kết nối/log từ e1_mvcc.py và SQL/session từ e2_support.py.

## Nguồn đối chiếu

- PostgreSQL deadlocks:
  https://www.postgresql.org/docs/17/explicit-locking.html#LOCKING-DEADLOCKS
- PostgreSQL error codes:
  https://www.postgresql.org/docs/17/errcodes-appendix.html
- MySQL deadlock handling:
  https://dev.mysql.com/doc/refman/8.4/en/innodb-deadlocks-handling.html
