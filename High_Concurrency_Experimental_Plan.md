# Experimental Plan — Handling High Concurrency and Transaction Contention in Database Systems

> **Đề tài:** Handling High Concurrency and Transaction Contention in Database Systems  
> **Hệ quản trị trọng tâm:** PostgreSQL  
> **Hệ quản trị đối chứng:** MySQL (InnoDB)  
> **Phạm vi tài liệu:** Thiết kế thí nghiệm, demo tình huống tranh chấp, đo hiệu năng và so sánh hai DBMS.  
> **Tiền đề:** Nhóm đã hoàn thành phần tổng quan vấn đề và cơ chế PostgreSQL (MVCC, Locking, Optimistic Concurrency Control).

## 1. Mục tiêu nghiên cứu và định hướng

Báo cáo đi theo cấu trúc **Problem → Cơ chế PostgreSQL → Thiết kế thí nghiệm → Demo các tình huống tranh chấp → Đo hiệu năng → So sánh MySQL → Phân tích mức độ phù hợp của PostgreSQL**.

Giai đoạn thực nghiệm trả lời ba câu hỏi lớn:

1. **Concurrency gây ra vấn đề gì?** Khi nhiều transaction đồng thời đọc và ghi một dữ liệu, blocking, lost update, deadlock, abort… xuất hiện như thế nào?
2. **PostgreSQL giải quyết như thế nào?** MVCC, row/table locking, optimistic concurrency và isolation levels tác động thế nào đến tính đúng đắn và hiệu năng?
3. **Khác biệt giữa PostgreSQL và MySQL InnoDB?** Khi workload và môi trường tương đương, hai DBMS khác gì về throughput, latency, lock contention và transaction failure? Nguyên nhân kỹ thuật của khác biệt là gì?

### 1.1. Thiết kế ba tầng thực nghiệm

- **Tầng 1 — Functional experiments:** Demo trực quan MVCC, Lock, Lost Update/OCC, Deadlock và isolation anomalies; kiểm tra dữ liệu cuối cùng.
- **Tầng 2 — Performance benchmarks:** Đo throughput, TPS, mean/p50/p95/p99 latency, lock wait, abort/retry rate và tài nguyên khi tăng mức độ đồng thời.
- **Tầng 3 — Cross-DBMS comparison:** Thực hiện workload tương đương trên PostgreSQL và MySQL InnoDB; đối chiếu số đo và lý giải cơ chế.

> Không đặt mục tiêu chứng minh PostgreSQL **luôn** nhanh hơn. Một kết quả có giá trị khoa học phải cho biết hệ nào phù hợp hơn **trong điều kiện nào, vì sao và đánh đổi điều gì**.

## 2. Lựa chọn dataset và workload

Cần phân biệt:

- **Dataset:** Các bảng và bản ghi (tài khoản, kho, đơn hàng, khách hàng).
- **Workload:** Các loại transaction, tần suất, read/write ratio, phân bố khóa truy cập và số client đồng thời.

Đối với transaction contention, **workload và phân bố truy cập dữ liệu** quan trọng không kém quy mô dataset: 1.000 tài khoản có hot row vẫn có thể tạo contention cao hơn hàng triệu bản ghi được truy cập phân tán.

### 2.1. Các lựa chọn

| Lựa chọn | Đặc điểm | Ưu điểm | Hạn chế | Khuyến nghị |
|---|---|---|---|---|
| **TPC-C** | Benchmark OLTP về kho hàng, đặt hàng, thanh toán và tồn kho | Nghiệp vụ gần thực tế, nhiều transaction, có công cụ tạo dữ liệu/workload | Setup, phân tích và tùy chỉnh phức tạp hơn | **Benchmark chính** |
| **SmallBank** | Các transaction tài khoản, chuyển tiền, ghi nợ/ghi có | Dễ hiểu, phù hợp đo contention và correctness | Phạm vi nghiệp vụ hẹp hơn TPC-C | **Phương án thay thế**, hoặc demo bổ sung |
| **pgbench** | Benchmark đi kèm PostgreSQL, có thể tự tạo dữ liệu | Rất dễ khởi động để đo sơ bộ PostgreSQL | Không phải bộ chạy đối chứng MySQL trực tiếp; cần công cụ/driver khác để so công bằng | **Smoke test / kiểm tra môi trường** |

**Quyết định đề xuất:**

- **Benchmark chuẩn:** TPC-C qua **BenchBase** trên PostgreSQL và MySQL.
- **Demo cơ chế:** Schema ngân hàng/tồn kho nhỏ, script SQL + Python điều phối transaction để tái hiện lịch thực thi.
- **Nếu nhóm thiếu thời gian hoặc phần cứng:** Dùng SmallBank làm workload benchmark chính; không bắt buộc triển khai đầy đủ cả TPC-C lẫn SmallBank.

**Lưu ý về chuẩn:** Khi dùng tên “TPC-C”, cần tách bạch workload **TPC-C-like** chạy bằng BenchBase với kết quả **TPC-C chính thức** được kiểm toán/chứng nhận. Tỷ lệ read/write tùy biến không phải là transaction mix tiêu chuẩn của TPC-C.

### 2.2. Nguồn tài liệu

- TPC benchmarks: https://www.tpc.org/information/benchmarks5.asp
- BenchBase source và hướng dẫn: https://github.com/cmu-db/benchbase
- PostgreSQL pgbench: https://www.postgresql.org/docs/current/pgbench.html

## 3. Research Questions và giả thuyết

| RQ | Câu hỏi | Thí nghiệm liên quan |
|---|---|---|
| **RQ1** | MVCC giúp read/write concurrency thế nào? | E1, E5 |
| **RQ2** | Contention trên hot rows ảnh hưởng TPS, latency và blocking thế nào? | E2, E6 |
| **RQ3** | Pessimistic vs optimistic strategies đánh đổi correctness, lock wait, retries và latency ra sao? | E3 |
| **RQ4** | Deadlock xảy ra và được xử lý ra sao; thứ tự khóa có giảm deadlock không? | E4 |
| **RQ5** | PostgreSQL và MySQL khác nhau thế nào trên workload tương đương? | E5, E6 và so sánh ở Chương 6 |

Giả thuyết dự kiến cần **kiểm chứng**, không viết như kết quả đã đo:

- H1: Plain SELECT trong MVCC thường không chờ UPDATE chưa commit trên cùng row mà đọc phiên bản phù hợp với snapshot.
- H2: Khi tăng hot-row contention, TPS có thể bão hòa/giảm và p95/p99 latency tăng, dù CPU chưa chạm 100%.
- H3: Optimistic version-check có thể có lợi khi tỷ lệ conflict thấp nhưng tạo nhiều retries khi conflict cao; pessimistic locking có thể ổn định hơn trong trường hợp hot row.
- H4: Quy ước lock theo một thứ tự cố định làm giảm deadlock trong bài toán hai tài khoản.
- H5: Khác biệt giữa PostgreSQL và MySQL phụ thuộc isolation, loại truy vấn, index và kiểu tranh chấp, không thể suy rộng từ một biểu đồ TPS.

## 4. Kiến trúc và công nghệ

```text
          BenchBase / Custom Python Scenario Runner
             |                           |
             v                           v
       PostgreSQL                    MySQL InnoDB
             |                           |
             +-------------+-------------+
                           |
                   Metrics Collection
            (TPS, latency, waits, aborts,
               retries, CPU, memory)
                           |
                           v
                CSV → Pandas/Matplotlib
                           |
                           v
                  Tables, Charts, Report
```

| Thành phần | Công cụ đề xuất |
|---|---|
| DBMS chính | PostgreSQL (pin phiên bản cụ thể) |
| DBMS đối chứng | MySQL InnoDB (pin phiên bản cụ thể) |
| Môi trường | Docker Compose |
| Benchmark tiêu chuẩn | BenchBase |
| Demo transaction có kiểm soát | Python + `psycopg` / `mysql-connector-python` hoặc driver phù hợp |
| Phân tích | Python, Pandas, Matplotlib |
| Quan sát PostgreSQL | `pg_stat_activity`, `pg_locks`, `pg_stat_database`, logs |
| Quan sát MySQL | `performance_schema.data_lock_waits`, `sys.innodb_lock_waits`, logs |

Không cần xây dựng web application đầy đủ: **database + scenario runner + benchmark framework + analysis scripts** là đủ cho mục tiêu học phần.

### 4.1. Kiểm soát biến khi benchmark

- Chạy hai DBMS **lần lượt** trên cùng host, tránh cạnh tranh tài nguyên trực tiếp.
- Pin phiên bản DBMS, driver, benchmark framework; ghi rõ CPU, RAM, OS, Docker resource limits và storage.
- Giữ cùng quy mô dữ liệu, seed nếu có thể, cấu trúc logic và index tương đương, số client, duration, connection pooling, workload mix.
- Đặt **cùng isolation level** khi so hiệu năng tương đồng; tách riêng thí nghiệm hành vi mặc định.
- Warm-up trước mỗi lần đo; chạy **3–5 repetitions** trên từng cấu hình; báo cáo trung bình, độ lệch/biến thiên hoặc khoảng tin cậy nếu đủ số lần đo.
- Định nghĩa rõ **successful TPS** (chỉ transaction thành công) và cách tính latency có/không có retries.
- Reset/reload dữ liệu khi workload làm thay đổi đáng kể trạng thái DB; kiểm soát checkpoint, cache, background maintenance và I/O nếu chúng ảnh hưởng kết quả.
- Không khẳng định hai DBMS có thiết lập “tương đương tuyệt đối”; công bố cấu hình để người đọc đánh giá giới hạn so sánh.

## 5. Thiết kế sáu thí nghiệm

### Bảng tổng hợp

| ID | Thí nghiệm | Cơ chế / RQ | Độ ưu tiên |
|---|---|---|---|
| **E1** | Concurrent Read–Write | MVCC / RQ1 | Bắt buộc |
| **E2** | Hot Row Contention | Row locking / RQ2 | Bắt buộc |
| **E3** | Lost Update & OCC | Pessimistic vs optimistic / RQ3 | Bắt buộc |
| **E4** | Deadlock | Detection, prevention / RQ4 | Bắt buộc |
| **E5** | Isolation Level Comparison | Snapshot, abort, consistency / RQ1, RQ5 | Nên có |
| **E6** | Read/Write Workload Scaling | Throughput/latency / RQ2, RQ5 | Bắt buộc |

### E1 — Concurrent Read–Write: Chứng minh MVCC

**Câu hỏi:** Một transaction đang UPDATE nhưng chưa commit có chặn transaction khác thực hiện plain SELECT cùng row không?

Schema tối giản:

```sql
CREATE TABLE accounts (
    id INT PRIMARY KEY,
    balance INT NOT NULL
);
INSERT INTO accounts VALUES (1, 1000);
```

Lịch thực thi ở **READ COMMITTED**:

1. T1: `BEGIN; UPDATE accounts SET balance = 1500 WHERE id = 1;` (chưa COMMIT).
2. T2: `BEGIN; SELECT balance FROM accounts WHERE id = 1;` → kỳ vọng **1000**, không chờ UPDATE thông thường.
3. T1: `COMMIT;`
4. T2: `SELECT balance FROM accounts WHERE id = 1;` → kỳ vọng **1500** vì statement mới dùng snapshot mới ở READ COMMITTED.
5. T2: `COMMIT;`

**Đo:** SELECT latency trước/sau commit; blocking state; throughput đọc trong workload read/write. **Chứng minh:** readers không nhất thiết block writers và vice versa đối với plain SELECT, nhưng điều này **không** đồng nghĩa mọi hoạt động đều lock-free.

**Biến thể:** Lặp lại ở REPEATABLE READ để cho thấy snapshot nhất quán trong transaction (chú ý thời điểm tạo snapshot).

### E2 — Hot Row Contention: Tranh chấp cập nhật

**Câu hỏi:** Khi nhiều transaction UPDATE cùng row, TPS và latency thay đổi thế nào?

Ví dụ thao tác:

```sql
BEGIN;
UPDATE inventory
SET stock = stock - 1
WHERE product_id = 1 AND stock > 0;
COMMIT;
```

Cần tạo bảng `inventory` và nạp đủ stock trước benchmark; làm rõ giao dịch cập nhật 0 row do hết hàng có được tính là business success hay không.

Ba phân bố khóa truy cập:

| Scenario | Phân bố truy cập | Ý nghĩa |
|---|---|---|
| No contention | Mỗi client nhắm row khác nhau | Baseline |
| Moderate contention | Chia tải trên ~100 rows | Xung đột vừa |
| High contention | Phần lớn tải dồn một row | Hotspot |

**Concurrent client levels:** 1, 8, 16, 32, 64, 128 (điều chỉnh theo RAM/CPU); ví dụ mỗi điểm warm-up rồi đo 60 giây, lặp 3–5 lần.

**Đo:** TPS, p50/p95/p99 latency, lock wait duration, blocked transactions, timeout/abort, CPU usage.

**Kỳ vọng:** High contention có thể đạt điểm bão hòa sớm, p95 tăng mạnh; đây là **giả thuyết** cần thử, không tự điền số liệu.

### E3 — Lost Update & Optimistic Concurrency

**Câu hỏi:** Cùng một nghiệp vụ rút tiền, các chiến lược tránh lost update khác nhau thế nào về correctness và hiệu năng?

Giả sử `balance=1000`; T1 và T2 cùng rút 100.

**A. Read–Modify–Write không bảo vệ (để minh họa lỗi):**

```sql
SELECT balance FROM accounts WHERE id = 1;
-- Ứng dụng tính :new_balance = balance - 100
UPDATE accounts SET balance = :new_balance WHERE id = 1;
```

Nếu hai transaction đều đọc 1000 trước khi ghi, cả hai có thể ghi 900 thay vì 800 (lost update ở tầng ứng dụng). Phải điều phối rõ lịch thực thi để tái hiện; kết quả phụ thuộc isolation/DBMS.

**B. Pessimistic locking:**

```sql
BEGIN;
SELECT balance FROM accounts WHERE id = 1 FOR UPDATE;
-- Ứng dụng tính :new_balance
UPDATE accounts SET balance = :new_balance WHERE id = 1;
COMMIT;
```

**C. Optimistic version check (application-level OCC):**

Schema cần có `version`:

```sql
ALTER TABLE accounts ADD COLUMN version INT NOT NULL DEFAULT 1;
```

Sau khi đọc `balance`, `version`, ứng dụng dùng:

```sql
UPDATE accounts
SET balance = balance - 100,
    version = version + 1
WHERE id = 1 AND version = :expected_version;
```

Nếu **affected rows = 0**, phát hiện conflict; ứng dụng rollback/re-read/retry (với giới hạn retry/backoff), hoặc trả trạng thái conflict. Ngay cả version-check UPDATE vẫn có thể chờ khóa vật lý.

**D. Atomic SQL UPDATE baseline (nên có):**

```sql
UPDATE accounts
SET balance = balance - 100
WHERE id = 1 AND balance >= 100;
```

**Đo:** Correctness (balance cuối), successful TPS, latency end-to-end bao gồm retry, conflict/retry rate, lock wait. So sánh dưới low vs high contention.

> OCC không phải cơ chế mặc định áp cho mọi PostgreSQL transaction: ở đây đó là chiến lược ứng dụng dùng version column. Pessimistic locking và MVCC có thể cùng tồn tại.

### E4 — Deadlock Detection & Prevention

**Câu hỏi:** Deadlock hình thành thế nào, DBMS phản hồi gì, việc lấy lock theo thứ tự cố định có giúp không?

- T1 lock Account A rồi yêu cầu Account B.
- T2 lock Account B rồi yêu cầu Account A.
- Hai transaction chờ lẫn nhau → deadlock; DBMS phát hiện và abort một transaction, ứng dụng cần retry nếu muốn hoàn tất nghiệp vụ.

**Phương án cải thiện:** Với thao tác chuyển tiền giữa hai tài khoản, luôn lấy row locks theo thứ tự tăng dần `account_id`, bất kể hướng chuyển.

**Đo:** deadlock count, aborted transactions, retries, latency trước và sau quy tắc thứ tự lock; phân biệt **blocking bình thường** với **deadlock**.

### E5 — Isolation Level Comparison

**Câu hỏi:** Các isolation levels thay đổi tính nhất quán, abort/retry và hiệu năng ra sao?

Thử trên PostgreSQL:

- READ COMMITTED
- REPEATABLE READ
- SERIALIZABLE

Lặp lại thí nghiệm phù hợp trên MySQL, lưu ý rằng semantics của mức cô lập có thể khác nhau giữa hệ quản trị. Dùng lịch thực thi định trước để quan sát non-repeatable read, write conflict hoặc serialization failure ở nơi tương ứng; đo TPS, p95, abort rate khi chạy workload đồng thời.

**Không kết luận mức isolation “tốt nhất” chỉ dựa vào TPS**: cần so sánh cả tính đúng đắn tương ứng với nghiệp vụ.

### E6 — Read/Write Workload Scaling

Chạy workload có thể tái lập trên BenchBase; ưu tiên **TPC-C** hoặc **SmallBank**.

Nếu dùng **custom workload**, có thể thử:

| Workload | Read | Write | Mục tiêu |
|---|---:|---:|---|
| W1 | 90% | 10% | Read-heavy |
| W2 | 50% | 50% | Mixed |
| W3 | 10% | 90% | Write-heavy |

**Quan trọng:** Đây là workload tùy biến; **không** gọi là transaction mix chuẩn TPC-C. Nếu tuyên bố chạy TPC-C-like theo cấu hình chuẩn, giữ nguyên transaction proportions đúng theo chuẩn/cấu hình benchmark được công bố.

Tăng client từ 1 → 8 → 16 → 32 → 64 → 128, đo throughput, p50/p95/p99 và failure. Vẽ hai nhóm biểu đồ: (i) TPS theo clients, (ii) p95 theo clients; chia theo DBMS, isolation và loại workload phù hợp.

**Kết quả mẫu chỉ để minh họa hình dạng đồ thị, tuyệt đối không đưa vào báo cáo dưới dạng số thực nghiệm.**

## 6. Metrics: hiệu năng, contention và tính đúng đắn

### 6.1. Performance

| Metric | Cách hiểu |
|---|---|
| TPS / throughput | Transaction **thành công** mỗi giây; ghi rõ cách tính |
| Mean latency | Trung bình thời gian phản hồi |
| p50 latency | Trung vị |
| p95/p99 latency | Độ trễ đuôi, phản ánh transaction chậm |
| CPU utilization | Mức tải CPU |
| Memory utilization | Mức tải RAM |

### 6.2. Contention

| Metric | Cách hiểu |
|---|---|
| Lock wait time | Thời gian chờ khóa; cần mô tả phương pháp đo |
| Blocked transactions | Transaction/session bị chặn |
| Deadlock count | Số deadlock phát hiện |
| Abort rate | Tỷ lệ transaction phải rollback/thất bại |
| Retry rate | Tỷ lệ transaction thử lại hoặc số attempts/transaction |

### 6.3. Correctness

Đối với giao dịch ngân hàng:

- **Conservation:** Tổng số dư sau các giao dịch chuyển tiền thành công phải bằng tổng số dư trước (nếu không có nạp/rút ngoại hệ thống).
- **Atomicity:** Không có giao dịch “trừ tiền nhưng không cộng tiền” được xem là thành công.
- **Lost-update prevention:** Hai giao dịch rút hợp lệ không được ghi đè làm mất tác động của nhau.
- **Business invariants:** Số dư hoặc tồn kho không âm nếu nghiệp vụ yêu cầu.

> Một workload có TPS cao nhưng vi phạm correctness **không** phải giải pháp concurrency control tốt hơn.

## 7. PostgreSQL vs MySQL: so sánh có chiều sâu

Hai hệ đều hỗ trợ MVCC nhưng không đồng nhất về hành vi isolation và locking.

### 7.1. Baseline công bằng

- PostgreSQL mặc định **READ COMMITTED**, MySQL InnoDB mặc định **REPEATABLE READ** (xác nhận theo phiên bản dùng trong dự án).
- Khi so performance tương đương, **cấu hình cùng isolation level** và ghi rõ thông số.
- Dành bài test riêng cho **default isolation behavior** để nghiên cứu lựa chọn mặc định của sản phẩm.

### 7.2. Range query, gap/next-key lock

Thử tại REPEATABLE READ với bảng và index được định nghĩa rõ:

```sql
-- Transaction T1
BEGIN;
SELECT *
FROM orders
WHERE order_id BETWEEN 100 AND 200
FOR UPDATE;
```

T2 thực hiện:

```sql
INSERT INTO orders(order_id) VALUES (150);
```

**Điều kiện:** ID 150 chưa tồn tại, phải có index phù hợp và dữ liệu mẫu phù hợp để quan sát locking.

- MySQL InnoDB có thể dùng gap/next-key locks cho range locking reads ở một số tình huống, khiến INSERT vào gap phải chờ.
- PostgreSQL `FOR UPDATE` thông thường khóa các hàng hiện hữu, không tự động gap-lock toàn bộ khoảng khóa theo cùng cách; mức SERIALIZABLE lại có cơ chế xử lý khác.

**Quan sát:** T2 có bị chờ không, wait duration, locks thực tế, hành vi khi T1 commit/rollback. Không khái quát mọi câu truy vấn hoặc mọi isolation level từ một scenario.

### 7.3. Nguyên tắc diễn giải

Thay vì kết luận “PostgreSQL vượt trội”, báo cáo nên trả lời:

- Điều kiện nào PostgreSQL có TPS/latency tốt hơn?
- Điều kiện nào MySQL nhỉnh hơn, hoặc hai bên tương đương?
- Đó là do snapshot/isolation, row/range locking, index, query plan, contention hay cấu hình khác?
- Kết quả có ổn định giữa nhiều lần đo và nhiều mức client không?

**Nguồn chính thức để đối chiếu:**

- PostgreSQL Transaction Isolation: https://www.postgresql.org/docs/current/transaction-iso.html
- PostgreSQL Explicit Locking: https://www.postgresql.org/docs/current/explicit-locking.html
- MySQL InnoDB Locking: https://dev.mysql.com/doc/refman/8.4/en/innodb-locking.html
- MySQL InnoDB Transaction Isolation: https://dev.mysql.com/doc/refman/8.4/en/innodb-transaction-isolation-levels.html

## 8. Lộ trình triển khai

### Giai đoạn 1 — Environment & Dataset

1. Viết `docker-compose.yml` cho PostgreSQL và MySQL.
2. Cài hoặc build BenchBase theo tài liệu tại thời điểm triển khai.
3. Khởi tạo schema/dataset TPC-C quy mô thử nghiệm.
4. Chạy smoke test, xác minh benchmark trả số liệu.

**Đầu ra:** Docker Compose, configs, schema, số lượng bản ghi, kiểm tra kết nối.

### Giai đoạn 2 — Functional Demos

1. Viết script E1–E4 với điều phối transaction và barrier/event để tái hiện deterministic interleavings.
2. Ghi log mốc `BEGIN`, SQL, wait, `COMMIT/ROLLBACK`, error SQLSTATE nếu có.
3. Kiểm chứng dữ liệu cuối và lưu transaction timeline.

**Đầu ra:** Scripts, SQL logs, screenshots minh chứng, bảng expected vs observed.

### Giai đoạn 3 — Performance Evaluation

1. Định nghĩa các mức clients, duration, workload, seed và data scale.
2. Warm-up, chạy đo chính thức 3–5 lượt/cấu hình.
3. Thu metrics từ benchmark, DBMS và tài nguyên hệ thống.
4. Xuất CSV và tạo biểu đồ.

**Đầu ra:** Raw CSV, TPS graph, latency graph, contention/abort stats.

### Giai đoạn 4 — Cross-DBMS

1. Chạy lại các kịch bản có ý nghĩa trên MySQL với cùng quy mô và logic.
2. Phân tích câu truy vấn, locks, isolation, query plans nếu cần.
3. Lập bảng PostgreSQL–MySQL và lý giải khác biệt.

**Đầu ra:** Comparison tables, observations, technical explanation và trade-offs.

### Giai đoạn 5 — Báo cáo

1. Liên hệ bằng chứng thực nghiệm với phần MVCC/Lock/OCC nhóm đã viết.
2. Trả lời lần lượt các RQ; không bỏ qua kết quả ngược giả thuyết.
3. Nêu hạn chế benchmark (máy local, cache, cỡ dataset, isolation semantics, measurement noise).
4. Kết luận tình huống phù hợp của từng chiến lược/DBMS.

**Đầu ra:** Experimental results, discussion, threats to validity, conclusion.

## 9. Cấu trúc báo cáo đề xuất (nửa sau)

### Chương 3 — Experimental Design

- 3.1 Research Questions and Hypotheses
- 3.2 Benchmark Dataset and Workloads
- 3.3 Experimental Environment and DBMS Configurations
- 3.4 Experimental Scenarios and Variables
- 3.5 Evaluation Metrics and Measurement Protocol

### Chương 4 — Transaction Contention Demonstrations

- 4.1 MVCC and Concurrent Read–Write (E1)
- 4.2 Hot Row and Row-Level Locking (E2)
- 4.3 Lost Update, Pessimistic vs Optimistic (E3)
- 4.4 Deadlock Detection and Prevention (E4)
- 4.5 Isolation Level Cases (E5)

### Chương 5 — Performance Evaluation

- 5.1 Experimental Protocol
- 5.2 Throughput vs Concurrency
- 5.3 Latency p50/p95/p99 vs Concurrency
- 5.4 Lock Wait, Abort and Retry
- 5.5 Correctness and Workload Sensitivity

### Chương 6 — PostgreSQL vs MySQL InnoDB

- 6.1 Cross-DBMS Setup and Fairness
- 6.2 Results by Workload and Contention
- 6.3 Isolation/Locking Behavior Differences
- 6.4 Architectural Explanations and Trade-offs

### Chương 7 — Discussion and Conclusion

- 7.1 Answers to Research Questions
- 7.2 Practical Recommendations
- 7.3 Limitations / Threats to Validity
- 7.4 Future Work and Conclusion

## 10. Checklist bàn giao và điểm cần tránh

### Checklist

- [ ] Có tối thiểu 2 DBMS: PostgreSQL và MySQL InnoDB.
- [ ] Ghi rõ benchmark dataset, schema, data scale và workload.
- [ ] Có script tái hiện các tình huống tranh chấp, không chỉ mô tả lý thuyết.
- [ ] E1–E4 có expected/actual behavior và SQL logs.
- [ ] E5/E6 đo performance bằng nhiều concurrency levels.
- [ ] TPS, latency p95 và ít nhất một contention metric được đo thực tế.
- [ ] Có correctness checks (tổng số dư, không âm, không lost update).
- [ ] Có warm-up, repetitions, raw output CSV và biểu đồ.
- [ ] So sánh cùng isolation level khi cần, công bố khác biệt mặc định.
- [ ] Phân tích nguyên nhân từ MVCC, locks và workload thay vì chỉ đọc biểu đồ.
- [ ] Kết luận điều kiện áp dụng/trade-off, không khẳng định PostgreSQL nhanh hơn trong mọi trường hợp.

### Các lỗi phương pháp luận cần tránh

1. **Nhầm dataset với workload:** Dữ liệu lớn không tự tạo high contention.
2. **Không đo correctness:** TPS cao nhưng số dư sai là kết quả vô nghĩa cho nghiệp vụ tiền tệ.
3. **So DBMS khác isolation levels mà không nói rõ:** Không bảo đảm phép so sánh tương đương.
4. **Nhầm OCC với MVCC hoặc isolation:** Version-column OCC là logic ở tầng ứng dụng.
5. **Nhầm lock wait với deadlock:** Blocked transaction chưa hẳn hình thành chu trình chờ.
6. **Dùng số minh họa như số đo:** Biểu đồ kết quả chỉ được dựng từ output thực tế.
7. **Trộn benchmark chuẩn với tùy chỉnh:** Custom transaction mix cần ghi rõ là custom workload.
8. **Chỉ báo cáo một lần chạy:** Dễ bị chi phối bởi cache, CPU scheduling, background tasks.

## 11. Đề xuất chốt phạm vi

**Stack:** PostgreSQL + MySQL InnoDB + Docker Compose + BenchBase + Python scripts + Pandas/Matplotlib.

**Workload:** TPC-C-like với BenchBase cho benchmark; ngân hàng/tồn kho nhỏ cho demo E1–E4. Nếu bị giới hạn thời gian, chuyển benchmark chính sang SmallBank.

**Thí nghiệm tối thiểu:** E1, E2, E3, E4, E6. E5 và thí nghiệm range/gap lock dùng để tăng chiều sâu so sánh.

**Các chỉ số ưu tiên:** successful TPS, p95 latency, lock wait, abort/retry rate, correctness.

**Bước kế tiếp:** Viết bảng Experimental Design chi tiết cho từng E1–E6 gồm: objective, hypothesis, independent variables, control variables, dataset state, exact transaction schedule, logging SQL, repetitions, expected observations, resulting tables/charts, rồi mới bắt đầu lập trình.

---

*Tài liệu là kế hoạch thực nghiệm và đề xuất kỹ thuật; các kết quả/kỳ vọng chưa phải số liệu benchmark thực tế.*
