"""BƯỚC 3c — SINH VIÊN VIẾT. Tự động hoá runbook §4 "Runbook: Region Chính Down".

7 bước trên slide, mỗi bước 1 dòng log có ts. Log này CHÍNH LÀ timeline của postmortem.
  1 xac_nhan_outage          — probe cả 2 region, đừng tin 1 lần fail (dùng nhiều lần
                              hoặc gọi health_checker.probe nếu đã viết xong 3a)
  2 thong_bao_incident       — ts của dòng này là mốc "operator biết tin", LUÔN LUÔN
                              SAU t_outage trong chaos-events (không thể trùng — operator
                              không thể biết ngay giây outage xảy ra). Ghi cả 2 ts vào
                              log để postmortem tính được "độ trễ thông báo".
  3 scale_gpu_pool           — gọi HÀM `failover.failover(...)` MỘT LẦN DUY NHẤT. Hàm
                              đó tự làm đủ 5 bước con (verify/restore/scale/wait/cutover)
                              và tự ghi log riêng vào reports/failover-events.jsonl.
  4 verify_state_replica     — KHÔNG gọi lại failover — chỉ ĐỌC kết quả (vector count +
                              weights ở region phụ) từ dict mà bước 3 trả về, để log vào
                              runbook-run.jsonl cho postmortem đọc 1 chỗ duy nhất.
  5 dns_cutover              — cũng chỉ đọc lại: kết quả cutover có ok hay không.
  6 verify_golden_signals    — 10 request thật vào region phụ: p95 latency + error rate
  7 post_incident            — elapsed_s + lệnh đo RTO

BÁN TỰ ĐỘNG, KHÔNG FULL-AUTO (§4: "failover đầu tiên nên là bán tự động — alert +
1-click confirm — tránh flapping gây failover 2 chiều liên tục"). Mặc định phải hỏi
người vận hành confirm; --auto chỉ dùng trong CI/khi chấm điểm.

Chạy:  python dr/runbook.py --primary a --target b --backend fs
"""
import argparse
import json
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from dr import failover as fo  # noqa: E402

LOG = pathlib.Path("reports/runbook-run.jsonl")
URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}


def step(n, name, **kw):
    """Ghi 1 dòng {ts, iso, step, name, ...} vào LOG."""
    LOG.parent.mkdir(parents=True, exist_ok=True)
    rec = {"ts": time.time(), "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
           "step": n, "name": name, **kw}
    with LOG.open("a") as f:
        f.write(json.dumps(rec) + "\n")
    print("RUNBOOK", json.dumps(rec))
    return rec


def confirm(auto: bool, msg: str) -> bool:
    """auto=True -> True; ngược lại hỏi y/N. Bán tự động có chủ đích, không full-auto."""
    if auto:
        return True
    ans = input(f"{msg} [y/N] ").strip().lower()
    return ans == "y"


def _confirm_outage(primary: str, tries: int = 3, interval: float = 5.0):
    """Xác nhận outage bằng nhiều lần probe, không tin 1 lần fail.

    interval mặc định khớp cadence của health_checker.py (5s) -- nếu confirm nhanh
    hơn health checker thật, cutover sẽ xảy ra TRƯỚC khi health check phát hiện,
    tức là số đo được là do tay người bấm nút, không phải do automation tái lập được.
    """
    from dr import health_checker as hc
    fails, reason = 0, None
    for _ in range(tries):
        ready, reason = hc.probe(primary, timeout=2.0)
        if not ready:
            fails += 1
        time.sleep(interval)
    return fails, reason


def _golden_signals(n: int = 10) -> dict:
    """10 request thật vào edge -> p95 latency + error rate."""
    lat, errors = [], 0
    for i in range(n):
        t0 = time.time()
        try:
            r = httpx.get("http://127.0.0.1:8080/v1/infer", timeout=5.0,
                          params={"q": f"golden-check-{i}"})
            ok = r.status_code == 200
        except Exception:
            ok = False
        lat.append((time.time() - t0) * 1000)
        if not ok:
            errors += 1
    lat.sort()
    p95 = round(lat[max(0, int(len(lat) * 0.95) - 1)], 1) if lat else None
    return {"sample_size": n, "p95_ms": p95, "error_rate": round(errors / n, 2) if n else None}


def run(primary: str, target: str, backend: str, auto: bool) -> dict:
    """7 bước của Runbook §4 "Region Chính Down"."""
    t_start = time.time()

    # 1_xac_nhan_outage
    fails, reason = _confirm_outage(primary)
    outage_confirmed = fails >= 2
    step(1, "xac_nhan_outage", primary=primary, tries=3, fails=fails,
         reason=reason, outage_confirmed=outage_confirmed)
    if not outage_confirmed:
        result = {"ok": False, "reason": "outage_khong_duoc_xac_nhan"}
        step(7, "post_incident", ok=False, elapsed_s=round(time.time() - t_start, 2),
             reason=result["reason"])
        return result

    # 2_thong_bao_incident — mốc "operator biết tin", luôn sau t_outage thật
    t_notice = time.time()
    step(2, "thong_bao_incident", primary=primary, t_operator_biet=t_notice,
         notice_delay_s=round(t_notice - t_start, 2))

    if not confirm(auto, f"Region {primary} outage da xac nhan. Failover sang {target}?"):
        result = {"ok": False, "reason": "operator_tu_choi"}
        step(7, "post_incident", ok=False, elapsed_s=round(time.time() - t_start, 2),
             reason=result["reason"])
        return result

    # 3_scale_gpu_pool — gọi failover.failover(...) đúng MỘT LẦN DUY NHẤT
    fo_result = fo.failover(target, backend, wait=60)
    step(3, "scale_gpu_pool", target=target, failover_ok=fo_result.get("ok"),
         elapsed_s=fo_result.get("elapsed_s"))

    # 4_verify_state_replica — chỉ ĐỌC kết quả bước 3, không gọi lại failover
    step(4, "verify_state_replica", target=target,
         target_state=fo_result.get("target_state"),
         rpo_seconds=fo_result.get("rpo_seconds"), docs_lost=fo_result.get("docs_lost"))

    # 5_dns_cutover — cũng chỉ đọc lại
    step(5, "dns_cutover", target=target, cutover_ok=fo_result.get("ok"))

    # 6_verify_golden_signals
    golden = _golden_signals(10) if fo_result.get("ok") else {"n": 0, "p95_ms": None,
                                                               "error_rate": None}
    step(6, "verify_golden_signals", **golden)

    # 7_post_incident
    elapsed_s = round(time.time() - t_start, 2)
    step(7, "post_incident", ok=fo_result.get("ok"), elapsed_s=elapsed_s,
         measure_cmd="python tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl "
                     "--target-rto 300")

    return {"ok": fo_result.get("ok"), "elapsed_s": elapsed_s,
            "failover": fo_result, "golden_signals": golden}


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--primary", default="a")
    p.add_argument("--target", default="b")
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--auto", action="store_true")
    a = p.parse_args()
    print(json.dumps(run(a.primary, a.target, a.backend, a.auto), indent=2))
