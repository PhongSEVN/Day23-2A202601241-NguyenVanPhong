# Runbook 1 trang — Region chính down

Runbook này viết để một người KHÔNG viết code cũng chạy được lúc 3 giờ sáng. Mỗi bước có
lệnh copy-paste được và cách biết bước đó đã xong, không cần đoán.

| # | Bước | Lệnh | Biết là xong khi | Ai làm |
|---|---|---|---|---|
| 1 | Xác nhận outage | `python chaos/kill_region.py status` | `a.alive=false` 3 lần liên tiếp (cách nhau ~5s) | on-call |
| 2 | Mở incident + bấm giờ RTO | `python dr/runbook.py --primary a --target b --backend fs` (bỏ `--auto` khi làm thật, script sẽ hỏi confirm y/N trước khi cutover — chỉ dùng `--auto` cho drill chấm điểm/CI) | dòng `"name": "thong_bao_incident"` xuất hiện trong `reports/runbook-run.jsonl` | on-call |
| 3 | Restore state ở region phụ | tự động (nằm trong bước 2, `dr/failover.py` bước `2_restore_snapshot`) — nếu cần chạy tay: `python state/snapshot.py get --region b --backend fs` | dòng `"step": "2_restore_snapshot"` trong `reports/failover-events.jsonl` có `rpo_seconds` và `docs_lost` khác null | on-call |
| 4 | Scale pool warm→full | tự động (`dr/failover.py` bước `3_scale_pool` rồi `4_wait_ready`) | `curl localhost:8002/readyz` trả 200 | on-call |
| 5 | DNS/LB cutover | tự động (`dr/failover.py` bước `5_dns_cutover`, chỉ chạy SAU khi bước 4 đã ready) | `curl localhost:8080/edge/state` cho `active_region=b` | on-call |
| 6 | Verify golden signals | tự động, 10 request thật (`dr/runbook.py` bước `verify_golden_signals`) — kiểm tra tay nếu cần: `for i in $(seq 10); do curl -s -o /dev/null -w "%{http_code} %{time_total}s\n" localhost:8080/v1/infer; done` | p95 dưới 800ms, error rate dưới 5% trên 10 request | on-call |
| 7 | Đo RTO + postmortem | `python tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300` | `rto_verdict` khác null | SRE lead |

Bước 4 phải xong trước bước 5 — đây không phải chuyện tuỳ chọn thứ tự, mà là điều kiện bắt
buộc trong `dr/failover.py`. Region B chỉ trả 200 ở `/readyz` khi đủ cả bốn thứ: pool đã
`full`, hết thời gian warm-up, có model weights, và vector DB không rỗng. Nếu ai đó lỡ tay
đổi `edge/active_region` trước khi B thật sự sẵn sàng, khách sẽ nhận 503 từ *cả hai* region
cùng lúc, và RTO sẽ dài ra thay vì ngắn lại — đúng cái bẫy mà GUIDE cảnh báo ở §2.

Bước 6 nên chờ ít nhất vài giây sau cutover mới đo, vì `edge/proxy.py` vẫn cache region cũ
tối đa `EDGE_TTL_SECONDS` giây (mặc định 5s) trước khi đọc lại file `active_region`. Trong
lần drill của mình, khoảng cách giữa lúc DNS cutover thật sự xảy ra và lúc request đầu tiên
thành công là gần 3.7 giây — nếu đo golden signals ngay lập tức, rất có thể vẫn dính vài
request đi nhầm vào region A đã chết, không phải vì hệ thống chưa hồi phục mà vì cache chưa
hết hạn.

**Rollback (failover ngược):** chỉ trả traffic về Region A sau khi đủ ba điều kiện cùng lúc.
Thứ nhất, `python chaos/kill_region.py status` phải cho `a.ready=true` liên tục ít nhất 2
phút, không chỉ kiểm tra một lần — vì A có thể vừa sống lại rồi lại chết ngay sau đó, và nếu
rollback theo một lần check duy nhất thì rất dễ gây flapping. Thứ hai, phải chạy
`python state/replicate.py --every 30 --duration <N> --backend fs` để A bắt kịp phần dữ liệu
đã ghi trong lúc B đang phục vụ — nếu bỏ qua bước này, rollback sẽ khiến A "quay lại" với dữ
liệu cũ hơn cả B, tức là mất dữ liệu ngược. Thứ ba, cần có xác nhận bằng lời (Slack hoặc gọi
điện) của **SRE lead**, không phải chỉ chạy script rồi coi như xong.

SRE lead là người duy nhất có quyền kích hoạt rollback. Mình cố tình không để `dr/runbook.py
--auto` tự động trả traffic về A, vì full-auto hai chiều mà không có circuit breaker sẽ dẫn
tới hai region flap qua lại liên tục — đúng cảnh báo ở §4 Anti-Patterns, và đó là kiểu lỗi
khó phát hiện hơn nhiều so với một lần down đơn giản.
