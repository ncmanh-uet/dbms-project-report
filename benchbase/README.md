# BenchBase trong Docker

BenchBase dùng để tạo schema, nạp dữ liệu TPC-C-like và chạy workload trên
PostgreSQL/MySQL. Python vẫn được dùng cho các demo transaction có lịch chạy
được điều khiển rõ ràng.

## Chạy từ thư mục dự án

```powershell
docker compose run --rm benchbase-postgres --help
docker compose run --rm benchbase-mysql --help
```

Hai service thuộc Compose profile `tools`, nên `docker compose up -d` thông
thường chỉ khởi động hai database. Chỉ định tên service trong `compose run`
sẽ chạy công cụ tương ứng. `--rm` xóa container công cụ sau khi chạy xong.

`--help` chỉ hiện hướng dẫn, không tạo bảng, nạp dữ liệu hay chạy benchmark.

Hai image BenchBase được cố định bằng digest SHA-256 trong `compose.yaml`,
để các lần chạy sau dùng đúng bản image đã kiểm tra.

## Kết nối và kết quả

- Từ container BenchBase: PostgreSQL ở `postgres:5432`, MySQL ở `mysql:3306`.
- Từ Windows: PostgreSQL ở `127.0.0.1:5432`, MySQL ở `127.0.0.1:3307`.
- Kết quả PostgreSQL: `benchbase/results/postgres/`.
- Kết quả MySQL: `benchbase/results/mysql/`.

Các thư mục kết quả được mount vào `/benchbase/results` của container.

## Cấu hình TPC-C-like: 1 warehouse

- PostgreSQL: `config/tpcc-postgres.xml`.
- MySQL: `config/tpcc-mysql.xml`.
- Cả hai: scale factor 1, random seed 20261023, loaderThreads 1, batch size 128.
- Các session do BenchBase mở dùng `TRANSACTION_READ_COMMITTED`.
- Thiết lập isolation này không thay đổi mặc định toàn server hoặc các
  connection Workbench/Python mở riêng.
- Cấu hình XML được mount chỉ đọc tại `/benchbase/lab-config`.

Phần `works` khai báo 1 terminal, 60 giây, rate 1000 và weights
45/43/4/4/4 theo thứ tự NewOrder/Payment/OrderStatus/Delivery/StockLevel.
Phần này chỉ được dùng khi bật execution. Bước khởi tạo dùng
`--execute=false`, nên không tạo ra kết quả benchmark hiệu năng.

### Các lệnh khởi tạo đã dùng

Các lệnh sau dành cho khởi tạo database trống. Không cần chạy lại sau khi
đã nạp xong; để kiểm tra dữ liệu hiện có, dùng script xác minh bên dưới.

```powershell
docker compose run --rm benchbase-postgres -b tpcc -c /benchbase/lab-config/tpcc-postgres.xml --create=true --load=true --execute=false
docker compose run --rm benchbase-mysql -b tpcc -c /benchbase/lab-config/tpcc-mysql.xml --create=true --load=true --execute=false
```

Log khởi tạo được lưu trong `logs/load-postgres.log` và `logs/load-mysql.log`.

### Xác minh dữ liệu sau khi nạp

Từ thư mục gốc dự án:

```powershell
.\.venv\Scripts\python.exe .\benchbase\verify_tpcc.py
```

Script chỉ đọc database, kiểm tra đúng 9 bảng, số bản ghi khởi tạo, tổng số
order lines so với `SUM(oorder.o_ol_cnt)`, giá trị `d_next_o_id`, khoảng tồn
kho ban đầu và engine InnoDB trên MySQL. Kết quả được lưu tại
`logs/dataset-verification.json`.

Đây là kiểm tra trạng thái khởi tạo, không phải toàn bộ kiểm định consistency
của TPC-C. Số lượng khởi tạo có thể thay đổi sau khi chạy workload. Hai bộ
dữ liệu có cùng quy mô; seed giống nhau không bảo đảm mọi cột giống hệt,
chẳng hạn các cột timestamp được tạo ở thời điểm khác nhau.

## Nguồn chính thức

- https://github.com/cmu-db/benchbase
- https://github.com/cmu-db/benchbase/blob/main/docker/benchbase/README.md
