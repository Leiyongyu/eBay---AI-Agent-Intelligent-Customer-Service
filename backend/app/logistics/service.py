"""按订单查物流：eBay 发货记录 + 谷仓出库单拿物流号，17TRACK 查轨迹。

省额度的规则：
- 只在“查包裹”工具被调用（买家问物流）时才注册，不批量注册订单；
- 同一个物流号只注册一次（tracking_numbers.number 唯一），轨迹在缓存时长内直接复用；
- 每天新注册数量有上限（TRACK17_REGISTER_DAILY_LIMIT），超了只返回 eBay/谷仓的基本信息；
- 承运商名称能精确对上 17TRACK 运输商代码时明确传入，避免识别错白扣一单；
- 谷仓已回传妥投时间的包裹不注册（已签收，不用查轨迹）；
- 剩余额度低于 TRACK17_MIN_QUOTA 时不再注册新物流号（额度每小时查一次，已注册的照常免费查）。
没查成轨迹的包裹带上 tracking_unsupported（承运商不支持）/ tracking_limited（额度或当天上限），判断时不算断更。
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..db import utcnow
from ..models import TrackingNumber
from .goodcang import GoodcangClient, GoodcangError
from .track17 import ALREADY_REGISTERED, Track17Client, Track17Error, carrier_code, summarize

log = logging.getLogger(__name__)
NOT_YET = (None, "NotFound")  # 刚注册还没结果：只缓存 5 分钟
LIMIT_NOTE = "暂不查轨迹"
_quota: dict = {"remain": None, "at": 0.0}


def _quota_remain(client: Track17Client) -> int | None:
    """17TRACK 剩余额度（每小时查一次，查额度不扣费）；查不到返回 None（不拦）。"""
    if time.monotonic() - _quota["at"] > 3600:
        try:
            _quota["remain"] = int((client.quota() or {}).get("quota_remain"))
        except Exception:  # noqa: BLE001  查不到额度就不拦
            _quota["remain"] = None
        _quota["at"] = time.monotonic()
    return _quota["remain"]


def _clean(number: str | None) -> str:
    return "".join((number or "").split()).upper()


def _fresh(rec: TrackingNumber, s: Settings, now: datetime) -> bool:
    if not rec.fetched_at or not rec.track:
        return False
    ttl = timedelta(minutes=5) if rec.status in NOT_YET else timedelta(minutes=s.track17_cache_minutes)
    return now - rec.fetched_at < ttl


def _ship_date(value: str | None) -> str | None:
    """17TRACK 要 YYYY/MM/DD。"""
    return value[:10].replace("-", "/") if value and len(value) >= 10 else None


def _registered_today(db: Session, now: datetime) -> int:
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return db.scalar(select(func.count()).select_from(TrackingNumber).where(TrackingNumber.registered_at >= start)) or 0


def _store(rec: TrackingNumber, accepted: dict, now: datetime) -> None:
    t = summarize(accepted)
    rec.track, rec.status, rec.sub_status, rec.delivered_at = t, t["status"], t["sub_status"], t["delivered_at"]
    rec.carrier_code = rec.carrier_code or t["carrier_code"]
    rec.fetched_at = now


def refresh(db: Session, rec: TrackingNumber, *, client: Track17Client, s: Settings, ship_date: str | None = None,
            order_id: str | None = None, now: datetime | None = None, wait: bool = True) -> str | None:
    """取（必要时先注册）一个物流号的轨迹，写回缓存。返回说明文字（额度用完、注册被拒等），正常为 None。"""
    now = now or utcnow()
    item = {"number": rec.number, **({"carrier": rec.carrier_code} if rec.carrier_code else {})}
    if not rec.registered_at:
        if rec.register_error:
            return f"17TRACK 无法跟踪该物流号：{rec.register_error}"
        if _registered_today(db, now) >= s.track17_register_daily_limit:
            return f"今天新查询的物流号已达上限（{s.track17_register_daily_limit} 个），{LIMIT_NOTE}"
        remain = _quota_remain(client)
        if remain is not None and remain < s.track17_min_quota:
            return f"17TRACK 剩余额度 {remain} 单，低于 {s.track17_min_quota}，{LIMIT_NOTE}（请到 17TRACK 充值）"
        reg = dict(item, auto_detection=True, **({"order_no": order_id} if order_id else {}),
                   **({"ship_date": ship_date} if ship_date else {}))
        accepted, rejected = client.register([reg])
        if accepted:
            rec.registered_at, rec.carrier_code = now, accepted[0].get("carrier") or rec.carrier_code
        elif rejected and (rejected[0].get("error") or {}).get("code") == ALREADY_REGISTERED:
            rec.registered_at = now  # 之前在 17TRACK 注册过（如别的系统），直接取详情
        else:
            err = (rejected[0].get("error") or {}).get("message") if rejected else "注册失败"
            rec.register_error = str(err)[:300]
            db.flush()
            return f"17TRACK 无法跟踪该物流号：{rec.register_error}"
        db.flush()
        item = {"number": rec.number, **({"carrier": rec.carrier_code} if rec.carrier_code else {})}
    deadline = time.monotonic() + (s.track17_wait_seconds if wait else 0)
    while True:
        got = client.track_info([item]).get(rec.number)
        status = (((got or {}).get("track_info") or {}).get("latest_status") or {}).get("status")
        if got and status not in NOT_YET:
            _store(rec, got, now)
            break
        if time.monotonic() >= deadline:
            if got:
                _store(rec, got, now)
            db.flush()
            return "已提交 17TRACK 查询，轨迹稍后可用（运输商可能还没上网）"
        time.sleep(3)
    db.flush()
    return None


def lookup(db: Session, store_id: str, order_id: str, ebay_packages: list[dict], *, settings: Settings | None = None,
           goodcang: GoodcangClient | None = None, track: Track17Client | None = None) -> dict:
    """返回 {"packages": [...], "warehouse": [谷仓出库单], "notes": [...]}；不含收件人信息。"""
    s = settings or get_settings()
    now = utcnow()
    notes: list[str] = []
    whs: list[dict] = []
    if s.goodcang_ready:
        try:
            whs = (goodcang or GoodcangClient(s)).orders_by_reference(order_id)
        except GoodcangError as e:
            notes.append(f"谷仓查询失败：{e}")

    # 包裹：eBay 发货记录在前（没有物流号的也保留），谷仓补充 eBay 没有的物流号
    pkgs: list[dict] = [{**p, "tracking_number": _clean(p.get("tracking_number")) or None, "source": "ebay"}
                        for p in ebay_packages]
    for wh in whs:
        for raw in wh["tracking_numbers"]:
            n = _clean(raw)
            same = next((p for p in pkgs if p["tracking_number"] == n), None)
            if same:
                same["carrier"] = same.get("carrier") or wh.get("carrier_name")
                same["warehouse_order"] = wh.get("order_code")
            else:
                pkgs.append({"order_id": order_id, "package_id": wh.get("order_code"), "tracking_number": n,
                             "carrier": wh.get("carrier_name"), "event_time": wh.get("date_shipping"),
                             "source": "goodcang", "warehouse_order": wh.get("order_code")})

    tracked = [p for p in pkgs if p["tracking_number"]]
    use_17 = not s.track17_not_ready_reasons()
    if pkgs and not tracked:
        notes.append("有发货记录但没有物流号")
    elif tracked and not use_17:
        notes.append("未开启 17TRACK，只有承运商和物流号，没有沿途轨迹")
    if not use_17:
        return {"packages": pkgs, "warehouse": whs, "notes": notes}
    client = track or Track17Client(s)
    by_code = {w.get("order_code"): w for w in whs}
    for p in tracked:
        n = p["tracking_number"]
        rec = db.scalar(select(TrackingNumber).where(TrackingNumber.number == n))
        wh = by_code.get(p.get("warehouse_order"))
        if (rec is None or not rec.registered_at) and wh and wh.get("dscan_time"):
            p["tracking_note"] = "承运商已回传妥投时间，没有再查 17TRACK 轨迹（省额度）"
            continue
        if rec is None:
            rec = TrackingNumber(number=n, store_id=store_id, order_id=order_id, source=p["source"],
                                 carrier_name=p.get("carrier"), carrier_code=carrier_code(p.get("carrier")))
            db.add(rec)
            db.flush()
        if not _fresh(rec, s, now):
            try:
                note = refresh(db, rec, client=client, s=s, ship_date=_ship_date(p.get("event_time")),
                               order_id=order_id, now=now)
            except Track17Error as e:
                note = f"17TRACK 查询失败：{e}"
            if note:
                p["tracking_note"] = note
        if rec.register_error and not rec.registered_at:
            p["tracking_unsupported"] = True  # 17TRACK 不支持这个承运商：只能请买家到承运商官网查
        elif not rec.registered_at and LIMIT_NOTE in (p.get("tracking_note") or ""):
            p["tracking_limited"] = True
        p["tracking"] = rec.track
        p["tracking_fetched_at"] = rec.fetched_at.isoformat(timespec="seconds") + "Z" if rec.fetched_at else None
    return {"packages": pkgs, "warehouse": whs, "notes": notes}


def quota_status(settings: Settings | None = None) -> dict:
    """系统状态页用：17TRACK 是否就绪、剩余额度；谷仓是否已配置。"""
    s = settings or get_settings()
    out = {"goodcang_ready": s.goodcang_ready, "track17_reasons": s.track17_not_ready_reasons(), "quota": None}
    if not out["track17_reasons"]:
        try:
            q = Track17Client(s).quota()
            out["quota"] = {k: q.get(k) for k in ("quota_total", "quota_used", "quota_remain", "today_used")}
        except Track17Error as e:
            out["error"] = str(e)
    return out
