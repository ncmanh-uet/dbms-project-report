# E1 — MVCC trên dữ liệu TPC-C-like

## Mục đích

Kiểm tra một plain SELECT có đọc được phiên bản đã commit khi transaction
khác đang giữ UPDATE chưa commit trên cùng bản ghi. Thí nghiệm dùng
`warehouse.w_ytd`, với `w_id = 1`, ở READ COMMITTED trên cả hai DBMS.

## Chạy trong Terminal VS Code

Mở Docker Desktop và đợi Engine sẵn sàng. Từ thư mục gốc dự án,
khởi động hai database nếu chúng chưa chạy:

```powershell
docker compose up -d postgres mysql
```

Khi hai database đã sẵn sàng nhận kết nối:

```powershell
.\.venv\Scripts\python.exe .\experiments\e1_mvcc.py
```

Chạy riêng từng hệ:

```powershell
.\.venv\Scripts\python.exe .\experiments\e1_mvcc.py --db postgres
.\.venv\Scripts\python.exe .\experiments\e1_mvcc.py --db mysql
```

Trong lúc demo, để riêng bản ghi warehouse 1 cho thí nghiệm; tạm dừng
workload và các thao tác sửa bản ghi này từ Workbench/công cụ khác.

## Lịch thực thi

1. T1 mở connection, đặt READ COMMITTED, bắt đầu transaction.
2. T1 đọc và khóa warehouse 1 bằng SELECT FOR UPDATE, lấy giá trị ban đầu B.
3. T1 tăng w_ytd thêm 100 và giữ transaction chưa commit.
4. T2 mở connection độc lập, đặt READ COMMITTED, bắt đầu transaction chỉ đọc.
5. T2 chạy plain SELECT, kiểm tra nhận B và phát tín hiệu đã đọc xong.
6. T1 chỉ COMMIT sau khi nhận tín hiệu đọc thành công từ T2.
7. T2 nhận tín hiệu T1 COMMIT thành công, đọc lại trong cùng transaction,
   kiểm tra nhận B + 100, rồi COMMIT và đóng connection.
8. T1 dùng transaction dọn dẹp riêng, khóa lại bản ghi, khôi phục B bằng
   UPDATE có điều kiện, COMMIT và đọc lại để kiểm chứng.

Python dùng Event để đồng bộ, không dùng sleep để quyết định thứ tự.
Connection T1 được sử dụng trên luồng chính; connection T2 được tạo,
sử dụng và đóng trong luồng đọc riêng. Không chia sẻ một connection giữa
hai transaction. Hai DBMS được chạy lần lượt.

Nếu xảy ra lỗi trước COMMIT, script rollback T1 để giải phóng khóa. Script
có timeout cho truy vấn/chờ tín hiệu, ghi SQLSTATE khi driver cung cấp,
và vẫn thử dọn dẹp. Nếu giá trị dọn dẹp khác B và B + 100, script báo lỗi
và bỏ qua việc ghi đè. Luôn kiểm tra `restored` và `status` trong summary.

## Bằng chứng

Mỗi lần chạy tạo thư mục riêng `experiments/e1_logs/<UTC run id>/` gồm:

- `timeline.log`: SQL, tham số, kết quả, thời gian truy vấn, các mốc điều phối.
- `timeline.jsonl`: cùng sự kiện, kèm UTC timestamp, số thứ tự và elapsed_ms.
- `summary.json`: connection IDs, isolation, B, hai giá trị T2 đọc được,
  thời gian hai SELECT, kiểm tra thứ tự và giá trị sau khôi phục.

Lịch phải có thứ tự:
UPDATE_UNCOMMITTED → READ_BEFORE_COMMIT → COMMIT_START → COMMIT_DONE
→ READ_AFTER_COMMIT → RESTORED.

Tín hiệu quan trọng là SELECT đầu tiên hoàn tất trước khi T1 bắt đầu COMMIT.
Độ trễ một lần SELECT chỉ là số đo minh họa của demo, không dùng để kết luận
hệ nào nhanh hơn. Thí nghiệm tập trung vào plain SELECT; không suy rộng kết
quả sang locking reads hoặc DDL.

Lệnh chỉ thay đổi isolation của các connection thí nghiệm. Cấu hình mặc định
của server và các connection Workbench khác được quản lý riêng.

## Tài liệu đối chiếu

- PostgreSQL 17 Transaction Isolation:
  https://www.postgresql.org/docs/17/transaction-iso.html
- MySQL 8.4 Consistent Nonlocking Reads:
  https://dev.mysql.com/doc/refman/8.4/en/innodb-consistent-read.html
