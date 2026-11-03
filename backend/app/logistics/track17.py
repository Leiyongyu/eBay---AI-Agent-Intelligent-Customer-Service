"""17TRACK v2.4 客户端与轨迹整理。

计费（官方文档）：每个物流号注册时扣 1 单，之后自动跟踪不重复扣；获取详情不扣费；实时查询接口另扣费，这里不用。
频率限制 3 次/秒，一次最多 40 个单号。所有接口 POST，请求头 17token。
"""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

import httpx

from ..config import PROJECT_ROOT, Settings, get_settings

CARRIER_LIST_URL = "https://res.17track.net/asset/carrier/info/apicarrier.all.json"
CARRIER_CACHE = PROJECT_ROOT / "data" / "track17_carriers.json"
ALREADY_REGISTERED = -18019901
MAX_EVENTS = 100

STATUS_LABELS = {
    "NotFound": "查询不到", "InfoReceived": "运输商已收到下单信息，等待揽收", "InTransit": "运输途中",
    "Expired": "运输过久（长时间未投递成功）", "AvailableForPickup": "到达待取（需收件人自取）",
    "OutForDelivery": "派送途中", "DeliveryFailure": "投递失败", "Delivered": "已签收", "Exception": "可能异常",
}
SUB_STATUS_LABELS = {
    "NotFound_Other": "运输商没有返回信息", "NotFound_InvalidCode": "物流单号无效",
    "InfoReceived": "运输商已收到下单信息", "InTransit_Other": "运输中", "Expired_Other": "运输过久",
    "AvailableForPickup_Other": "到达待取", "OutForDelivery_Other": "派送途中", "DeliveryFailure_Other": "投递失败",
    "Delivered_Other": "已签收", "Exception_Other": "其他异常",
    "InTransit_PickedUp": "已揽收", "InTransit_Departure": "已离开起运地", "InTransit_Arrival": "已到达目的地国家",
    "InTransit_CustomsProcessing": "清关中", "InTransit_CustomsReleased": "清关完成",
    "InTransit_CustomsRequiringInformation": "清关需要补充资料",
    "DeliveryFailure_NoBody": "找不到收件人", "DeliveryFailure_Security": "安全/清关/费用原因投递失败",
    "DeliveryFailure_Rejected": "收件人拒收", "DeliveryFailure_InvalidAddress": "地址错误",
    "Exception_Returning": "退件中", "Exception_Returned": "退件已签收", "Exception_NoBody": "找不到收件人",
    "Exception_Security": "安全/清关/费用问题", "Exception_Damage": "货物损坏", "Exception_Rejected": "收件人拒收",
    "Exception_Delayed": "延误", "Exception_Lost": "丢失", "Exception_Destroyed": "已销毁", "Exception_Cancel": "物流订单取消",
}
# 需要人工处理的轨迹状态（风险闸门用）
RISKY_STATUS = {"Exception", "DeliveryFailure", "Expired"}

_gate = threading.Lock()
_next_at = 0.0
_carriers: dict[str, int] | None = None


class Track17Error(RuntimeError):
    pass


def _norm(name: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def carrier_code(name: str | None, http: httpx.Client | None = None) -> int | None:
    """承运商名称 → 17TRACK 运输商代码；只认名称完全一致的，对不上就交给 17TRACK 自动识别。"""
    global _carriers
    if not name:
        return None
    if _carriers is None:
        rows = None
        if CARRIER_CACHE.exists() and time.time() - CARRIER_CACHE.stat().st_mtime < 7 * 86400:
            rows = json.loads(CARRIER_CACHE.read_text(encoding="utf-8"))
        else:
            try:
                rows = (http or httpx.Client(timeout=30)).get(CARRIER_LIST_URL).json()
                CARRIER_CACHE.parent.mkdir(parents=True, exist_ok=True)
                CARRIER_CACHE.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
            except (httpx.HTTPError, ValueError):
                rows = json.loads(CARRIER_CACHE.read_text(encoding="utf-8")) if CARRIER_CACHE.exists() else []
        index: dict[str, int] = {}
        for r in rows:
            index.setdefault(_norm(r.get("_name")), r.get("key"))
        _carriers = index
    return _carriers.get(_norm(name))


class Track17Client:
    def __init__(self, settings: Settings | None = None, http: httpx.Client | None = None):
        self.s = settings or get_settings()
        self.http = http or httpx.Client(timeout=30)

    def _post(self, path: str, payload) -> dict:
        global _next_at
        if self.s.track17_not_ready_reasons():
            raise Track17Error("；".join(self.s.track17_not_ready_reasons()))
        for attempt in range(3):
            with _gate:  # 官方限 3 次/秒
                now = time.monotonic()
                wait = max(0.0, _next_at - now)
                _next_at = max(now, _next_at) + 0.4
            if wait:
                time.sleep(wait)
            try:
                resp = self.http.post(f"{self.s.track17_base_url.rstrip('/')}/{path}", json=payload,
                                      headers={"17token": self.s.track17_api_key, "Content-Type": "application/json"})
            except httpx.HTTPError as e:
                raise Track17Error(f"连接 17TRACK 失败：{type(e).__name__}") from None
            if resp.status_code == 429 and attempt < 2:
                time.sleep(1 + attempt)
                continue
            if resp.status_code == 401:
                raise Track17Error("17TRACK 拒绝访问：密钥错误、IP 不在白名单或账号被禁用")
            if resp.status_code != 200:
                raise Track17Error(f"17TRACK 返回 HTTP {resp.status_code}")
            body = resp.json()
            if body.get("code") != 0:
                raise Track17Error(f"17TRACK 错误码 {body.get('code')}")
            data = body.get("data") or {}
            errors = data.get("errors") if isinstance(data, dict) else None
            if errors:
                raise Track17Error(f"17TRACK：{errors[0].get('message')}")
            return data
        raise Track17Error("17TRACK 限流，重试后仍失败")

    def register(self, items: list[dict]) -> tuple[list[dict], list[dict]]:
        data = self._post("register", items)
        return data.get("accepted") or [], data.get("rejected") or []

    def track_info(self, items: list[dict]) -> dict[str, dict]:
        """物流号 → accepted 项（含 track_info）。未注册或暂无结果的不在里面。"""
        data = self._post("gettrackinfo", items)
        return {a.get("number"): a for a in data.get("accepted") or []}

    def quota(self) -> dict:
        return self._post("getquota", {})


def summarize(accepted: dict) -> dict:
    """把 17TRACK 的 track_info 整理成给模型和工作台用的固定格式（不含收发件人信息）。"""
    info = accepted.get("track_info") or {}
    latest = info.get("latest_status") or {}
    status, sub = latest.get("status"), latest.get("sub_status")
    metrics = info.get("time_metrics") or {}
    eta = metrics.get("estimated_delivery_date") or {}
    providers = (info.get("tracking") or {}).get("providers") or []
    events, carrier_names, tips = [], [], []
    for p in providers:
        prov = p.get("provider") or {}
        if prov.get("name"):
            carrier_names.append(prov["name"])
        if p.get("provider_tips"):
            tips.append(str(p["provider_tips"])[:300])
        for e in p.get("events") or []:
            events.append({"time": e.get("time_iso") or e.get("time_utc"), "time_utc": e.get("time_utc"),
                           "location": e.get("location"), "description": e.get("description"),
                           "sub_status": e.get("sub_status")})
    events.sort(key=lambda e: e["time_utc"] or e["time"] or "", reverse=True)
    delivered = next((e["time"] for e in events if e.get("sub_status") == "Delivered_Other"), None)
    le = info.get("latest_event") or {}
    return {
        "carrier": " / ".join(dict.fromkeys(carrier_names)) or None,
        "carrier_code": accepted.get("carrier"),
        "status": status,
        "status_label": STATUS_LABELS.get(status or "", status),
        "sub_status": sub,
        "sub_status_label": SUB_STATUS_LABELS.get(sub or ""),
        "latest_event": {"time": le.get("time_iso") or le.get("time_utc"), "time_utc": le.get("time_utc"),
                         "location": le.get("location"), "description": le.get("description")} if le else None,
        "events": events[:MAX_EVENTS],  # 完整轨迹（新到旧）
        "events_total": len(events),
        "delivered_at": delivered,
        "days_in_transit": metrics.get("days_of_transit"),
        "estimated_delivery": ({"from": eta.get("from"), "to": eta.get("to"), "source": eta.get("source")}
                               if eta.get("from") or eta.get("to") else None),
        "carrier_tips": tips or None,
    }
