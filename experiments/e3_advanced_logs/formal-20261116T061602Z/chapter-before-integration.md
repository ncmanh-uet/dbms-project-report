# 3. Thực nghiệm và so sánh với hệ quản trị cơ sở dữ liệu truyền thống

Chương này đánh giá khả năng xử lý giao dịch đồng thời và tranh chấp dữ liệu qua hai lớp thực nghiệm. Lớp thứ nhất sử dụng các lịch giao dịch được điều phối trên một số ít bản ghi để quan sát tính đúng đắn của cơ chế đọc đa phiên bản (MVCC), khóa và kiểm soát đồng thời lạc quan ở tầng ứng dụng (OCC). Lớp thứ hai sử dụng workload hỗn hợp trên BenchBase để đo thông lượng, độ trễ và các biểu hiện tranh chấp khi tăng số client đồng thời. PostgreSQL là hệ quản trị được khảo sát chính; MySQL với storage engine InnoDB được sử dụng làm hệ đối chứng.

Các kết quả được tổng hợp từ tài liệu, cấu hình và log đã lưu trong kho mã nguồn, trong đó benchmark chính thức gồm 42 lượt chạy ngày 10/11/2026. Các thực nghiệm kiểm chứng cơ chế được ký hiệu E1–E4; benchmark hỗn hợp được ký hiệu E6 theo hệ thống đặt tên của dự án. E5 không nằm trong phạm vi kết quả được báo cáo.

**Phạm vi của phép so sánh:** MVCC, khóa và OCC không phải ba chế độ độc lập được bật hoặc tắt trong benchmark hiện có. MVCC và khóa cùng tham gia xử lý giao dịch của DBMS; OCC trong E3 là cơ chế kiểm tra cột phiên bản do ứng dụng bổ sung. Vì vậy, chương này đối chiếu hành vi của ba cơ chế qua các kịch bản nhỏ, sau đó đánh giá hiệu năng của toàn hệ thống bằng E6. Dữ liệu hiện có chưa cho phép xếp hạng thông lượng của ba cơ chế trên cùng một workload chuẩn hóa.

## 3.1. Thực nghiệm trên PostgreSQL

### 3.1.1. Môi trường và thiết lập chạy thực nghiệm

Thực nghiệm được triển khai trên máy Windows 11, sử dụng Docker trên WSL2 để vận hành cơ sở dữ liệu và bộ sinh tải. Môi trường benchmark được ghi nhận trước và sau khi chạy; image được ghim theo digest nhằm hạn chế thay đổi phiên bản giữa các lượt. Bảng 3.1 trình bày cấu hình chính của đợt benchmark chính thức. Các demo E1–E4 được thực hiện riêng và không được xem là đã có cùng toàn bộ điều kiện kiểm soát tài nguyên của E6.

**Bảng 3.1. Môi trường benchmark chính thức.**

| Thành phần | Cấu hình |
|---|---|
| Máy chủ vật lý | Intel Core i7-1355U, 12 luồng logic; RAM khoảng 15,7 GiB |
| Nền tảng | Windows 11; Docker WSL2 có khoảng 7,6 GiB RAM |
| Hệ quản trị chính | PostgreSQL 17.11 |
| Hệ quản trị đối chứng | MySQL 8.4.13, InnoDB |
| Hạn mức mỗi DBMS | 2 vCPU, 2 GiB RAM, không cấp thêm swap |
| Bộ sinh tải | 4 vCPU, 1 GiB RAM; Java heap tối đa 512 MiB |
| BenchBase | Commit `33c00473807ebd49304d114a6d769d2d2b2bbb34`, có bổ sung instrumentation |
| Isolation của kết nối workload | READ COMMITTED trên cả hai hệ |
| Cache cấu hình được ghi nhận | PostgreSQL `shared_buffers = 128 MiB`; MySQL `innodb_buffer_pool_size = 128 MiB` |
| Giới hạn kết nối | `max_connections = 200` |
| Durability PostgreSQL | Bật `fsync`, `synchronous_commit`, `full_page_writes` |
| Durability MySQL | `innodb_flush_log_at_trx_commit = 1`, `sync_binlog = 1`; bật binary log |

Nguồn: [giao thức benchmark](benchbase/benchmark/protocol.json), [hồ sơ môi trường trước khi chạy](benchbase/benchmark/step17-environment-before.json) và [hướng dẫn benchmark](benchbase/benchmark/README.md).

Dữ liệu sử dụng mô hình TPC-C-like với một warehouse và chín bảng. Quy mô ban đầu của mỗi DBMS là 599.051 bản ghi, được kiểm tra trong [biên bản xác minh dataset](benchbase/logs/dataset-verification.json).

**Bảng 3.2. Quy mô dữ liệu ban đầu trên mỗi DBMS.**

| Bảng | Số bản ghi | Vai trò trong mô hình |
|---|---:|---|
| `warehouse` | 1 | Kho hàng |
| `district` | 10 | Đơn vị trực thuộc kho |
| `customer` | 30.000 | Khách hàng |
| `history` | 30.000 | Lịch sử thanh toán |
| `item` | 100.000 | Danh mục mặt hàng |
| `stock` | 100.000 | Tồn kho theo mặt hàng |
| `oorder` | 30.000 | Đơn hàng |
| `new_order` | 9.000 | Đơn hàng chờ xử lý |
| `order_line` | 300.040 | Chi tiết đơn hàng |
| **Tổng cộng** | **599.051** | |

Cơ sở dữ liệu `tpcc` được giữ làm nguồn và phục vụ demo. Benchmark ghi vào cơ sở dữ liệu riêng `tpcc_bench`, được phục hồi từ baseline trước từng lượt. Hai baseline có cùng cấu trúc logic và quy mô; không giả định mọi giá trị ngẫu nhiên hoặc timestamp giống nhau tuyệt đối. Sau phục hồi, hệ thống kiểm tra số dòng, các tổng số học và cập nhật thống kê bằng `ANALYZE`.

Cách tổ chức này tách dữ liệu nguồn khỏi quá trình sinh tải, đồng thời giảm ảnh hưởng của lượng dữ liệu tích lũy từ lượt trước lên lượt sau. Tuy nhiên, đây không phải thử nghiệm cold cache: cache hệ điều hành không bị ép xóa, và warm-up tiếp tục làm thay đổi dữ liệu trước cửa sổ đo.

### 3.1.2. Đánh giá tính đúng đắn của cơ chế với dữ liệu nhỏ

Mục tiêu của nhóm thử nghiệm này là làm rõ trạng thái dữ liệu và quan hệ giữa các giao dịch. “Dữ liệu nhỏ” ở đây chỉ tập bản ghi trực tiếp tham gia kịch bản, chẳng hạn một dòng warehouse hoặc một đến hai dòng stock, dù cơ sở dữ liệu nền vẫn chứa toàn bộ dataset. Các connection độc lập được điều phối để tái hiện lịch thực thi mong muốn; kết quả được đối chiếu với trạng thái trước và sau COMMIT/ROLLBACK.

#### 3.1.2.1. Kịch bản kiểm thử với dữ liệu nhỏ

**Bảng 3.3. Các kịch bản kiểm chứng cơ chế.**

| Mã | Mục tiêu | Lịch giao dịch chính | Tiêu chí quan sát |
|---|---|---|---|
| E1 | Kiểm chứng đọc đa phiên bản | T1 UPDATE warehouse và giữ chưa commit; T2 SELECT trước và sau khi T1 commit | Không đọc dữ liệu chưa commit; lần đọc đầu hoàn tất trước khi T1 bắt đầu commit |
| E2 | Kiểm chứng chờ khóa khi ghi cùng dòng | T1 và T2 cùng tăng counter của một dòng stock; T1 giữ khóa trước | Có quan hệ chặn/chờ; sau hai commit, counter tăng đúng hai đơn vị |
| E3 | Tái hiện lost update và đánh giá OCC | Hai transaction cùng đọc counter; lần lượt thử ghi thường, ghi có version và ghi có version kèm retry | Phân biệt cập nhật bị mất, xung đột được phát hiện và yêu cầu hoàn tất sau retry |
| E4 | Kiểm chứng deadlock và thứ tự lấy khóa | Hai transaction cập nhật hai dòng A/B theo thứ tự ngược nhau hoặc cùng thứ tự | Phân biệt deadlock với chờ thông thường; kiểm tra rollback, retry và giá trị cuối |

Trong **E1**, giá trị `warehouse.w_ytd` ban đầu là 300.000. T1 cập nhật thành 300.100 nhưng chưa commit. T2 thực hiện SELECT thông thường qua connection khác, sau đó đọc lại trong cùng transaction READ COMMITTED khi T1 đã commit. Lịch này kiểm tra cả việc đọc phiên bản đã commit và sự thay đổi kết quả giữa hai câu SELECT.

Trong **E2**, T1 và T2 cùng thực hiện phép tăng `stock.s_ytd` trên dòng có khóa `(s_w_id = 1, s_i_id = 1)`. Khác với việc ứng dụng đọc một giá trị rồi ghi lại giá trị tính sẵn, câu lệnh cập nhật trực tiếp thực hiện phép tăng trong SQL:

```sql
UPDATE stock
SET s_ytd = s_ytd + 1
WHERE s_w_id = 1 AND s_i_id = 1;
```

T1 giữ transaction chưa commit để T2 phải chờ. Metadata được lấy trong thời gian chờ nhằm xác nhận quan hệ khóa, thay vì chỉ suy luận từ độ trễ của câu lệnh.

Trong **E3**, chương trình sao chép 16 dòng stock vào bảng hỗ trợ có tên riêng cho lượt chạy, bổ sung `counter_value` và `version`. Bảng này được chia sẻ giữa các connection và không làm thay đổi cấu trúc bảng stock nguồn. Mỗi pha demo được đặt lại về counter ban đầu 0 và version 1:

1. **Ghi không kiểm tra version:** T1 và T2 cùng đọc 0, cùng tính 1; T1 ghi và commit, sau đó T2 ghi lại 1 và commit.
2. **Phát hiện xung đột bằng version:** cả hai đọc version 1; T1 cập nhật và tăng version lên 2; T2 UPDATE với version cũ nên không khớp dòng nào và rollback.
3. **OCC kết hợp retry:** sau xung đột, T2 mở transaction mới, đọc lại counter/version, tính lại và thực hiện UPDATE có điều kiện.

Câu lệnh dưới đây minh họa điều kiện ghi của OCC; tên bảng thực tế được gắn mã lượt chạy:

```sql
UPDATE bang_ho_tro_occ
SET counter_value = :gia_tri_tinh_lai,
    version = version + 1
WHERE s_w_id = 1
  AND s_i_id = :item
  AND version = :version_da_doc;
```

Trong **E4**, hai dòng stock được ký hiệu A và B. Với lịch `opposite`, T1 lấy khóa A rồi yêu cầu B, còn T2 lấy khóa B rồi yêu cầu A. Với lịch `ordered`, cả hai cùng truy cập A rồi B. Chương trình kiểm tra kết quả transaction sống sót, trạng thái sau rollback, kết quả retry toàn transaction và khả năng khôi phục dữ liệu.

#### 3.1.2.2. Kết quả phân tích trạng thái và tính nhất quán trên ba cơ chế

**Bảng 3.4. Kết quả quan sát trên PostgreSQL.**

| Kịch bản | Trạng thái hoặc kết quả ghi nhận | Ý nghĩa |
|---|---|---|
| E1 — trước commit của T1 | T2 đọc 300.000 và hoàn tất trước khi T1 bắt đầu commit | SELECT không đọc bản cập nhật chưa commit, không phải đợi khóa cập nhật dòng trong lịch này |
| E1 — sau commit của T1 | T2 đọc 300.100 trong cùng transaction READ COMMITTED | Hai câu đọc có thể thấy hai trạng thái đã commit khác nhau |
| E2 — ghi cùng dòng | Có PID chặn/chờ và `wait_event_type = Lock`; counter từ 0 lên 2 sau hai commit | Ghi xung đột được phối hợp bằng khóa; không mất lần tăng trong kịch bản này |
| E3 — ghi thường | Hai COMMIT nhưng counter cuối bằng 1 | Tái hiện lost update của chuỗi đọc–tính–ghi ở ứng dụng |
| E3 — kiểm tra version | Counter = 1, version = 2; phát hiện một conflict | Lần ghi dựa trên dữ liệu cũ bị từ chối |
| E3 — version và retry | Counter = 2, version = 3 | Hai yêu cầu logic hoàn tất sau khi đọc lại và tính lại |
| E4 — thứ tự ngược | 5/5 cặp có đúng một nạn nhân deadlock, mã `40P01` | DBMS phá chu trình chờ bằng cách hủy một transaction |
| E4 — sau retry nạn nhân | Mỗi counter A/B tăng tổng cộng 2 | Retry toàn transaction hoàn tất phần công việc bị hủy |
| E4 — cùng thứ tự | 5/5 cặp có hai COMMIT; không deadlock, không retry | Thống nhất thứ tự lấy khóa loại bỏ chu trình trong lịch A/B đã thiết kế |

Nguồn: các log [E1](experiments/e1_logs/20261024T034759_313380Z/summary.json), [E2](experiments/e2_logs/20261109T152250_363110Z/summary.json), [E3](experiments/e3_logs/20261109T161429_704849Z/summary.json) và [E4](experiments/e4_logs/20261109T163251_991835Z/summary.json).

E1 cho thấy MVCC hỗ trợ việc đọc phiên bản đã commit trong khi một giao dịch khác đang cập nhật. Ở READ COMMITTED, lần đọc sau có thể thấy dữ liệu mới được commit; không nên diễn giải đây là snapshot cố định cho toàn bộ transaction. Kết luận này chỉ áp dụng cho SELECT thông thường trong lịch thử nghiệm, không bao quát locking read hoặc DDL.

E2 làm rõ rằng MVCC không loại bỏ nhu cầu khóa khi nhiều giao dịch ghi cùng bản ghi. Hai phép tăng trực tiếp trong SQL hoàn tất với counter đúng bằng 2. Ngược lại, E3 cho thấy khóa của từng UPDATE không tự khắc phục việc ứng dụng sử dụng giá trị đã đọc cũ: nếu cả hai bên cùng tính 1 rồi ghi lại 1, hai COMMIT vẫn có thể chỉ tạo một lần tăng thực tế.

OCC giải quyết trường hợp E3 bằng cách kiểm tra xem phiên bản đã đọc còn hợp lệ tại thời điểm ghi hay không. Khi không khớp version, ứng dụng phải rollback và quyết định retry hoặc trả thất bại. Retry đúng phải đọc lại và tính lại trong transaction mới; gửi lại nguyên giá trị/version cũ không giải quyết được xung đột. UPDATE có điều kiện vẫn sử dụng cơ chế MVCC và khóa bên dưới của DBMS.

**Bảng 3.5. Đối chiếu vai trò và giới hạn của ba cơ chế từ thử nghiệm nhỏ.**

| Khía cạnh | MVCC | Khóa | OCC ở tầng ứng dụng |
|---|---|---|---|
| Vai trò được kiểm chứng | Cung cấp phiên bản phù hợp cho câu đọc | Điều phối các thao tác ghi xung đột | Phát hiện kết quả tính dựa trên phiên bản cũ |
| Bằng chứng chính | E1 | E2 và E4 | E3 |
| Biểu hiện khi có cạnh tranh | Plain SELECT vẫn đọc bản đã commit trong E1 | Chờ khóa; có thể deadlock khi có vòng chờ | UPDATE không khớp version; rollback và retry |
| Điều kiện để yêu cầu hoàn tất đúng | Phụ thuộc isolation và logic nghiệp vụ | Cần xử lý transaction bị hủy, quản lý thứ tự truy cập | Kiểm tra số dòng cập nhật, đọc lại/tính lại khi retry |
| Giới hạn quan sát | Không tự bảo vệ mọi chuỗi đọc–tính–ghi | Giữ khóa làm tăng chờ; không sửa giá trị tính sẵn đã cũ | Có chi phí thử lại; có thể hết ngân sách retry |

Các lượt tải sơ bộ đi kèm bổ sung bằng chứng về ảnh hưởng của phân bố truy cập. Trong E2 với 16 client, số session chờ khóa mục tiêu lớn nhất được lấy mẫu là 0 khi mỗi client truy cập dòng riêng (`distinct`), 12 khi chia sẻ bốn dòng (`shared4`) và 15 khi cùng truy cập một dòng (`hot1`), trên cả hai DBMS. Đây là giá trị cực đại trong mẫu quan sát, không phải tổng số sự kiện chờ.

Trong E3, tổng các cấu hình nhiều client truy cập hot row trên hai DBMS có 423 yêu cầu logic hết ngân sách retry, dù mỗi yêu cầu được phép tối đa tám lần thử lại sau lần đầu. Counter và version vẫn khớp số yêu cầu thực sự thành công. Do đó, dữ liệu cuối nhất quán theo các yêu cầu đã chấp nhận không đồng nghĩa mọi yêu cầu đầu vào đều hoàn tất. Các lượt sơ bộ chỉ chạy một lần mỗi cấu hình, không warm-up; chúng được dùng để minh họa cơ chế, không dùng làm phép so sánh TPS chính thức.

Các kiểm tra khôi phục dữ liệu E1/E2/E4 và dọn bảng hỗ trợ E3 đều đạt trong các lượt được trích dẫn. Riêng nhãn PASS của pha lost update có nghĩa là tái hiện đúng lỗi mong đợi, không có nghĩa trạng thái nghiệp vụ cuối là đúng. Tương tự, kết quả không deadlock của lịch `ordered` chỉ chứng minh cho mô hình hai dòng đã thử, không bảo đảm mọi workload phức tạp đều hết deadlock.

### 3.1.3. Đánh giá hiệu năng với workload lớn

#### 3.1.3.1. Thiết lập workload lớn và các biến số kiểm thử thông qua Framework BenchBase

E6 sử dụng BenchBase để sinh đồng thời năm loại giao dịch nghiệp vụ trên dataset TPC-C-like. Trong phần này, “workload lớn” được hiểu là số lượng yêu cầu và mức đồng thời lớn hơn các demo có điều phối; dataset vẫn chỉ có một warehouse và chưa đại diện cho cơ sở dữ liệu quy mô sản xuất.

**Bảng 3.6. Thành phần workload hỗn hợp.**

| Loại giao dịch | Trọng số cấu hình | Nội dung nghiệp vụ chính |
|---|---:|---|
| NewOrder | 45% | Tạo đơn hàng và cập nhật tồn kho |
| Payment | 43% | Ghi nhận thanh toán, cập nhật số liệu kho, district và khách hàng |
| OrderStatus | 4% | Tra cứu trạng thái đơn hàng |
| Delivery | 4% | Xử lý các đơn hàng chờ giao |
| StockLevel | 4% | Kiểm tra mức tồn kho |

Đây là trọng số sinh yêu cầu, không phải tỷ lệ SUCCESS bắt buộc quan sát được. Với một warehouse, nhiều Payment có thể cập nhật cùng dòng tổng tiền thanh toán `warehouse.w_ytd`; NewOrder còn chia sẻ district và stock. Thiết kế này tạo điều kiện quan sát contention tập trung, nhưng không có nghĩa mọi request đều ghi vào một dòng duy nhất.

**Bảng 3.7. Biến số và điều kiện kiểm soát của E6.**

| Nhóm | Thiết lập |
|---|---|
| Biến khảo sát chính | Số client đồng thời: 1, 4, 8, 16, 32, 64, 128 |
| Biến đối chứng | PostgreSQL và MySQL InnoDB |
| Điều kiện dữ liệu | Một warehouse; restore baseline riêng của engine trước từng lượt |
| Isolation và tải | READ COMMITTED; `rate = unlimited`; mô hình client khép kín, không thêm think time |
| Thời gian | Warm-up 15 giây, sau đó đo cấu hình 60 giây |
| Số lần lặp | Ba lượt cho mỗi cặp DBMS–số client |
| Ngân sách retry | Tối đa ba attempt tổng cộng cho mỗi request, gồm lần đầu |
| Thu thập bổ sung | Metadata session/lock khoảng 1 giây/lần; Docker CPU/RAM khoảng 2 giây/lần |

Trong mô hình client khép kín, số client là mức đồng thời được cấu hình; đây không phải phép đo khả năng đáp ứng một luồng yêu cầu ngoài hệ thống có tốc độ đến cố định. Mỗi client tiếp tục phát yêu cầu sau khi xử lý yêu cầu trước, nên độ trễ và tốc độ phát tải có liên hệ với nhau.

Hai chỉ tiêu hiệu năng chính được định nghĩa như sau:

- **Successful TPS:** số request có trạng thái cuối SUCCESS, bắt đầu và kết thúc trong cửa sổ đo hợp lệ, chia cho thời gian đo thực tế do BenchBase ghi nhận. Chỉ tiêu này tính toàn bộ hỗn hợp giao dịch, không phải tpmC được chứng nhận.
- **p95 của request thành công:** phân vị 95% độ trễ của các request SUCCESS trong từng lượt, bao gồm các attempt và retry bên trong request. Khi tổng hợp ba lượt, báo cáo sử dụng trung bình của ba giá trị p95, không phải p95 tính trên tập request gộp.

Với lượt chạy thứ \(i\), công thức thông lượng là:

\[
TPS_i = \frac{N_{SUCCESS,i}}{T_{MEASURE,i}},\qquad
\overline{TPS} = \frac{TPS_1 + TPS_2 + TPS_3}{3}.
\]

Thời gian đo thực tế có thể dài hơn 60 giây cấu hình khoảng một giây; phép tính sử dụng giá trị thực tế. Khoảng min–max trong các bảng và hình biểu diễn độ phân tán của ba lượt, không phải khoảng tin cậy thống kê.

Bản BenchBase sử dụng có bộ đếm SUCCESS gốc bao gồm warm-up. Vì vậy, dự án bổ sung CSV theo request tại đúng điều kiện ghi latency để tính successful TPS trong cửa sổ đo, thay vì sử dụng trực tiếp goodput gốc. Số request được đối chiếu với `Measured Requests` của BenchBase. Việc ghi metrics có thể tạo overhead và được áp dụng cho cả hai DBMS.

#### 3.1.3.2. Chạy thực nghiệm trên PostgreSQL

Ma trận PostgreSQL gồm bảy mức client, mỗi mức ba lượt, tương ứng 21 lượt chính thức. Mỗi lượt thực hiện theo quy trình:

1. Phục hồi `tpcc_bench` từ baseline PostgreSQL, kiểm tra dữ liệu và chạy `ANALYZE`.
2. Áp dụng hạn mức tài nguyên và cấu hình kết nối; chỉ để PostgreSQL chịu tải, dừng DBMS đối chứng trong lượt đo.
3. Chạy warm-up 15 giây rồi chuyển liên tục sang pha đo 60 giây, không phục hồi dữ liệu giữa hai pha.
4. Thu request outcomes, độ trễ, metadata chờ khóa và mẫu tài nguyên.
5. Đối chiếu số request, kiểm tra các bất biến dữ liệu và lưu đầu ra của lượt chạy.

PostgreSQL được đặt `lock_timeout = 5s` và `statement_timeout = 15s` cho workload. Vòng retry của BenchBase được bổ sung xử lý deadlock `40P01` nhằm tương ứng với nhánh xử lý deadlock MySQL. Retry gọi lại procedure cùng loại và có thể sinh lại tham số, nên không đồng nhất với việc thử lại một yêu cầu nghiệp vụ có tham số cố định như demo E4.

Toàn bộ 42 lượt của hai hệ, bao gồm 21 lượt PostgreSQL, đạt kiểm tra thực thi và các bất biến dữ liệu được lựa chọn. Những bất biến này gồm: tổng thanh toán warehouse bằng tổng district; số đơn kế tiếp khớp mã đơn lớn nhất; tổng số order line khớp số dòng khai báo trong order; và stock quantity nằm trong khoảng kiểm tra. Kết quả này không thay thế bộ kiểm định đầy đủ TPC-C, cũng không chứng minh toàn bộ lịch giao dịch là serializable.

Chi tiết từng lượt được lưu tại [trials.csv](benchbase/benchmark/analysis/trials.csv); kết quả tổng hợp tại [summary.csv](benchbase/benchmark/analysis/summary.csv). Các lần chạy smoke và tải sơ bộ E2/E3 không được trộn vào ma trận chính thức.

#### 3.1.3.3. Kết quả hiệu năng và đối chiếu vai trò của ba cơ chế

**Bảng 3.8. Hiệu năng PostgreSQL theo số client, mỗi cấu hình ba lượt.**

| Client | Successful TPS trung bình | TPS min–max | Trung bình p95 (ms) |
|---:|---:|---:|---:|
| 1 | 430,19 | 398,47–462,77 | 5,54 |
| 4 | 660,95 | 641,77–697,81 | 11,17 |
| 8 | 621,37 | 614,10–634,77 | 38,08 |
| 16 | 446,19 | 314,62–542,17 | 143,43 |
| 32 | 428,01 | 379,05–454,56 | 329,51 |
| 64 | 320,03 | 305,25–337,83 | 955,85 |
| 128 | 235,23 | 214,23–251,98 | 2.545,62 |

Nguồn: [summary.csv](benchbase/benchmark/analysis/summary.csv); các số được làm tròn đến hai chữ số thập phân.

Thông lượng PostgreSQL tăng từ 430,19 TPS ở một client lên mức trung bình cao nhất trong các mức đã thử là 660,95 TPS ở bốn client. Khi tăng lên tám client, thông lượng còn 621,37 TPS; ở 128 client giảm xuống 235,23 TPS. Như vậy, khả năng phục vụ không tăng tuyến tính theo số kết nối. Sau vùng đỉnh quan sát, tăng concurrency đi kèm suy giảm thông lượng và gia tăng rõ rệt độ trễ đuôi.

Trung bình p95 tăng từ 5,54 ms ở một client lên 329,51 ms ở 32 client và 2.545,62 ms ở 128 client. Tại mức 16 client, TPS dao động từ 314,62 đến 542,17; cả ba lượt đều được giữ lại. Chưa có telemetry đủ để xác định nguyên nhân của lượt chậm là nhiệt CPU, paging, I/O hay một tiến trình nền cụ thể.

Các mẫu tài nguyên cho thấy PostgreSQL sử dụng xấp xỉ hạn mức CPU từ tám client. Tỷ lệ session chờ khóa trung bình trong mẫu khoảng 70,65% ở 32 client và 47,12% ở 128 client. Việc tỷ lệ chờ khóa giảm ở mức 128 không chứng minh contention giảm: session có thể dành thời gian cho thực thi hoặc chờ các tài nguyên khác. Metadata này không đo toàn bộ thời gian chờ và cũng không chỉ ra dòng dữ liệu gây ra từng wait trong E6.

Liên hệ với kiểm thử nhỏ, E1 giải thích khả năng đọc phiên bản đã commit khi có cập nhật, còn E2/E4 cho thấy ghi cạnh tranh vẫn phải đối mặt với khóa và deadlock. Các quan sát đó hỗ trợ diễn giải xu hướng bão hòa của E6 nhưng chưa tách được đóng góp nhân quả của từng cơ chế vào TPS hoặc p95.

**Bảng 3.9. Phạm vi kết luận hiệu năng đối với ba cơ chế.**

| Cơ chế | Bằng chứng hiện có | Kết luận có thể rút ra | Nội dung chưa được đo |
|---|---|---|---|
| MVCC | E1 và workload tổng thể E6 | SELECT trong E1 đọc bản đã commit mà không phải đợi UPDATE đang giữ khóa dòng | TPS/latency riêng của MVCC khi các yếu tố khác được giữ nguyên |
| Khóa | E2, E4 và mẫu lock wait trong E6 | Ghi cùng dòng có chờ; thứ tự lấy khóa ảnh hưởng deadlock trong lịch đã thử | Phần trăm suy giảm TPS chỉ do khóa, tách khỏi CPU/I/O và các chi phí khác |
| OCC ứng dụng | E3, gồm demo và tải sơ bộ | Version phát hiện stale write; retry có thể hoàn tất yêu cầu nhưng có chi phí và giới hạn | Workload E6 có version-column OCC để so sánh công bằng với cách cập nhật khác |

Vì vậy, kết quả định lượng chính của mục này là hiệu năng PostgreSQL dưới workload hỗn hợp. Để xếp hạng trực tiếp ba phương án kiểm soát đồng thời, cần bổ sung một ma trận thí nghiệm có cùng thao tác nghiệp vụ, cùng dữ liệu, phân bố truy cập, ngân sách retry và cách tính latency; đây là hướng mở rộng, chưa phải kết quả đã thực hiện.

## 3.2. So sánh với hệ quản trị cơ sở dữ liệu truyền thống

### 3.2.1. Lựa chọn hệ quản trị cơ sở dữ liệu đối chứng

MySQL 8.4.13 với InnoDB được lựa chọn vì dự án đã có môi trường chạy, dữ liệu tương ứng và hỗ trợ cùng workload BenchBase. Việc dùng chung mô hình dữ liệu, mix giao dịch và mức isolation giúp phép đối chiếu tập trung vào hành vi của hai DBMS dưới các điều kiện được công bố.

Cụm từ “hệ quản trị cơ sở dữ liệu truyền thống” trong chương được hiểu là hệ quản trị quan hệ đối chứng. PostgreSQL cũng là một hệ quản trị cơ sở dữ liệu quan hệ; InnoDB cũng sử dụng cơ chế đa phiên bản và khóa. Do đó, phép đo này không đại diện cho so sánh “PostgreSQL có MVCC” với “MySQL chỉ dùng khóa”, mà là so sánh hai hiện thực DBMS khi cùng xử lý workload giao dịch có tranh chấp.

Các demo trên MySQL tái hiện cùng kết quả logic như PostgreSQL: E1 đọc 300.000 trước commit rồi 300.100 sau commit; E2 hoàn tất với counter tăng hai đơn vị; E3 phát hiện lost update và khắc phục bằng version/retry; E4 có 5/5 cặp deadlock khi lấy khóa ngược thứ tự và không deadlock ở 5/5 cặp cùng thứ tự. Khác biệt quan sát ở E4 là mã lỗi: PostgreSQL trả `40P01`, MySQL trả mã 1213 với SQLSTATE `40001`. Sự tương đồng về kết quả logic này tạo cơ sở cho việc đối chiếu hiệu năng mà không đánh đồng tốc độ với tính đúng đắn.

### 3.2.2. Thiết lập thực nghiệm đối chứng

Hai DBMS được chạy lần lượt trên cùng máy, mỗi lượt chỉ một hệ chịu tải. Tổng ma trận là:

\[
2\ \text{DBMS} \times 7\ \text{mức client} \times 3\ \text{lần lặp}
= 42\ \text{lượt}.
\]

Thứ tự engine được đảo ở lần lặp thứ hai nhằm giảm ảnh hưởng có hệ thống của thứ tự chạy. Mỗi lượt đều phục hồi baseline riêng, warm-up và đo lại theo cùng giao thức. Sau toàn bộ ma trận, dữ liệu nguồn `tpcc` được xác nhận không đổi theo fingerprint đã sử dụng, hai `tpcc_bench` được phục hồi baseline và quyền metadata tạm được thu hồi.

**Bảng 3.10. Các điều kiện tương đồng và khác biệt cần giữ khi diễn giải.**

| Hạng mục | Điều kiện chung | Khác biệt hoặc giới hạn |
|---|---|---|
| Dữ liệu | Cùng chín bảng, một warehouse, cùng quy mô ban đầu | Baseline riêng từng engine; không khẳng định giống mọi ô dữ liệu |
| Tài nguyên | Mỗi DBMS 2 vCPU, 2 GiB RAM | Cùng hạn mức không tạo ra cùng kiến trúc hoặc chi phí thực thi |
| Isolation | Kết nối benchmark cùng READ COMMITTED | MySQL global vẫn mặc định REPEATABLE READ; workload được đặt isolation riêng |
| Cache | Hai tham số cache chính đều ghi nhận 128 MiB | Không phải tổng bộ nhớ của engine; không kiểm soát cold cache hệ điều hành |
| Durability | Giữ các thiết lập ghi bền vững đã ghi nhận | MySQL có binary log; chi phí ghi log không hoàn toàn tương đương |
| Timeout | Thời hạn chờ khóa cấu hình 5 giây | PostgreSQL có statement timeout 15 giây; MySQL có SELECT timeout 15 giây, phạm vi khác nhau |
| Retry | Tối đa ba attempt; có xử lý deadlock cho cả hai | Retry có thể sinh lại tham số; không thêm retry riêng cho lock timeout |
| Sinh tải | Cùng mix, số client, thời gian và `randomSeed` cấu hình | RNG riêng trong TPCCWorker khiến chuỗi tham số không được bảo đảm giống hệt |

Các khác biệt này được công bố để xác định đúng phạm vi công bằng của phép đo: kết quả so sánh hai cấu hình triển khai cụ thể, không phải phép cô lập duy nhất thuật toán điều khiển tương tranh.

### 3.2.3. Kết quả so sánh hiệu năng

**Bảng 3.11. Successful TPS trung bình của hai hệ.**

| Client | PostgreSQL (TPS) | MySQL (TPS) | PostgreSQL/MySQL (lần) |
|---:|---:|---:|---:|
| 1 | 430,19 | 137,20 | 3,14 |
| 4 | 660,95 | 236,60 | 2,79 |
| 8 | 621,37 | 238,95 | 2,60 |
| 16 | 446,19 | 197,08 | 2,26 |
| 32 | 428,01 | 231,63 | 1,85 |
| 64 | 320,03 | 218,63 | 1,46 |
| 128 | 235,23 | 219,93 | 1,07 |

Tỷ số được tính từ số trung bình chưa làm tròn trong [summary.csv](benchbase/benchmark/analysis/summary.csv). Mỗi hàng đại diện cho ba lượt trên mỗi hệ.

![Hình 3.1. Successful TPS theo số client](</C:/DBMS/Project Report/benchbase/benchmark/analysis/01_successful_tps.png>)

*Hình 3.1. Thông lượng request thành công theo mức đồng thời. Chấm/đường chính thể hiện trung bình ba lượt; thanh dọc biểu diễn min–max, chấm nhỏ thể hiện từng lượt. Trục số client dùng thang log₂. Hình được lấy từ đầu ra phân tích đã lưu trong dự án.*

PostgreSQL có TPS trung bình cao hơn ở cả bảy mức đã đo. Chênh lệch lớn nhất theo tỷ số nằm ở một client, khoảng 3,14 lần; ở 128 client, tỷ số còn khoảng 1,07 lần. PostgreSQL đạt mức trung bình cao nhất quan sát được ở bốn client, trong khi MySQL đạt 238,95 TPS ở tám client. Đối với MySQL, các khoảng đo ở nhiều mức tải có chồng lấn nên chưa đủ cơ sở gọi tám client là điểm tối ưu đã được chứng minh.

Ở 128 client, TPS PostgreSQL nằm trong khoảng 214,23–251,98, còn MySQL là 193,00–236,23. Hai khoảng có chồng lấn; vì vậy, thứ hạng theo trung bình không tự chứng minh khác biệt có ý nghĩa thống kê. Mức 16 client cũng dao động mạnh trên cả hai hệ, với MySQL từ 140,74 đến 234,10 TPS. Không loại bỏ lượt chậm để làm đường biểu diễn đều hơn.

**Bảng 3.12. Trung bình p95 của request thành công.**

| Client | PostgreSQL (ms) | MySQL (ms) | Hệ có p95 trung bình thấp hơn |
|---:|---:|---:|---|
| 1 | 5,54 | 17,81 | PostgreSQL |
| 4 | 11,17 | 35,16 | PostgreSQL |
| 8 | 38,08 | 74,13 | PostgreSQL |
| 16 | 143,43 | 251,97 | PostgreSQL |
| 32 | 329,51 | 392,48 | PostgreSQL |
| 64 | 955,85 | 819,02 | MySQL |
| 128 | 2.545,62 | 1.526,48 | MySQL |

![Hình 3.2. p95 theo số client](</C:/DBMS/Project Report/benchbase/benchmark/analysis/02_p95_latency.png>)

*Hình 3.2. Trung bình p95 từng lượt của request thành công, bao gồm retry bên trong request. Cả hai trục dùng thang log theo nhãn trong hình; thanh dọc là min–max của ba p95, không phải khoảng tin cậy hoặc phân vị của tập request gộp.*

PostgreSQL có p95 trung bình thấp hơn ở các mức 1–32 client, nhưng thứ hạng đảo chiều ở 64 và 128 client. Tại 128 client, PostgreSQL đạt 2.545,62 ms, trong khi MySQL là 1.526,48 ms. Các p95 từng lượt tương ứng nằm trong khoảng 2.401,06–2.727,11 ms và 1.403,00–1.712,69 ms. Kết quả cho thấy lợi thế về TPS không đồng nghĩa lợi thế về độ trễ đuôi ở mọi mức tải.

Ngoài TPS và latency, cần phân biệt các trạng thái kết thúc request. Bảng 3.13 tổng hợp toàn bộ 42 lượt; các số dưới đây là tổng gộp, không phải trung bình tỷ lệ theo cấu hình.

**Bảng 3.13. Phân loại kết quả request trong cửa sổ đo của hai DBMS.**

| Chỉ tiêu | Số lượng | Diễn giải |
|---|---:|---|
| Tổng request đo | 848.494 | Request nằm trong cửa sổ đo hợp lệ |
| SUCCESS | 839.700 | Được tính vào successful TPS |
| NewOrder rollback cố ý | 3.849 | Khoảng 1,01% trong 382.434 request NewOrder |
| Delivery bị hủy do tranh chấp | 4.945 | Khoảng 14,55% trong 33.981 request Delivery |
| Request kết thúc ở nhóm lỗi SQL/hết retry | 0 | Không bao gồm hai nhóm USER_ABORTED phía trên |
| Request retry rồi thành công | 16 | Là tập con của SUCCESS, không cộng thêm vào tổng request |

Nguồn: [biên bản phân tích](benchbase/benchmark/analysis/analysis-validation.json) và [kiểm tra benchmark chính thức](benchbase/benchmark/step17-verification.json).

NewOrder rollback khi gặp item không hợp lệ là hành vi được thiết kế trong workload. Delivery có thể bị hủy khi một giao dịch khác đã lấy dòng đơn hàng giữa bước SELECT và DELETE, khiến DELETE không còn tác động đúng một dòng. Vì vậy, không được gộp toàn bộ USER_ABORTED thành rollback cố ý của NewOrder. Khóa cũ `expected_user_aborts` trong đầu ra từng lượt chứa cả hai nhóm; phần phân tích đã tách lại theo loại giao dịch. Successful TPS loại cả hai nhóm ngay từ đầu.

Trong 16 request retry rồi thành công, mã lỗi cuối được lưu gồm 10 trường hợp `40P01/0` và sáu trường hợp `40001/1213`. Đây là số request có retry, không phải khẳng định đã đếm toàn bộ mọi sự kiện lỗi SQL. Việc nhóm kết thúc lỗi SQL/hết retry bằng 0 cũng không có nghĩa workload không có hủy do tranh chấp, bởi 4.945 Delivery đã kết thúc ở nhánh USER_ABORTED.

Về bằng chứng hỗ trợ, ở 128 client, MySQL có tỷ lệ session chờ khóa trung bình trong mẫu khoảng 97,37%, CPU khoảng 79,91% hạn mức; PostgreSQL có tỷ lệ session chờ khóa khoảng 47,12% và sử dụng CPU xấp xỉ hạn mức. Hai hệ có thể chịu các giới hạn tài nguyên khác nhau, nhưng chưa đủ dữ liệu để quy toàn bộ chênh lệch hiệu năng cho một nguyên nhân duy nhất. Các [hình chờ khóa](benchbase/benchmark/analysis/03_lock_wait.png), [hình abort/retry](benchbase/benchmark/analysis/04_abort_retry.png) và [hình CPU/RAM](benchbase/benchmark/analysis/05_cpu_memory.png) được lưu để đối chiếu bổ sung.

### 3.2.4. Phân tích ưu điểm và hạn chế

**Bảng 3.14. Tổng hợp ưu điểm quan sát được và giới hạn diễn giải.**

| Đối tượng | Ưu điểm hoặc kết quả tích cực | Hạn chế và đánh đổi |
|---|---|---|
| PostgreSQL | TPS trung bình cao hơn ở cả bảy mức; p95 trung bình thấp hơn tại 1–32 client | TPS giảm sau đỉnh quan sát; p95 tăng mạnh ở tải cao và cao hơn MySQL tại 64–128 client |
| MySQL InnoDB | P95 trung bình thấp hơn PostgreSQL tại 64–128 client; tái hiện cùng các kết quả logic trong demo | TPS trung bình thấp hơn trong cấu hình thử; nhiều session chờ khóa khi concurrency cao |
| MVCC | Hỗ trợ đọc dữ liệu đã commit khi có UPDATE chưa commit trong E1 | Không loại bỏ tranh chấp ghi và không tự khắc phục stale read–modify–write |
| Khóa và thứ tự truy cập | Phối hợp cập nhật cùng dòng; cùng thứ tự tránh deadlock trong E4 | Có thời gian chờ; thứ tự ngược có thể tạo deadlock; ứng dụng vẫn phải xử lý rollback |
| OCC ứng dụng | Phát hiện xung đột phiên bản; retry có thể hoàn tất yêu cầu với kết quả đúng | Có chi phí kiểm tra/thử lại; có thể hết retry ở hot row; chưa có benchmark E6 riêng |

Về mặt thiết kế ứng dụng, kết quả cho thấy nên kết hợp kiểm soát tính đúng đắn với quản lý concurrency. Việc mở thêm connection không bảo đảm tăng năng lực xử lý khi các giao dịch chia sẻ tài nguyên hoặc điểm nóng. Nếu nghiệp vụ sử dụng chuỗi đọc–tính–ghi, cần có cơ chế bảo vệ phù hợp; E3 minh họa một cách thực hiện bằng version và retry. Với nhiều bản ghi cần cập nhật, thống nhất thứ tự truy cập giúp giảm nguy cơ tạo vòng chờ như lịch E4. Những nhận xét này xuất phát từ các kịch bản đã thử, chưa xác định một cấu hình connection pool hay chiến lược retry tối ưu cho mọi triển khai.

Các giới hạn thực nghiệm cần được xét cùng kết quả:

1. **Môi trường local và tài nguyên dùng chung.** Máy laptop, Docker WSL2 và ứng dụng nền cùng sử dụng phần cứng. Chưa đo đủ telemetry để giải thích mọi dao động giữa các lượt.
2. **Phạm vi workload hẹp.** Chỉ khảo sát một warehouse, một mix và READ COMMITTED. Chưa có ma trận thay đổi quy mô dữ liệu, số warehouse hoặc isolation; E6 cũng chưa có biến thể OCC cột version.
3. **Số lần lặp và thời gian đo hữu hạn.** Ba lượt/cấu hình, mỗi lượt đo khoảng 60 giây, chưa đủ để kết luận về tải kéo dài hoặc ý nghĩa thống kê của mọi chênh lệch. Min–max là mô tả độ phân tán.
4. **Điều kiện tính latency.** P95 chính chỉ xét request thành công; request vắt qua ranh giới pha đo bị loại theo điều kiện của BenchBase. Ở tải có latency cao, hiệu ứng biên có thể ảnh hưởng kết quả.
5. **Phép so sánh chưa cô lập mọi yếu tố.** Cùng quota không đồng nghĩa cùng chi phí durability, cache, driver, SQL nội bộ và timeout. Chuỗi tham số ngẫu nhiên cũng không được bảo đảm giống hệt từng request.
6. **Giới hạn quan sát tài nguyên.** Lock wait là mẫu tức thời, không phải tổng thời gian chờ hoặc tỷ lệ transaction từng bị block. Nhãn pha của mẫu CPU/RAM chỉ gần đúng; memory Docker không phải toàn bộ RAM trên Windows.
7. **Phạm vi kiểm chứng dữ liệu.** Các invariant được kiểm tra đều đạt nhưng chưa bao phủ mọi ràng buộc nghiệp vụ, không chứng minh serializability và không tạo ra kết quả TPC-C/tpmC được chứng nhận.

Từ hai lớp bằng chứng, có thể kết luận rằng tính đúng đắn và hiệu năng dưới tải đồng thời phụ thuộc đồng thời vào cơ chế DBMS, lịch truy cập và logic xử lý xung đột của ứng dụng. PostgreSQL có lợi thế về thông lượng trung bình trong cấu hình đã đo, trong khi MySQL có p95 thấp hơn tại hai mức đồng thời cao nhất. Việc lựa chọn và cấu hình hệ quản trị cần cân nhắc cả thông lượng, độ trễ đuôi, tỷ lệ hoàn tất yêu cầu và đặc điểm tranh chấp, thay vì chỉ dựa vào một chỉ tiêu tổng hợp.

### Tài liệu và dữ liệu thực nghiệm dùng trong chương

Các nguồn dưới đây cho phép truy vết nội dung chương và đối chiếu số liệu mà không cần chạy lại workload:

- [Tài liệu kết quả và thảo luận đã có](Experimental_Results_and_Discussion.md): tổng hợp kết quả E1–E4/E6, diễn giải và giới hạn.
- [Giao thức E6](benchbase/benchmark/protocol.json) và [README benchmark](benchbase/benchmark/README.md): cấu hình, quy trình và định nghĩa metrics.
- [Kết quả E1](experiments/e1_logs/20261024T034759_313380Z/summary.json), [E2](experiments/e2_logs/20261109T152250_363110Z/summary.json), [E3](experiments/e3_logs/20261109T161429_704849Z/summary.json), [E4](experiments/e4_logs/20261109T163251_991835Z/summary.json): các lượt kiểm chứng cơ chế được trích dẫn.
- [Báo cáo 42 lượt chính thức](benchbase/benchmark/formal-report.json) và [biên bản kiểm tra](benchbase/benchmark/step17-verification.json): thực thi, phân loại outcomes và invariant.
- [Số liệu từng lượt](benchbase/benchmark/analysis/trials.csv), [số liệu tổng hợp](benchbase/benchmark/analysis/summary.csv) và [biên bản xác minh phân tích](benchbase/benchmark/analysis/analysis-validation.json): nguồn bảng, hình và định nghĩa thống kê.

Các log thử lỗi và smoke vẫn được lưu trong kho mã nguồn nhưng không dùng để tính hiệu năng chính thức. Chương này chỉ sử dụng các lượt cơ chế được chỉ rõ và toàn bộ 42 lượt formal, không hàm ý mọi lần chạy trong quá trình phát triển đều thành công.
