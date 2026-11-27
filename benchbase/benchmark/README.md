# Bước 16 — Chuẩn bị benchmark và kiểm tra ngắn

Phạm vi: chuẩn bị phép đo E6 trên PostgreSQL/MySQL, chạy thử để xác minh công cụ. Kết quả `smoke` không dùng để kết luận hiệu năng. Bước 17 mới chạy ma trận chính thức sau khi người dùng cho phép.

**Bước 17 đã hoàn thành ngày 10/11/2026, sau khi được cho phép:** 42/42 lượt chính thức đạt kiểm tra thực thi và dữ liệu đo: 2 DBMS × 7 mức client × 3 lần lặp. Mỗi lượt restore baseline, warm-up 15s, đo cấu hình 60s. Tổng cộng 848.494 request trong cửa sổ đo, 839.700 SUCCESS và 16 request retry thành công. Có 3.849 NewOrder rollback theo thiết kế và 4.945 Delivery USER_ABORTED do tranh chấp. Đây là các nhóm riêng, không gộp mọi USER_ABORTED thành rollback cố ý của NewOrder. Chi tiết ở `formal-report.json` và biên bản phân loại/kiểm tra lại `step17-verification.json`.

Đã xác nhận fingerprint `tpcc` không đổi, cả hai `tpcc_bench` đã phục hồi baseline, hai dịch vụ được bật lại và quyền metadata tạm đã thu hồi. Cấu hình image/CPU/RAM/cache/durability trước và sau không đổi. Có dao động tốc độ giữa các lượt; giữ toàn bộ ba lần lặp để phân tích, không chọn bỏ lượt chậm. Chưa suy ra nguyên nhân chỉ từ số TPS.

**Bước 18:** Script `analyze_results.py` đọc lại file hoàn toàn offline, xuất 42 dòng từng lượt, 14 dòng tổng hợp và 5 nhóm hình PNG/SVG (2 hình chính, 3 hình hỗ trợ). Xem [analysis/README.md](analysis/README.md) để chạy lại và đọc đúng min–max, p95, abort, lock samples và CPU/RAM. Không cần bật Docker; dữ liệu thô bước 17 được giữ nguyên.

**Bước 19:** Bản [Kết quả thực nghiệm và thảo luận](../../Experimental_Results_and_Discussion.md) nối bằng chứng E1–E4 với E6, có caption hai hình chính, giới hạn suy luận, danh mục log đã dùng và khung trình bày 15 phút. `step19-verification.json` ghi việc đối chiếu số liệu, liên kết và tính nguyên vẹn của nguồn. Việc soạn báo cáo không tạo workload mới.

**Đã kiểm tra ngày 10/11/2026:** 8 lượt smoke (1/32/64/128 client × 2 DBMS), mỗi lượt warm-up 3s và thời gian đo cấu hình 6s, đều PASS. Đã quan sát đủ số session; số request sidecar khớp BenchBase; 4 invariant sau workload đều đạt. Kiểm tra cuối xác nhận `tpcc` giữ nguyên fingerprint, `tpcc_bench` đã về baseline và quyền metadata tạm đã thu hồi. Bằng chứng ở `step16-verification.json` và `smoke-report.json`. Thời gian đo thực tế có thể dài hơn cấu hình do bộ lập lịch BenchBase; công thức TPS luôn dùng elapsed thực tế từ native summary.

## Những gì đã chuẩn bị

- Giữ database `tpcc` cho demo E1–E4. Benchmark chỉ ghi vào `tpcc_bench`.
- Chụp baseline bằng pg_dump/mysqldump từ dữ liệu 1 warehouse đã nạp, 9 bảng, 599.051 dòng mỗi DBMS. Không tạo lại dữ liệu ngẫu nhiên giữa các lượt.
- Phục hồi `tpcc_bench` trước mỗi lượt; đối chiếu số dòng và các tổng số học với bản gốc; ANALYZE sau restore. Sau toàn bộ lệnh smoke/run, phục hồi lại baseline và bật cả hai DBMS.
- Ghim image DBMS và BenchBase theo digest; lưu cấu hình, phiên bản, giới hạn tài nguyên, SHA-256 và output thô.
- Chỉ chạy tải trên một DBMS tại một thời điểm; dừng DBMS còn lại trong lượt đo.
- Cấp tạm SELECT trên 3 bảng Performance Schema cần dùng, tự thu hồi trong `finally`; có journal ở thư mục mỗi lượt MySQL. Không cần cấp/thu hồi thủ công khi lệnh kết thúc bình thường hoặc có lỗi Python được xử lý.

Runner chỉ được DROP database cố định `tpcc_bench` đã có hồ sơ sở hữu gắn với volume. Nếu tên này có sẵn mà không có hồ sơ, runner dừng. Không xóa volume hoặc database `tpcc`. Không chạy workload/demo khác cùng lúc.

## Giao thức đã chạy ở bước 17

Nguồn cấu hình: `protocol.json`.

- 1 warehouse; TPC-C-like, NewOrder/Payment/OrderStatus/Delivery/StockLevel = 45/43/4/4/4.
- READ COMMITTED cho cả hai; `rate=unlimited`, mô hình client khép kín, không thêm think time.
- Các mức client: 1, 4, 8, 16, 32, 64, 128. Mức 128 là mức khảo sát cao; kết quả thử ngắn chưa chứng minh chịu tải ổn định 60 giây.
- Mỗi lượt: phục hồi baseline → warm-up 15 giây → đo 60 giây; 3 lần lặp. Đổi thứ tự DBMS ở lần lặp chẵn.
- Mỗi DBMS: 2 vCPU, 2 GiB RAM, không swap thêm. Generator: 4 vCPU, 1 GiB RAM, Java heap tối đa 512 MiB.
- `max_connections=200`; PostgreSQL startup options đặt lock timeout 5s, statement timeout 15s; MySQL session đặt InnoDB lock timeout 5s, metadata lock timeout 5s, SELECT timeout 15s. Statement timeout PostgreSQL và SELECT timeout MySQL không có cùng phạm vi.
- Tối đa **3 lần thử tổng cộng** mỗi request, gồm lần đầu. Giữ retry gốc của BenchBase và bổ sung PostgreSQL `40P01` để deadlock được retry như MySQL `1213/40001`. Lock timeout không được thêm retry riêng.
- Giữ các thiết lập durability/cache khác của image hiện có; lưu giá trị thực tế trong `environment.json`. Cùng hạn mức tài nguyên không có nghĩa cấu hình cache nội bộ hai DBMS giống hệt nhau.

Máy triển khai: i7-1355U, 12 luồng logic, RAM vật lý khoảng 15,7 GiB; Docker khoảng 7,6 GiB RAM. Ổ C ít dung lượng nên có kiểm tra còn ít nhất 2 GiB trước restore/lượt đo. Không tự đóng ứng dụng của người dùng. Hoạt động ứng dụng nền có thể gây nhiễu.

## Lệnh sử dụng trong PowerShell tại thư mục dự án

Đọc protocol, không chạy Docker:

```powershell
.\.venv\Scripts\python.exe .\benchbase\benchmark\run_benchmark.py plan
```

Chuẩn bị/kiểm tra lại database riêng và baseline:

```powershell
.\.venv\Scripts\python.exe .\benchbase\benchmark\run_benchmark.py setup
```

Smoke test: mặc định 1, 32, 64, 128 client mỗi DBMS, warm-up 3s, đo 6s, 1 lượt. Có thể chọn mức để kiểm tra riêng:

```powershell
.\.venv\Scripts\python.exe .\benchbase\benchmark\run_benchmark.py smoke
.\.venv\Scripts\python.exe .\benchbase\benchmark\run_benchmark.py smoke --clients 1 --dbms postgres
```

**Chỉ khi được duyệt bước 17**, chạy ma trận chính thức:

```powershell
.\.venv\Scripts\python.exe .\benchbase\benchmark\run_benchmark.py run
```

Lệnh `run` không phải một phần đã thực hiện trong bước 16. Warm-up phải lặp lại bên trong từng lượt đo; warm-up hôm nay không thay thế warm-up ở bước 17.

## E6 bổ sung theo mức cô lập

Đợt RC/RR đo 36 lượt với 2 DBMS × 2 mức cô lập × 3 mức client (4, 16, 64) × 3 lần lặp. So sánh ghép cặp RC/RR theo từng DBMS, mức client và lần lặp. Đảo thứ tự RC/RR giữa các cặp kế tiếp theo mức tải và lần lặp. Mỗi lượt dùng cùng baseline, mix, thời gian đo, giới hạn tài nguyên và ngân sách thử lại của bước 17.

Chế độ `--compare-isolation` thêm `compose.isolation.yaml`. Override tắt binary log của MySQL bằng `--skip-log-bin` cho cả RC và RR. Runner kiểm tra `@@GLOBAL.log_bin` trước từng lượt MySQL. InnoDB vẫn ghi redo log theo cấu hình ghi bền hiện có. BenchBase kiểm tra mức cô lập JDBC khi mở kết nối và rollback giao dịch kiểm tra trước khi sinh tải. Mỗi lượt lưu bằng chứng theo worker trong `benchbase.log`.

Bản vá dùng cho E6 nằm ở [instrumentation/Worker.patch](isolation/formal-20261126T030922514842Z/instrumentation/Worker.patch), kèm [instrumentation.json](isolation/formal-20261126T030922514842Z/instrumentation/instrumentation.json) là siêu dữ liệu ghi lúc chạy (trùng khối `instrumentation` trong `environment.json` của đợt). Tệp `runtime/Worker.patch` là bản của đợt RC, không có phần kiểm tra mức cô lập JDBC. Build lại bằng `build_metrics.py --verify-isolation` cho đúng `patch_sha256` và `helper_sha256`; hash của jar build lại không trùng bản đã chạy (bản dựng lại dùng JDK 25.0.2 và đóng gói ở thời điểm khác), nên `instrumented_jar_sha256` trong `instrumentation.json` là hash của jar đã chạy.

Override cũng ghim PostgreSQL 17.12. Lần chạy thử ma trận chính thức trên 17.11 dừng khi RR ở 64 client gặp lỗi nội bộ MultiXact (`XX000`). Nhóm lưu nguyên lượt lỗi và chạy lại ma trận với 17.12 cho cả RC và RR. Bản sửa được ghi trong [ghi chú PostgreSQL 17.12](https://www.postgresql.org/docs/17/release-17-12.html).

```powershell
.\.venv\Scripts\python.exe .\benchbase\benchmark\build_metrics.py --verify-isolation
.\.venv\Scripts\python.exe .\benchbase\benchmark\run_benchmark.py smoke --compare-isolation
.\.venv\Scripts\python.exe .\benchbase\benchmark\run_benchmark.py run --compare-isolation
```

Để chạy lại riêng lượt 3, dùng `run_benchmark.py run --compare-isolation --first-repetition 3 --repetitions 1`. Lệnh này giữ thứ tự DBMS và RC/RR của lượt 3 và ghi dữ liệu vào một đợt riêng. Sau khi kiểm tra cả hai đợt hoàn tất, dùng `analyze_isolation.py <đợt-gốc> --replace-third-from <đợt-chạy-lại>`. Thư mục chạy lại nhận bảng tổng hợp của 36 lượt, gồm lượt 1–2 từ đợt gốc và lượt 3 mới. Tệp đầu vào của đợt gốc được giữ để đối chiếu.

Mỗi đợt tạo `isolation/smoke-*` hoặc `isolation/formal-*` chứa cấu hình quan sát, danh sách lượt và kiểm tra phục hồi baseline. Dữ liệu thô nằm trong `results/<dbms>/<kind>-isolation-*`; thư mục mỗi lượt được ghi trong báo cáo JSON của đợt. Các đầu ra bước 17 dùng cho khảo sát mức tải ở RC trong cấu hình cũ. Các số đo của đợt RC/RR được kiểm chứng và tổng hợp độc lập:

```powershell
$campaign = Get-ChildItem .\benchbase\benchmark\isolation -Directory -Filter 'formal-*' | Sort-Object Name -Descending | Select-Object -First 1
.\.venv\Scripts\python.exe .\benchbase\benchmark\analyze_isolation.py $campaign.FullName
.\.venv\Scripts\python.exe .\benchbase\benchmark\plot_isolation.py $campaign.FullName .\report_assets\e6_isolation
```

`summary.csv` ghi trung bình, min và max của ba lượt. `paired.csv` ghi chênh lệch RR so với RC của từng cặp. Tỷ lệ thử lại dùng tổng yêu cầu đo làm mẫu số. Tỷ lệ Delivery bị hủy dùng số yêu cầu Delivery. Các cột mã SQL đếm yêu cầu theo mã lỗi SQL cuối đã lưu, kể cả yêu cầu thành công sau khi thử lại.

Runner tự ghép `compose.yaml` và `compose.override.yaml`. Chạy riêng Compose gốc có thể bỏ giới hạn benchmark trong lần recreate sau; runner luôn áp dụng lại override. Bản BenchBase gốc vẫn dùng khi không có override.

Khi chuyển máy/clone repo mới, runtime binary và baseline được gitignore. Bật Docker, cài JDK 24 và dependency Python hiện có, rồi build instrumentation trước `setup`:

```powershell
.\.venv\Scripts\python.exe .\benchbase\benchmark\build_metrics.py --jdk 'C:\Program Files\Java\jdk-24'
.\.venv\Scripts\python.exe .\benchbase\benchmark\run_benchmark.py setup
```

Build tự lấy JAR/lib từ đúng hai image được ghim khi thiếu, kiểm tra SHA-256 rồi biên dịch. Database `tpcc` nguồn phải đã nạp và ở trạng thái ban đầu như bước 10. Không build lại runtime trong khi đang chạy benchmark.

## Định nghĩa số liệu

BenchBase được ghim ở commit `33c00473807ebd49304d114a6d769d2d2b2bbb34`. Ở bản này, histogram trạng thái trong Worker đếm cả warm-up. `Goodput (requests/second)` gốc lấy histogram SUCCESS chia thời gian đo nên không dùng làm successful TPS nếu có warm-up.

`build_metrics.py` biên dịch một bản vá nhỏ của Worker và helper `java/.../RequestMetrics.java`. SQL của 5 procedure và cách phân phối transaction giữ nguyên. Cùng bản vá được dùng cho cả hai profile. Mã nguồn gốc, diff, mã biên dịch và SHA-256 ở `runtime/`; source URL chính xác ở `runtime/instrumentation.json`. JAR gốc được giữ lại. Build bằng JDK 24 với `--release 23`; image chạy Java 23.0.2.

Mỗi request được ghi vào `requests/worker-*.csv` **ngay tại điều kiện ghi latency gốc**, sau khi đo end timestamp. Chỉ lấy request bắt đầu/kết thúc trong pha MEASURE hợp lệ, cùng phase ID. Request vắt qua ranh giới warm-up/đo hoặc kết thúc sau ranh giới đo bị loại như BenchBase gốc. Do đó TPS là completion rate theo cửa sổ của BenchBase, có hiệu ứng biên ở tải có latency cao. Warm-up vẫn thay đổi dữ liệu và cache trước khi đo, đúng mô hình chạy liên tục.

- `successful_tps = số request có trạng thái cuối SUCCESS / elapsed_measure_seconds`.
- `latency_success_ms`: p50/p95/p99 của request thành công, gồm toàn bộ thời gian từ đầu doWork đến cuối doWork và các lần thử lại bên trong. Không gồm hàng đợi trước khi worker bắt đầu hoặc I/O ghi CSV sau end timestamp.
- `latency_all_completed_ms`: cùng định nghĩa thời gian nhưng bao gồm cả user abort/failure, công bố riêng.
- `requests_with_retry`: số request đã thực sự bắt đầu nhiều hơn 1 lần thử. `reexecutions_started = Σ(attempts−1)`; `retry_events` đếm sự kiện retryable, có thể gồm lỗi cuối khi đã hết ngân sách.
- USER_ABORTED là nhóm do BenchBase chủ động rollback. NewOrder hủy khi item không hợp lệ; Delivery cũng hủy khi dòng `new_order` đã bị một Delivery khác lấy mất giữa SELECT và DELETE dưới READ COMMITTED. Không coi toàn bộ USER_ABORTED là rollback cố ý của NewOrder. Mã nguồn đúng commit được giữ trong `runtime/vendor/src/main/java/com/oltpbenchmark/benchmarks/tpcc/procedures/`.
- Khóa cũ `expected_user_aborts` trong kết quả mỗi lượt chứa **tất cả** USER_ABORTED. Giữ nguyên output đã chạy để truy vết; `step17-verification.json` tách `neworder_intentional_aborts`, `delivery_conflict_aborts` và số hủy theo transaction từng lượt. `failed_requests` cũ chỉ gồm ERROR/RETRY cuối/UNKNOWN/RETRY_DIFFERENT cuối, nên giá trị 0 không có nghĩa không có Delivery bị hủy vì tranh chấp. Successful TPS đã loại mọi USER_ABORTED ngay từ đầu, không bị sai bởi cách đặt tên khóa cũ.
- `last_sql_error_counts` giữ lỗi SQL cuối gặp trong request, kể cả request retry xong thành công; đây không phải histogram mọi lần phát sinh lỗi.
- Luôn đối chiếu số dòng sidecar với `Measured Requests` gốc. Không khớp, thiếu session, thiếu sampler, OOM hoặc SQLSTATE ngoài nhóm dự kiến thì lượt chạy FAIL.
- MySQL metadata: đếm session và waiter trong Performance Schema của database benchmark. PostgreSQL: session theo application_name và waiter loại Lock. Đây là mẫu tức thời, không phải tổng số lần chờ hoặc tổng thời gian chờ.
- SQL sampler khoảng 1s/lần; Docker stats khoảng 2s/lần, có lưu thời gian thu thực tế. CPU 100% trong Docker tương đương 1 vCPU. Nhãn pha của sample tài nguyên lấy từ log, chỉ gần đúng; CSV request dùng điều kiện pha chính xác.

Ghi metrics có overhead ở cả hai engine; writer buffer theo worker, không nằm trong latency timestamp nhưng có thể ảnh hưởng throughput. Đây là benchmark đã instrument, không phải kết quả của BenchBase nguyên bản.

## Giới hạn và kiểm tra

- Đây là **TPC-C-like 1 warehouse trên máy local**, tập trung tranh chấp. Không phải kết quả TPC-C chuẩn/tpmC được chứng nhận.
- 10 district được chia sẻ khi client >10. Đã kiểm tra mã nguồn: khoảng district có độ rộng 0 vẫn trả về district hợp lệ với công thức random hiện tại. Không cần tăng warehouse chỉ để mở 32/64/128 client; smoke phải xác nhận session và không có SQL lỗi phạm vi district.
- `randomSeed` cố định không đảm bảo chuỗi tham số từng transaction giống hệt nhau: TPCCWorker có Random riêng không seed. Kiểm soát cùng dữ liệu, mix và phân phối; lặp nhiều lượt để đánh giá nhiễu.
- Retry ở đây theo định nghĩa vòng `doWork` của BenchBase: gọi lại procedure cùng loại transaction; procedure có thể sinh lại tham số. Không suy diễn số liệu này thành thời gian retry một yêu cầu nghiệp vụ có tham số cố định.
- Fingerprint baseline là kiểm tra số dòng và tổng số học; không phải checksum mọi ô hoặc bộ kiểm định đầy đủ TPC-C. Các kiểm tra sau workload cũng chỉ là một số invariant nghiệp vụ.
- Cache là cache sau restore/ANALYZE và warm-up, không ép cold cache hệ điều hành. Docker VM và host dùng chung ổ đĩa, bộ nhớ và ứng dụng nền.
- Java sidecar flush bằng shutdown hook. Khi kill cưỡng bức/mất điện, output có thể thiếu; không dùng số liệu đó. Nếu Python bị kill cưỡng bức, `finally` cũng có thể không thu hồi quyền: xem journal MySQL rồi thu hồi đúng các quyền đã cấp thêm. Không thu hồi quyền có sẵn trước lượt chạy.

## Đầu ra

- `environment.json`: tài nguyên Docker, image/version, cache/durability thực tế, protocol và instrumentation.
- `baseline/*-ownership.json`: fingerprint, hash archive, volume sở hữu; archive để reset có kiểm soát.
- `results/<dbms>/<run-id>/`: XML thực chạy, run manifest, BenchBase log/native CSV/JSON, request outcomes, DB samples, CPU/RAM samples, kết quả đã tính lại, journal quyền MySQL.
- `smoke-report.json`: kiểm tra ngắn, không đưa vào biểu đồ hiệu năng chính thức.
- `last-cleanup.json`: xác nhận phục hồi baseline sau lệnh smoke/run.
- `step16-verification.json`: kết quả kiểm tra cuối bước 16. Có thể chạy lại kiểm tra chỉ đọc bằng `verify_ready.py` trước khi bắt đầu bước 17.
- `formal-report.json`: tổng hợp 42 lượt chính thức; mỗi lượt tham chiếu đến thư mục `formal-*` có đầy đủ raw output.
- `step17-execution.log`: log điều phối toàn bộ ma trận, gồm phục hồi và cấp/thu hồi quyền metadata.
- `step17-environment-before.json`, `step17-environment-after.json`: snapshot môi trường trước và sau ma trận.
- `step17-verification.json`: đối chiếu lại CSV request, số request native, successful TPS/p95, XML, hash baseline/JAR, invariant, phân loại USER_ABORTED và trạng thái dọn dẹp.

Sau bước 17, dùng `verify_formal.py` để chạy lại việc đối chiếu kết quả đã có; script không tạo workload mới. `verify_ready.py` là kiểm tra trước khi có lượt formal, nên không dùng thay cho kiểm tra sau bước 17.

```powershell
.\.venv\Scripts\python.exe .\benchbase\benchmark\verify_formal.py
```

Không trộn các thư mục `smoke-*` với `formal-*`. Nếu cần thay protocol sau pilot, công bố protocol cuối trước bước 17 và chạy lại các lượt chính thức theo cùng cấu hình.
