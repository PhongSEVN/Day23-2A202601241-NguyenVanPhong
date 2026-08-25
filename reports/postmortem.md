# Postmortem — DR Drill Lab 23

Viết theo tinh thần blameless của §4: câu hỏi mình tự đặt ra không phải "ai bấm sai" mà là
"hệ thống/process nào đang cho phép chuyện này xảy ra".

## 1. Timeline

| ISO time | Sự kiện | Evidence |
|---|---|---|
| 2026-08-25T13:44:53 | outage bắt đầu (chaos kill region a, netblock) | `chaos/chaos-events.jsonl:8` |
| 2026-08-25T13:44:53 | user đầu tiên bị ảnh hưởng (+0.0s) | `reports/drill-2-withdr.jsonl:27` |
| 2026-08-25T13:45:12 | health check alert (+18.8s) | `reports/health-events.jsonl:6` |
| 2026-08-25T13:45:16 | cutover xong (+23.3s — lần này chạy `--auto` vì đây là drill chấm điểm; mặc định runbook.py vẫn hỏi y/N) | `reports/failover-events.jsonl:25` |
| 2026-08-25T13:45:20 | resolved — request đầu tiên OK từ region phụ (+27.0s) | `reports/drill-2-withdr.jsonl:40` |

## 2. RTO/RPO đo được vs mục tiêu — gap ở bước nào?

RTO mục tiêu là 300 giây, đo được 27.0 giây, tức gap âm 273 giây — dư rất nhiều. RPO cũng
vậy, mục tiêu 300 giây nhưng đo được chỉ 10.0 giây, tương đương 5 document bị mất. Cả hai
đều PASS thoải mái, nhưng con số PASS dễ dàng này không có nghĩa là kiến trúc đã tối ưu —
nó chỉ có nghĩa là mục tiêu 300 giây đang khá rộng so với những gì lab mô phỏng.

Bước tốn nhiều giây nhất, chiếm gần như toàn bộ RTO, là đoạn `dr/runbook.py` tự xác nhận
outage trước khi gọi `dr/failover.py` — khoảng 22.96 giây trong tổng 27.0 giây, tức khoảng
85%. Lý do là hàm `_confirm_outage` trong runbook.py không đọc lại log của
`dr/health_checker.py` (daemon đang chạy song song, đã ghi `UNHEALTHY` từ giây 18.8), mà tự
mở kết nối probe riêng 3 lần, mỗi lần chờ tối đa 2 giây timeout rồi ngủ 5 giây trước khi thử
lại. Ba lần như vậy cộng lại đã gần 21 giây, cộng thêm thời gian gọi hàm và ghi log thì ra
đúng khoảng 23 giây — gần bằng cả detect floor lý thuyết (15 giây) cộng thêm phần dư ra do
kiến trúc không tận dụng lại kết quả đã có sẵn.

## 3. Root cause (5 whys)

Câu hỏi mình tự hỏi không phải "vì sao script chaos chạy ra như vậy" mà là: nếu đây là một
outage thật ngoài đời, bước nào trong runbook của mình sẽ là điểm nghẽn?

1. Vì sao RTO gần như hoàn toàn nằm ở giai đoạn "xác nhận outage" chứ không phải ở giai
   đoạn restore/cutover? — Vì `dr/runbook.py` không tin tưởng health_checker.py đang chạy
   nền, mà tự gọi lại `probe()` độc lập 3 lần trước khi dám gọi `failover()`.
2. Vì sao nó không tin health_checker mà tự probe lại? — Vì lúc thiết kế, hai process này
   chưa từng được nối với nhau: health_checker.py chỉ ghi ra file JSONL, không có ai đọc
   lại real-time cả, kể cả runbook.py.
3. Vì sao chưa có cơ chế đọc lại đó? — Vì file JSONL hiện tại là log một chiều, chưa có
   API hay hàng đợi để runbook.py subscribe theo thời gian thực; đơn giản là chưa ai viết
   phần đó.
4. Vì sao điều này quan trọng nếu là outage thật? — Trong lần chạy `--mock`, mỗi lần probe
   chỉ chờ tối đa 2 giây (timeout mình đặt). Một outage mạng thật có thể khiến kết nối treo
   lâu hơn nhiều tuỳ cấu hình hạ tầng, và vì runbook.py probe tuần tự 3 lần, RTO có thể
   phình to gấp nhiều lần so với con số 23 giây đo được ở đây — mà mình sẽ không biết trước
   được mức phình đó là bao nhiêu.
5. Root cause thật sự: kiến trúc hiện có hai nguồn "sự thật" về sức khoẻ hệ thống —
   health_checker.py daemon và runbook.py tự probe — không đồng bộ với nhau. Đây không
   phải lỗi một dòng code cụ thể, mà là một khoảng trống trong thiết kế: chưa ai quyết định
   ai mới là nguồn tin cậy duy nhất.

## 4. Action items

| # | Action | Owner | Deadline | Giảm RTO/RPO bao nhiêu giây |
|---|---|---|---|---|
| 1 | Sửa `dr/runbook.py` để đọc trực tiếp dòng `state_change` mới nhất trong `reports/health-events.jsonl` thay vì tự probe lại 3 lần | SRE lead | trước lần drill kế tiếp | giảm khoảng 20 giây (bớt gần hết phần 22.96s ở bước xác nhận outage) |
| 2 | Xây cơ chế chia sẻ trạng thái thật giữa health_checker.py và runbook.py (tối thiểu: tail file, tốt hơn thì dùng queue/pubsub) để chỉ còn một nguồn sự thật duy nhất | on-call platform team | trong sprint tới | không đo trực tiếp được bằng giây, nhưng loại bỏ rủi ro RTO phình to bất định khi timeout mạng thật khác với `--mock` |

Action item 1 là chỗ dễ làm và mang lại lợi ích rõ nhất — gần như cả phần RTO đang "lãng
phí" đều nằm ở đó. Action item 2 mang tính nền tảng hơn: nó không tự làm RTO nhanh hơn ngay,
nhưng nó đóng cái gap thật sự đứng sau vấn đề, tức là loại bỏ luôn khả năng hai process nói
hai chuyện khác nhau về cùng một region.

## 5. Ba câu hỏi bắt buộc trả lời

**1. `interval × threshold` của mình là bao nhiêu giây? Nó chiếm bao nhiêu % RTO?**

`5s × 3 = 15 giây` — đây là detect floor lý thuyết của `dr/health_checker.py`, chiếm khoảng
15/27.0 ≈ 55.6% RTO đo được nếu tính theo con số lý thuyết đó. Nhưng như đã nói ở phần 2,
con số thực tế chi phối RTO không phải 15 giây này mà là vòng lặp tự probe riêng của
runbook.py, chiếm tới khoảng 85%. Hai con số này khác nhau vì health checker và cơ chế
kích hoạt failover trong kiến trúc hiện tại là hai đường tách biệt, không phải một.

**2. Nếu hạ interval xuống 1s, RTO giảm mấy giây — và mình trả giá gì?**

Nếu chỉ hạ `interval` của health_checker.py, detect floor lý thuyết giảm còn `1×3=3
giây`, nhưng RTO thực tế của lần drill này gần như không đổi, vì health_checker vốn không
phải thứ kích hoạt failover — runbook.py mới là. Muốn RTO thật sự giảm, phải sửa
`_confirm_outage` trong runbook.py, đúng như action item 1. Còn nếu vẫn cứ hạ interval
xuống 1 giây cho health_checker, cái giá phải trả là hệ thống nhạy hơn với những đợt chậm
mạng ngắn hạn — dễ đánh dấu UNHEALTHY nhầm rồi kích hoạt cutover, rồi region chính lại hồi
phục bình thường ngay sau đó, dẫn tới hai region flap qua lại nếu không có cơ chế circuit
breaker hay cooldown giữa các lần cutover, đúng như cảnh báo ở §4 Anti-Patterns.

**3. Nếu outage kéo dài 6 giờ và region chính mất dữ liệu vĩnh viễn, `docs_lost` của mình
có nghĩa gì với khách hàng?**

Trong lần drill này `docs_lost=5`, ứng với RPO 10.0 giây. Nghe thì nhỏ, nhưng nếu 5
document đó thật sự là 5 hoá đơn hay 5 ticket cụ thể mà khách hàng vừa gửi ngay trước lúc
outage, thì với họ dữ liệu đó không phải "bị trễ" — nó biến mất hẳn, và không tự phục hồi
khi region A sống lại, vì hệ thống DR chỉ khôi phục từ snapshot cuối cùng đã đồng bộ sang B,
chứ không phải từ chính dữ liệu còn nằm trên A. Nếu outage kéo dài 6 tiếng và A mất dữ liệu
vĩnh viễn, `docs_lost` không còn là một con số kỹ thuật để báo cáo nội bộ nữa — nó là danh
sách cụ thể những khách hàng cần được chủ động liên hệ, chứ không thể chỉ gói gọn trong một
số trung bình rồi coi như xong.
