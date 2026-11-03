"""物流状况判断（确定性规则，不交给模型）：只看“轨迹多少天没有更新”，再加查不到、异常、已签收几种情况。

- 没有跟踪号 / 17TRACK 查不到 → 查不到物流信息，转人工核实；
- 轨迹可能异常、投递失败、运输过久、退件 → 转人工；
- 已签收 → 正常（买家说没收到由风险闸门另判）；
- 其余按最近一条动态距今的天数：≥ LOGISTICS_RESHIP_DAYS 要人工判断补发，≥ LOGISTICS_STALL_DAYS 算断更转人工跟进。
谷仓的时间是东八区、没有时区标记，这里按 +08:00 解析。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ..config import Settings, get_settings
from .track17 import RISKY_STATUS

CN = timezone(timedelta(hours=8))
STAGE_LABELS = {
    "NO_TRACKING": "没有跟踪号", "NO_INFO": "查不到物流轨迹", "EXCEPTION": "物流异常", "DELIVERED": "已签收",
    "RESHIP_REVIEW": "轨迹长时间未更新，需判断补发", "STALLED": "轨迹多天未更新", "OK": "正常运输中", "UNKNOWN": "未开启轨迹查询",
}
# 需要人工的阶段 → 风险原因码
HUMAN_REASONS = {"NO_TRACKING": "TRACKING_NOT_FOUND", "NO_INFO": "TRACKING_NOT_FOUND", "EXCEPTION": "TRACKING_EXCEPTION",
                 "STALLED": "TRACKING_STALLED", "RESHIP_REVIEW": "TRACKING_RESHIP_REVIEW"}


def parse_time(value: str | None, naive_tz=timezone.utc) -> datetime | None:
    """ISO 时间（带或不带时区）或谷仓的 'YYYY-MM-DD HH:MM:SS'（东八区）→ UTC aware。"""
    if not value:
        return None
    v = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(v.replace(" ", "T", 1) if "T" not in v else v)
    except ValueError:
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=naive_tz)).astimezone(timezone.utc)


def assess(pkg: dict, *, tracking_enabled: bool, now: datetime | None = None,
           settings: Settings | None = None) -> dict:
    s = settings or get_settings()
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    t = pkg.get("tracking") or {}
    wh = pkg.get("warehouse") or {}
    last = None
    events = t.get("events") or []
    if events:
        last = parse_time(events[0].get("time_utc")) or parse_time(events[0].get("time"))
    if last is None:  # 没有轨迹节点时退回谷仓揽收 / 出库时间、eBay 发货时间
        last = (parse_time(wh.get("ascan_time"), CN) or parse_time(wh.get("date_shipping"), CN)
                or parse_time(pkg.get("event_time")))
    days = round((now - last).total_seconds() / 86400, 1) if last else None

    if not pkg.get("tracking_number"):
        stage = "NO_TRACKING"
    elif not tracking_enabled:
        stage = "UNKNOWN"
    elif not t or t.get("status") in (None, "NotFound"):
        stage = "NO_INFO"
    elif t.get("status") == "Delivered":
        stage = "DELIVERED"
    elif t.get("status") in RISKY_STATUS or (t.get("sub_status") or "").startswith("Exception"):
        stage = "EXCEPTION"
    elif days is not None and days >= s.logistics_reship_days:
        stage = "RESHIP_REVIEW"
    elif days is not None and days >= s.logistics_stall_days:
        stage = "STALLED"
    else:
        stage = "OK"
    return {"stage": stage, "stage_label": STAGE_LABELS[stage],
            "last_update": last.isoformat(timespec="minutes") if last else None,
            "days_without_update": days, "needs_human": stage in HUMAN_REASONS,
            "reason_code": HUMAN_REASONS.get(stage)}
