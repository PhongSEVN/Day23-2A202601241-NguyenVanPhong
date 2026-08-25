# RTO/RPO Evidence — Lab 23

Mỗi con số dưới đây mình đều trỏ về một dòng log thật (`đường/dẫn.jsonl:số_dòng`), không có
số nào là ước lượng bằng cảm giác.

## 1. Drill 1 — không có DR (baseline)

| Chỉ số | Giá trị | Cách đo | Evidence |
|---|---|---|---|
| t_outage | `2026-08-25T12:33:39` | chaos kill | `chaos/chaos-events.jsonl:1` |
| Request fail đầu tiên | `+0.0s` | dòng `ok:false` đầu tiên sau t_outage | `reports/drill-1-nodr.jsonl:43` |
| Request thành công sau đó | không có | không có dòng `ok:true` nào sau t_outage | `reports/measure-drill-1.json` |
| RTO | `NO_RECOVERY` | `tools/measure_rto.py` | `reports/measure-drill-1.json` |

Drill này không phải để "thành công" — mục đích là chứng minh nếu không có DR thì hệ thống
chết hẳn, không tự hồi phục. Region A bị giết lúc 12:33:39, gần như ngay lập tức (0.0s) request
đã fail, và tới hết 51 request trong lần chạy không có cái nào thành công lại. 9/51 request fail
— con số ít hơn tổng requests_failed lý thuyết một chút vì loadgen tự dừng theo `--duration 40`
trước khi kịp gửi hết.

## 2. Drill 2 — có DR

| Mốc | +giây từ t_outage | Cách đo | Evidence |
|---|---|---|---|
| t_outage (mốc 0) | 0 | `action:kill` | `chaos/chaos-events.jsonl:8` |
| User thấy lỗi đầu tiên | 0.0s | dòng `ok:false` đầu | `reports/drill-2-withdr.jsonl:27` |
| Health check phát hiện | 18.8s | `to:UNHEALTHY, region:a` | `reports/health-events.jsonl:6` |
| Snapshot restore xong | 23.06s | `step:2_restore_snapshot` | `reports/failover-events.jsonl:22` |
| Region phụ ready | 23.17s | `step:4_wait_ready` | `reports/failover-events.jsonl:24` |
| DNS cutover | 23.27s | `step:5_dns_cutover` | `reports/failover-events.jsonl:25` |
| **RTO đo được** | **27.0s** | dòng `ok:true` đầu sau lỗi | `reports/drill-2-withdr.jsonl:40` |

| Chỉ số | Đo được | Mục tiêu (slide §1) | Verdict |
|---|---|---|---|
| RTO — Inference API | `27.0s` | 300s (5 phút) | PASS |
| RPO — Vector DB | `10.0s` / `5` doc | 300s (5 phút) | PASS |

Điều thú vị nhất khi mình đọc lại log của lần chạy này là DNS cutover (23.27s) xảy ra
*trước* rất lâu so với con số "sàn phát hiện" lý thuyết `interval×threshold=15s` mà mình
tưởng sẽ là mốc quan trọng nhất. Hoá ra `dr/failover.py` được `dr/runbook.py` gọi không
phải nhờ `dr/health_checker.py` báo — mà nhờ chính runbook.py tự đi hỏi lại `/readyz`
của region A ba lần. Ba lần thăm dò đó, mỗi lần chờ tối đa 2 giây rồi ngủ 5 giây, cộng lại
mất gần 23 giây — dài hơn cả detect floor lý thuyết của health checker. Chi tiết đường đi
thật nằm ở phần 3 bên dưới. RPO 10.0 giây / 5 document là phần dữ liệu ghi vào region A sau
lần `state/replicate.py` cuối cùng nhưng chưa kịp có mặt ở region B — con số này phụ thuộc
đúng lúc chu kỳ replicate (30 giây) rơi vào đâu so với thời điểm restore, nên nó dao động
qua từng lần chạy chứ không phải một hằng số cố định. Toàn bộ số tổng hợp nằm ở
`reports/measure-drill-2.json`.

## 3. RTO của tôi gồm những gì (bắt buộc — đây là phần chấm điểm hiểu bài)

4 thành phần dưới đây mình chia thành các khoảng liên tiếp, không chồng lấn, tính từ
`t_outage` tới lúc request thành công đầu tiên. Cộng lại: `22.96 + 0.10 + 0.21 + 3.69 =
26.96s`, làm tròn ra đúng `27.0s` — chênh 0.04s là do `tools/measure_rto.py` làm tròn một
chữ số thập phân ở bước cuối, không phải số bị sai lệch.

| # | Thành phần | Giây | Đo từ → đến | Evidence |
|---|---|---|---|---|
| 1 | Health-check detection floor | **22.96s** | `t_outage` → `1_verify_target` | `chaos/chaos-events.jsonl:8` → `reports/failover-events.jsonl:21` |
| 2 | Snapshot restore | **0.10s** | `1_verify_target` → `2_restore_snapshot` | `reports/failover-events.jsonl:21` → `reports/failover-events.jsonl:22` |
| 3 | GPU pool warm-up | **0.21s** | `2_restore_snapshot` → `5_dns_cutover` | `reports/failover-events.jsonl:22` → `reports/failover-events.jsonl:25` |
| 4 | DNS/LB TTL cache | **3.69s** | `5_dns_cutover` → request thành công đầu tiên | `reports/failover-events.jsonl:25` → `reports/drill-2-withdr.jsonl:40` |

Mình đặt tên dòng 1 là "Health-check detection floor" theo đúng tên GUIDE đưa, nhưng phải
nói thẳng: con số `22.96s` đó **không phải** là `interval_s × threshold` của
`dr/health_checker.py` (số đó chỉ là `5×3=15.0s`, và daemon thực tế báo `UNHEALTHY` ở
`18.8s` — xem `reports/health-events.jsonl:6`). Số `22.96s` là thời gian để `dr/runbook.py`
tự xác nhận outage bằng vòng lặp probe riêng của nó (hàm `_confirm_outage`, 3 lần thử ×
(2s timeout + 5s sleep) ≈ 21 giây), rồi mới gọi `dr/failover.py`. Nói cách khác, trong kiến
trúc hiện tại, health_checker.py chạy song song và ghi log rất đẹp, nhưng nó **không phải**
thứ thật sự kích hoạt failover — cái thật sự gate cutover là vòng lặp tự probe của runbook.
Đây là phát hiện mình rút ra được từ chính log, không phải giả định trước khi chạy.

Dòng 2 và 3 gần như không đáng kể (0.1s và 0.21s) vì backend `fs` chỉ copy vài file nhỏ, và
`state/region-b/pool_state` đã từng được set `full` ở một lần chạy trước đó trong buổi lab
này — nên warm-up thật (`WARMUP_SECONDS=6s`) đã "dùng hết" từ trước, không rơi vào cửa sổ đo
của lần drill cuối này. Nếu chạy lại từ một region B hoàn toàn nguội, dòng 3 có thể lên tới
vài giây thay vì 0.21s.

Dòng 4 (3.69s) là khoảng cách giữa lúc DNS cutover thật sự xảy ra và lúc user mới nhận được
response đúng — bị chặn trên bởi `EDGE_TTL_SECONDS=5` trong `edge/proxy.py`, vì proxy chỉ
đọc lại file `edge/active_region` sau khi cache hết hạn, đúng như cách DNS cache thật hoạt
động.

**Tóm lại thành phần chiếm nhiều nhất trong RTO của mình không phải health-check floor lý
thuyết, mà là cách `dr/runbook.py` tự đi thăm dò lại thay vì tin vào daemon đang chạy song
song** — chiếm khoảng 85% tổng RTO. Đây cũng là điểm mình bàn kỹ hơn ở phần root cause của
`reports/postmortem.md`.
