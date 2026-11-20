# Kết quả thực nghiệm và thảo luận

**Đề tài:** Handling High Concurrency and Transaction Contention in Database Systems.  
**Phạm vi:** PostgreSQL và MySQL InnoDB; demo E1–E4 và benchmark hỗn hợp E6. E5 đã bỏ để phù hợp bài trình bày 15 phút.  
**Nguồn số liệu:** log demo được chỉ rõ ở phụ lục; 42 lượt benchmark ngày 10/11/2026 và kết quả tổng hợp đã kiểm tra ở bước 18.

Phần này có thể dùng làm nội dung kết quả thực nghiệm của báo cáo. Các con số là số đo đã lưu; các giải thích về nguyên nhân được phân biệt với quan sát. Không thực hiện benchmark mới khi soạn tài liệu.

## 1. Thiết kế và phạm vi phép so sánh

### 1.1. Hai tầng bằng chứng

E1–E4 sử dụng lịch transaction có điều phối để quan sát một cơ chế cụ thể: đọc phiên bản đã commit, chờ khóa, kiểm tra version và deadlock. Các lượt tải nhỏ đi kèm E2/E3 chỉ là sơ bộ; không gộp TPS của chúng với benchmark chính thức.

E6 sử dụng BenchBase chạy 5 loại transaction nghiệp vụ cùng lúc theo tỷ lệ NewOrder/Payment/OrderStatus/Delivery/StockLevel = 45/43/4/4/4. Đây là phép đo tổng thể của DBMS trên một workload hỗn hợp; không phải ba chế độ chạy riêng MVCC, locking và OCC. MVCC và khóa có thể cùng phục vụ một transaction. Workload E6 không bổ sung cột version và quy trình OCC như E3.

### 1.2. Dataset và điểm nóng

Mỗi DBMS có dataset TPC-C-like 1 warehouse, 10 district, 30.000 customer và 100.000 dòng stock. Tổng ban đầu là 599.051 dòng trong 9 bảng. `tpcc` là nguồn/demo; E6 chỉ ghi vào `tpcc_bench`, được phục hồi từ baseline riêng của từng engine. Hai baseline có cùng quy mô và cấu trúc logic, không khẳng định mọi giá trị/timestamp giống hệt từng byte.

Warehouse là đơn vị kho hàng trong mô hình nghiệp vụ. Các khách hàng khác nhau vẫn có thể tạo Payment cập nhật cùng tổng tiền thanh toán của kho:

```sql
UPDATE warehouse
SET w_ytd = w_ytd + ?
WHERE w_id = 1;
```

Với một warehouse, các Payment chồng lấn chia sẻ dòng này. Đây là điểm nóng có căn cứ từ [procedure Payment của đúng commit BenchBase](https://raw.githubusercontent.com/cmu-db/benchbase/33c00473807ebd49304d114a6d769d2d2b2bbb34/src/main/java/com/oltpbenchmark/benchmarks/tpcc/procedures/Payment.java). Tuy nhiên, bộ đếm wait của E6 không ghi row gây ra từng wait; chưa định lượng được bao nhiêu phần trăm chờ khóa đến riêng từ warehouse. NewOrder còn khóa district/stock, nên không mô tả toàn bộ workload là mọi client luôn UPDATE một dòng duy nhất.

### 1.3. Môi trường và giao thức

- Máy Windows 11, Intel Core i7-1355U, 12 luồng logic, RAM vật lý khoảng 15,7 GiB; Docker WSL2 khoảng 7,6 GiB RAM.
- PostgreSQL **17.11** và MySQL **8.4.13**, image được ghim theo digest. Mỗi DBMS có quota **2 vCPU/2 GiB**, không swap thêm. Generator có **4 vCPU/1 GiB**, Java heap tối đa 512 MiB.
- `shared_buffers` PostgreSQL và `innodb_buffer_pool_size` MySQL đều ghi nhận 128 MiB; đây không phải toàn bộ bộ nhớ mỗi engine sử dụng.
- Cả hai connection workload dùng **READ COMMITTED**. MySQL global mặc định vẫn là REPEATABLE READ; XML/JDBC đặt isolation cho các connection benchmark, không đổi mặc định toàn server.
- Giữ cấu hình durability đã ghi nhận: PostgreSQL `fsync`, `synchronous_commit`, `full_page_writes` bật; MySQL `innodb_flush_log_at_trx_commit=1`, `sync_binlog=1`, binary log bật. Cùng quota không có nghĩa hai engine có mọi chi phí ghi tương đương.
- Client **1, 4, 8, 16, 32, 64, 128**; **3 lần lặp/cấu hình**; tổng **42 lượt**. Chỉ một DBMS chịu tải mỗi lần; đảo thứ tự engine ở lần lặp thứ hai.
- Mỗi lượt: **restore baseline → kiểm tra/ANALYZE → warm-up 15 giây → đo cấu hình 60 giây**. Warm-up và đo chạy liên tục; không reset ở giữa. Công thức dùng thời gian đo thực tế của BenchBase, có thể dài hơn cấu hình khoảng một giây.

Nguồn cấu hình: [protocol](benchbase/benchmark/protocol.json), [snapshot trước chạy](benchbase/benchmark/step17-environment-before.json), [hướng dẫn giao thức](benchbase/benchmark/README.md). BenchBase được ghim ở commit `33c00473807ebd49304d114a6d769d2d2b2bbb34`.

### 1.4. Định nghĩa phép đo

**Successful TPS** là số request có trạng thái cuối SUCCESS và nằm trọn trong cửa sổ đo, chia thời gian đo thực tế. Đây là throughput của cả mix, không phải chỉ tiêu tpmC được chứng nhận. Bản BenchBase đang dùng có bộ đếm SUCCESS gốc gồm cả warm-up; vì vậy số liệu dùng sidecar request trong đúng điều kiện ghi latency của BenchBase, không dùng goodput gốc.

**p95** tính trên request thành công từng lượt, gồm các lần thử lại bên trong `doWork`. Hình tổng hợp thể hiện trung bình của **ba p95 từng lượt**, không phải p95 gộp tất cả request. Thanh sai số là **min–max**, không phải khoảng tin cậy. Giữ cả ba lượt, không chọn lượt tốt nhất.

E6 cho tối đa **3 attempt tổng cộng**, gồm lần đầu; bản vá thêm retry PostgreSQL `40P01` để cùng xử lý deadlock như nhánh MySQL. Retry trong E6 gọi lại procedure cùng loại và có thể sinh lại tham số. Điều này khác demo E4 chạy lại yêu cầu A/B cố định, và khác retry version-check của E3. Instrumentation và các khác biệt timeout được công bố trong [README benchmark](benchbase/benchmark/README.md).

## 2. Kết quả kiểm chứng cơ chế E1–E4

### 2.1. E1 — Đọc phiên bản đã commit trong khi có UPDATE

Trên cả hai DBMS, T1 cập nhật `warehouse.w_ytd` từ **300.000 lên 300.100** và giữ transaction chưa commit. T2 chạy plain SELECT bằng connection khác, đọc **300.000** và hoàn tất trước khi T1 bắt đầu commit. Sau khi T1 commit, T2 đọc lại trong cùng transaction READ COMMITTED và nhận **300.100**. Giá trị nguồn được khôi phục về 300.000; lịch thực thi và khôi phục đều PASS.

Kết quả cho thấy lần đọc đầu không đọc dữ liệu chưa commit và không phải đợi khóa cập nhật dòng của T1. Việc lần SELECT sau thấy giá trị mới phù hợp với snapshot theo từng câu đọc ở READ COMMITTED. Cơ chế này được mô tả trong [PostgreSQL Read Committed](https://www.postgresql.org/docs/17/transaction-iso.html#XACT-READ-COMMITTED) và [MySQL consistent nonlocking reads](https://dev.mysql.com/doc/refman/8.4/en/innodb-consistent-read.html). Kết luận giới hạn ở plain SELECT của lịch này; không suy rộng sang locking read hoặc DDL, và không lấy vài ms của demo để xếp hạng tốc độ hai hệ.

### 2.2. E2 — Ghi cùng dòng tạo chờ khóa

T1 và T2 cùng tăng `stock.s_ytd` của `(warehouse=1, item=1)`. Khi T1 chưa commit, metadata ghi nhận T2 chờ T1: PostgreSQL có quan hệ PID chặn/chờ và `wait_event_type=Lock`; MySQL có record lock ở PRIMARY với `lock_data=1, 1`. Sau khi T1 commit, T2 hoàn tất; counter tăng từ **0 lên 2**, rồi được khôi phục về 0.

Trong lượt sơ bộ 16 client, `distinct` ghi nhận tối đa **0** session chờ khóa mục tiêu, `shared4` tối đa **12**, `hot1` tối đa **15** trên cả hai hệ. Đây là max từ mẫu quan sát của lượt đó, không phải tổng số lần chờ. Các lượt sơ bộ không warm-up, chỉ một lần/cấu hình nên dùng để minh họa phân bố truy cập, không làm số liệu hiệu năng chính thức.

Kết quả E2 thể hiện giới hạn của đọc đa phiên bản đối với ghi cạnh tranh: các cập nhật xung đột vẫn cần phối hợp bằng khóa. Row locks và record locks được mô tả trong [PostgreSQL row-level locks](https://www.postgresql.org/docs/17/explicit-locking.html#LOCKING-ROWS) và [InnoDB locking](https://dev.mysql.com/doc/refman/8.4/en/innodb-locking.html).

### 2.3. E3 — Lost update, kiểm tra version và retry

Demo dùng bảng hỗ trợ sao chép counter từ stock và thêm version; không ALTER bảng stock nguồn. Với counter ban đầu 0, cả hai DBMS cho cùng câu chuyện:

1. **Lost update:** hai transaction cùng đọc 0, mỗi bên ghi giá trị đã tính là 1. Có hai COMMIT nhưng counter cuối chỉ **1**, thiếu một lần tăng. PASS ở giai đoạn này nghĩa là tái hiện đúng lỗi mong đợi.
2. **Version check:** hai bên đọc version 1. T1 ghi thành công và tăng version; UPDATE của T2 dùng version cũ không khớp dòng. Kết quả **counter=1, version=2**, một conflict được phát hiện.
3. **Retry:** T2 rollback, đọc lại và tính lại trong transaction mới. Kết quả **counter=2, version=3**, hai logical operation hoàn tất.

Điều kiện kiểm tra của ứng dụng có dạng:

```sql
UPDATE bang_ho_tro_occ
SET counter_value = :gia_tri_tinh_lai,
    version = version + 1
WHERE s_w_id = 1 AND s_i_id = :item
  AND version = :version_da_doc;
```

Version-check phát hiện kết quả tính từ dữ liệu đã cũ. Nó không tắt MVCC hoặc khóa vật lý của UPDATE. Nhánh MySQL lost-update có thể trả 0 affected rows khi ghi lại cùng giá trị; không nhầm trường hợp đó với conflict được phát hiện bằng điều kiện version.

Các lượt OCC sơ bộ còn có **423 logical operation hết retry** trong tổng các cấu hình hot-row nhiều client, với ngân sách **8 retry sau attempt đầu**. Counter/version vẫn khớp số operation thực sự thành công, không tính yêu cầu hết retry vào kết quả. Vì vậy, đảm bảo counter đúng không đồng nghĩa mọi yêu cầu đều được phục vụ thành công; conflict/retry có chi phí và ngân sách hữu hạn. Các lượt này chỉ minh họa, không so TPS trực tiếp với E6.

### 2.4. E4 — Deadlock khác với chờ khóa thông thường

Với A/B là hai stock row, lịch `opposite` cho T1 lấy A rồi B, T2 lấy B rồi A. Trong lượt kiểm chứng đầy đủ, mỗi DBMS có **5/5 cặp** xuất hiện đúng một nạn nhân deadlock: PostgreSQL trả `40P01`; MySQL trả `1213` với SQLSTATE `40001`. Mỗi cặp có một survivor commit, counter A/B đều tăng **1**; retry toàn transaction nạn nhân sau đó làm cả hai tăng **2**. Khôi phục dữ liệu đều đạt.

Với lịch `ordered`, cả hai lấy A rồi B. Mỗi DBMS có **5/5 cặp** đều hoàn tất hai COMMIT, **0 deadlock**, **0 retry**. Điều này kiểm chứng tác dụng của cùng thứ tự lấy khóa trong mô hình hai row đã thiết kế; không bảo đảm mọi workload phức tạp đều hết deadlock.

Chờ khóa đơn thuần là một cạnh chờ, còn deadlock cần một chu trình. DBMS có thể chọn nạn nhân và ứng dụng phải xử lý transaction bị hủy; không giả định T1 luôn là nạn nhân. Cơ sở giải thích: [PostgreSQL deadlocks](https://www.postgresql.org/docs/17/explicit-locking.html#LOCKING-DEADLOCKS) và [InnoDB deadlocks](https://dev.mysql.com/doc/refman/8.4/en/innodb-deadlocks.html). Không dùng thời gian detector của demo, trong đó PostgreSQL ghi nhận `deadlock_timeout=1s`, để xếp hạng tốc độ xử lý tải.

## 3. E6 — Throughput và độ trễ khi tăng concurrency

### 3.1. Throughput tăng đến một vùng rồi không tăng tiếp

![Hình 1. Successful TPS theo số client](</C:/DBMS/Project Report/benchbase/benchmark/analysis/01_successful_tps.png>)

**Hình 1 — Successful TPS của workload hỗn hợp theo số client.** Đường/chấm lớn là trung bình 3 lượt, thanh dọc là min–max và chấm nhỏ là từng lượt. Trục client là log₂. Chỉ đếm SUCCESS trong cửa sổ đo; giữ mọi lần lặp. Bản [SVG](benchbase/benchmark/analysis/01_successful_tps.svg) dùng khi cần hình vector.

PostgreSQL tăng từ **430,19 TPS ở 1 client** lên mức trung bình cao nhất trong các mức đã thử là **660,95 TPS ở 4 client**, với min–max **641,77–697,81**. Sau đó trung bình giảm xuống **621,37** ở 8 client và **235,23** ở 128 client. Trong cấu hình này, thêm client sau vùng đỉnh quan sát không tạo thêm throughput tương ứng.

MySQL tăng từ **137,20 TPS ở 1 client** lên **236,60** ở 4 client và **238,95** ở 8 client. Mức 8 có min–max **230,57–250,64**; các mức cao hơn không tạo xu hướng tăng rõ. Không gọi 8 client là điểm tối ưu đã được chứng minh vì các khoảng của nhiều mức tải có chồng lấn.

Các giá trị trung bình PostgreSQL cao hơn MySQL ở cả bảy mức đã thử. Tuy nhiên, ở 128 client, PostgreSQL **214,23–251,98 TPS** và MySQL **193,00–236,23 TPS** có chồng lấn. Ba lần lặp không đủ để biến thứ hạng mean này thành tuyên bố ý nghĩa thống kê hoặc kết luận cho mọi workload.

### 3.2. Đuôi latency tăng mạnh khi thêm client

![Hình 2. p95 request thành công theo số client](</C:/DBMS/Project Report/benchbase/benchmark/analysis/02_p95_latency.png>)

**Hình 2 — Trung bình p95 từng lượt của request thành công.** Thời gian gồm retry bên trong request. Cả trục client và trục p95 dùng thang log đã ghi rõ; khoảng cách tung thể hiện tỷ lệ, không phải chênh lệch ms bằng nhau. Thanh dọc là min–max của ba p95, không phải khoảng tin cậy hoặc p95 gộp. Bản [SVG](benchbase/benchmark/analysis/02_p95_latency.svg) dùng khi xuất báo cáo.

Trung bình p95 PostgreSQL tăng từ **5,54 ms ở 1 client** lên **329,51 ms ở 32 client**, **955,85 ms ở 64** và **2.545,62 ms ở 128**. MySQL lần lượt có **17,81 ms**, **392,48 ms**, **819,02 ms** và **1.526,48 ms** tại các mức tương ứng.

PostgreSQL có p95 trung bình thấp hơn ở các mức 1–32; MySQL có p95 trung bình thấp hơn ở 64 và 128. Do đó, thứ hạng TPS và thứ hạng độ trễ đuôi không trùng nhau ở vùng tải cao. Một kết luận chỉ nhìn TPS sẽ bỏ sót phần đánh đổi này.

Ở 128 client, ba p95 PostgreSQL nằm trong **2.401,06–2.727,11 ms**, còn MySQL trong **1.403,00–1.712,69 ms**. Đây là khác biệt quan sát trong ba lượt dưới cấu hình đã công bố, không phải bằng chứng một engine có latency tốt hơn trong mọi hoàn cảnh.

### 3.3. Không loại bỏ điểm dao động ở 16 client

TPS PostgreSQL tại 16 client có mean **446,19**, min–max **314,62–542,17**; MySQL có mean **197,08**, min–max **140,74–234,10**. Lần lặp thứ ba chậm hơn trên cả hai hệ. Số liệu này được giữ nguyên trên hình và trong CSV.

Không có telemetry đủ để kết luận nguyên nhân là nhiệt CPU, paging, I/O hoặc một ứng dụng nền cụ thể. Dao động này làm giảm độ chắc chắn của việc xếp hạng một điểm tải; nó là lý do phải trình bày từng lượt và độ phân tán thay vì chỉ một đường mean.

## 4. Diễn giải cùng bằng chứng hỗ trợ

### 4.1. Có điểm nóng và có nhiều loại giới hạn đồng thời

[Hình hỗ trợ lock wait](benchbase/benchmark/analysis/03_lock_wait.png) cho thấy tỷ lệ session chờ khóa trung bình trong mẫu MySQL tăng đến **97,37% ở 128 client**. PostgreSQL có khoảng **70,65% ở 32 client** và **47,12% ở 128**. Đây là tỷ lệ từ mẫu tức thời, không phải tỷ lệ transaction từng bị block hoặc tổng thời gian chờ khóa.

Chỉ số Lock giảm không đủ để kết luận hệ ít tranh chấp hơn: session còn có thể chờ CPU, I/O hoặc tài nguyên khác. [Hình CPU/RAM](benchbase/benchmark/analysis/05_cpu_memory.png) ghi nhận PostgreSQL sử dụng xấp xỉ quota CPU từ 8 client; MySQL tại 128 client khoảng **79,91% quota**. Các số này phù hợp với khả năng hai engine bị giới hạn theo cách khác nhau, nhưng chưa tách được tác động nhân quả của CPU, khóa, ghi log và thực thi truy vấn.

E2 chứng minh trực tiếp một UPDATE cùng row có thể chờ transaction giữ khóa. Mã Payment chỉ ra điểm nóng warehouse của E6. Ghép hai bằng chứng giúp giải thích vì sao có thể bão hòa khi tăng concurrency, nhưng chưa đủ để quy toàn bộ đường TPS cho riêng row warehouse hoặc riêng MVCC.

### 4.2. Abort và retry cần đọc theo đúng mẫu số

Trong **848.494 request đo** có **839.700 SUCCESS**, **3.849 NewOrder rollback cố ý** và **4.945 Delivery bị hủy do tranh chấp**. Có **16 request retry rồi thành công**: các mã lỗi cuối ghi trong request gồm **10 `40P01/0`** và **6 `40001/1213`**. Không có request kết thúc ở nhóm lỗi SQL/hết retry trong tập đo này.

NewOrder rollback chiếm khoảng **1,01% của 382.434 yêu cầu NewOrder**. Delivery bị hủy chiếm khoảng **14,55% của 33.981 yêu cầu Delivery**. Đây là tỷ lệ gộp toàn bộ lượt để mô tả tổng; hình hỗ trợ dùng trung bình tỷ lệ từng lượt theo từng cấu hình. Không so hai tỷ lệ mà bỏ qua mẫu số.

Trong [mã Delivery đúng commit](https://raw.githubusercontent.com/cmu-db/benchbase/33c00473807ebd49304d114a6d769d2d2b2bbb34/src/main/java/com/oltpbenchmark/benchmarks/tpcc/procedures/Delivery.java), procedure đọc đơn chờ giao rồi DELETE theo khóa. Nếu Delivery khác đã nhận đơn và DELETE không còn tác động đúng một row, procedure ném `UserAbortException`; Worker rollback. Đây là hủy do tranh chấp ở logic benchmark dưới READ COMMITTED, khác với NewOrder chọn item không hợp lệ theo thiết kế. Nhánh USER_ABORTED không được tính vào 16 retry do lỗi SQL.

Khóa cũ `expected_user_aborts` trong output từng lượt chứa cả hai nhóm; tên khóa không phản ánh đầy đủ ý nghĩa. Bước 18 phân loại lại từ CSV theo loại transaction. `sql_or_retry_failed_requests=0` không có nghĩa không có abort do tranh chấp. Successful TPS loại cả hai nhóm USER_ABORTED nên vẫn dùng đúng định nghĩa.

### 4.3. Tính đúng đắn được kiểm tra đến đâu?

E1/E2/E4 đối chiếu giá trị cuối và khôi phục; E3 đối chiếu counter/version theo số operation thành công và xóa fixture. Với E6, cả 42 lượt đạt các kiểm tra: tổng thanh toán warehouse bằng tổng district; số đơn kế tiếp khớp max order ID; số order line khớp tổng số dòng của order; stock quantity nằm trong khoảng đã kiểm tra.

Các kiểm tra đó là bằng chứng hữu ích về những invariant đã chọn. Chúng không kiểm định mọi quan hệ nghiệp vụ, không chứng minh lịch thực thi serializable, và không thay thế bộ kiểm định đầy đủ TPC-C. Vì vậy, không viết rằng toàn bộ workload đã được chứng minh không thể lost update hay có mọi bảo đảm của SERIALIZABLE.

## 5. Trả lời câu hỏi nghiên cứu và kết luận có giới hạn

- **RQ1 — MVCC hỗ trợ read/write thế nào?** E1 cho thấy plain SELECT đọc bản đã commit trong lúc transaction khác giữ UPDATE. Kết quả đọc lại ở READ COMMITTED phụ thuộc thời điểm câu đọc, không cố định theo một snapshot duy nhất cho cả transaction.
- **RQ2 — Hot row ảnh hưởng thế nào?** E2 tái hiện chờ khóa cùng row. E6 cho thấy thêm client có thể làm latency tăng mà throughput không tăng, hoặc giảm; cần đọc cùng mẫu wait và tài nguyên để giải thích phạm vi quan sát.
- **RQ3 — OCC phát hiện lost update ra sao?** E3 dùng version để từ chối lần ghi dựa trên dữ liệu cũ; đọc lại/tính lại khi retry khôi phục kết quả mong đợi. OCC vẫn có thể hết retry ở tải tranh chấp; E6 chưa đo một workload OCC version-column riêng.
- **RQ4 — Deadlock được xử lý/phòng tránh ra sao?** E4 quan sát nạn nhân, rollback và retry toàn transaction; lấy khóa cùng thứ tự loại bỏ vòng chờ trong lịch A/B đã thử. Không đồng nhất lock timeout với deadlock.
- **RQ5 — PostgreSQL và MySQL khác nhau thế nào?** Trong workload 1 warehouse/quota này, PostgreSQL có TPS mean cao hơn trên các mức đã đo và p95 mean thấp hơn ở 1–32 client; MySQL có p95 mean thấp hơn ở 64–128. Phải cân nhắc cả throughput, tail latency, abort và độ phân tán; chưa có căn cứ tuyên bố một hệ vượt trội trong mọi trường hợp.

Kết luận thực nghiệm là khả năng phục vụ đồng thời phụ thuộc cả lịch truy cập, kiểm soát xung đột của ứng dụng và giới hạn tài nguyên. PostgreSQL đáp ứng tốt mục tiêu throughput ở các mức tải vừa đã đo, nhưng tăng số connection vượt vùng đỉnh không bảo đảm thêm năng lực phục vụ. Khi chọn DBMS cho triển khai khác, cần đo lại workload, dataset và mục tiêu độ trễ tương ứng; không dùng riêng kết quả local này như một xếp hạng chung.

## 6. Giới hạn cần công bố

1. Một máy laptop, Docker WSL2 và workload nền dùng chung CPU/storage; không đo đủ telemetry để giải thích mọi dao động, nhất là mức 16 client.
2. Một warehouse tạo contention tập trung. Tăng warehouse đồng thời thay đổi dataset và phân bố truy cập, chưa có phép đo đó để suy rộng.
3. Chỉ một mix và READ COMMITTED, không có ma trận isolation hoặc phân bố hot-row riêng trong E6. E5 đã bỏ; E3 OCC là demo ứng dụng riêng.
4. Chỉ ba lượt/cấu hình; min–max không phải khoảng tin cậy. Không suy ý nghĩa thống kê chỉ từ hai đường mean hoặc từ khoảng có/không chồng lấn.
5. Request vắt qua ranh giới warm-up/đo hoặc kết thúc sau cửa sổ bị loại theo guard BenchBase. Ở latency cao có hiệu ứng biên; p95 còn có điều kiện request thành công.
6. SQL/resource sample có nhãn pha theo log nhận về, chỉ gần đúng. Lock sample không đo đủ mọi loại wait; Docker stats memory không phải toàn bộ RAM Windows.
7. Cùng quota và cache cấu hình chưa tạo tương đương tuyệt đối: durability, binary log, cache OS, driver, SQL/engine internals và phạm vi statement timeout có khác biệt được công bố.
8. Có overhead của instrumentation. [TPCCWorker của commit đã dùng](https://raw.githubusercontent.com/cmu-db/benchbase/33c00473807ebd49304d114a6d769d2d2b2bbb34/src/main/java/com/oltpbenchmark/benchmarks/tpcc/TPCCWorker.java) có `Random` riêng không seed, nên cùng `randomSeed` không bảo đảm chuỗi tham số từng request giống hệt. Retry benchmark cũng không bảo đảm giữ nguyên mọi tham số nghiệp vụ.
9. Đây là TPC-C-like đã instrument, không phải kết quả TPC-C/tpmC chứng nhận; các invariant mới là tập con kiểm tra dữ liệu.

## 7. Ghi chú trình bày trong 15 phút

Khung thời lượng dưới đây dành cho toàn bài; có thể ghép với phần lý thuyết nhóm đã chuẩn bị. Sử dụng log/timeline đã lưu để trình bày cơ chế, không chạy toàn bộ các ma trận demo trong thời lượng này.

- **00:00–01:30 — Vấn đề và mục tiêu.** Giới thiệu đọc/ghi cạnh tranh, lost update, deadlock; mục tiêu là hành vi đúng và khả năng phục vụ, không mặc định chứng minh PostgreSQL luôn thắng.
- **01:30–04:00 — Cơ chế.** Nêu vai trò MVCC, khóa và OCC ứng dụng. Chốt rằng chúng có thể cùng tồn tại, không phải ba lựa chọn tắt/bật độc lập trong E6.
- **04:00–07:30 — Bằng chứng E1–E4.** E1 đọc 300.000 trước commit rồi 300.100 sau commit; E2 có cạnh chờ và counter cuối 2; E3 chuỗi 1/version1 → 1/version2 → 2/version3; E4 5/5 deadlock với thứ tự ngược và 0/5 khi cùng thứ tự trên mỗi hệ. Dành trọng tâm E3 cho lost update → version → retry, không mở thêm nhánh so sánh khác.
- **07:30–11:30 — Hai hình chính.** Hình 1: chỉ vùng đỉnh TPS rồi xu hướng khi thêm client. Hình 2: chỉ p95 tăng và thứ hạng đảo ở 64/128. Nói rõ min–max của ba lượt và thang log; nhắc điểm 16 dao động.
- **11:30–13:30 — Giải thích có căn cứ.** Dùng một điểm hotspot từ Payment và việc tách Delivery abort để nối kết quả với contention. Các hình lock/CPU ở phần dự phòng, chỉ mở khi cần; không tuần tự trình bày cả ba hình hỗ trợ nếu thiếu thời gian.
- **13:30–15:00 — Giới hạn và kết luận.** Nhắc 1 warehouse, 2 vCPU/2 GiB, READ COMMITTED, 3 lượt. Kết luận bằng đánh đổi throughput/tail latency và vai trò logic ứng dụng; không suy rộng thành hệ nào tốt nhất cho mọi tải.

### Các câu trả lời ngắn khi được hỏi

- **Vì sao có bản sao `tpcc_bench`?** Để workload thay đổi dữ liệu mà mỗi lượt vẫn bắt đầu từ baseline và không phá dữ liệu demo.
- **Vì sao một warehouse đã có tranh chấp?** Payment của nhiều khách hàng vẫn cập nhật dòng tổng của cùng kho; NewOrder còn chia sẻ district/stock. Đây không phải mọi request bị ép vào một row.
- **Vì sao không coi `failed_requests=0` là không có lỗi do tranh chấp?** Vì Delivery USER_ABORTED là nhóm khác và có 4.945 lần; số SUCCESS và abort được báo riêng.
- **Có OCC trong benchmark không?** Không bổ sung OCC cột version như E3 vào E6. Một số procedure có nhánh kiểm tra kết quả/rollback riêng; không gọi đó là một phép benchmark OCC version-column.
- **Vì sao không kết luận PostgreSQL luôn nhanh hơn?** TPS và p95 cho các mặt khác nhau, có độ phân tán và chỉ đo một cấu hình local cụ thể.

## Phụ lục — Bằng chứng và cách đối chiếu

### A. Lượt cơ chế được trích dẫn

- E1: [summary 20261024T034759_313380Z](experiments/e1_logs/20261024T034759_313380Z/summary.json), hai kết quả PASS và phục hồi dữ liệu. Lịch chi tiết nằm cùng thư mục.
- E2: [summary 20261109T152250_363110Z](experiments/e2_logs/20261109T152250_363110Z/summary.json), hai demo và 24 lượt sơ bộ 10 giây, mỗi cấu hình một lần.
- E3: [summary 20261109T161429_704849Z](experiments/e3_logs/20261109T161429_704849Z/summary.json), sáu pha demo và 16 lượt sơ bộ 10 giây; hai file cleanup xác nhận xóa fixture, giữ nguồn và danh sách bảng ban đầu.
- E4: [summary 20261109T163251_991835Z](experiments/e4_logs/20261109T163251_991835Z/summary.json), 20 cặp kiểm chứng, 5 cặp/mode/DBMS; xem thêm `rounds.csv` cùng thư mục.

Đây là các lượt đủ cấu hình và có kiểm tra lịch/dữ liệu, không phải tuyên bố mọi lần chạy trong dự án đều PASS. Các log khác vẫn được giữ: E1 `20261024T034322_874237Z` lỗi kết nối; E2 `20261109T152806_672533Z` báo RuntimeError; E3 `20261109T161910_029797Z` báo cột `e3_deliberate_missing_column` không tồn tại; E4 `20261109T163503_360343Z` có hai lock timeout `55P03`, không đạt tiêu chí một deadlock victim/một survivor. Không dùng các lượt này làm số liệu TPS chính thức hay gọi timeout là deadlock.

### B. Benchmark chính thức và đầu ra bước 18

- [formal-report.json](benchbase/benchmark/formal-report.json): đủ 42 lượt; mỗi mục có đường dẫn nguồn.
- [step17-verification.json](benchbase/benchmark/step17-verification.json): kiểm tra số request, outcome, invariant và trạng thái dọn dẹp.
- [trials.csv](benchbase/benchmark/analysis/trials.csv): 42 dòng, giữ từng repetition và run directory.
- [summary.csv](benchbase/benchmark/analysis/summary.csv): 14 cấu hình; mean/min/max/stdev là thống kê từng lượt, các count có hậu tố `_sum`.
- [analysis-validation.json](benchbase/benchmark/analysis/analysis-validation.json): SHA-256 đầu vào/đầu ra, tổng số đếm, kiểm tra tái tạo offline và định nghĩa thống kê.
- [README phân tích](benchbase/benchmark/analysis/README.md): cách chạy lại và đọc các cột/hình. Hai hình chính được đặt ở mục 3; [abort/retry](benchbase/benchmark/analysis/04_abort_retry.png), lock và CPU/RAM là hình hỗ trợ.

Có thể tạo lại bảng/hình bằng lệnh dưới đây tại thư mục gốc dự án, không cần Docker:

```powershell
.\.venv\Scripts\python.exe -B .\benchbase\benchmark\analyze_results.py
```

Việc soạn phần kết quả và kiểm tra liên kết/số liệu không sửa CSV request, kết quả từng lượt hay cấu hình thí nghiệm.
