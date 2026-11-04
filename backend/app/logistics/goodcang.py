"""谷仓 OMS 开放接口：按 eBay 订单号查出库单，拿跟踪号、尾程承运商、出库仓、出库/揽收/妥投时间；查件单、库存只读。
唯一的写操作是新增查件单：草稿只存在本系统，客服在工作台核对、修改后点“提交到谷仓”才调用（save_type=2 提交）。

认证与 JMH-Date-Platform 的 RuoYi 项目一致：请求头 app-token / app-key（只放 .env）。
查询按接口文档：POST /order/get_order_list，code_type=reference_no（参考号 = eBay 订单号）+ order_code，
传了单号时其他筛选条件会被忽略；可查待发货、已发货、异常、问题件、废弃的订单。
一个 eBay 订单可能对应多个出库单，一票多箱时每箱有各自的跟踪号（orderBoxInfo[].tracking_number）。
返回值只保留物流相关字段，收件人姓名、地址、电话、邮箱一律不取（美国站、欧洲站订单谷仓本来也以 *** 返回）。
时间字段为谷仓系统时间（东八区）。
"""
from __future__ import annotations

import threading
import time

import httpx

from ..config import Settings, get_settings

# 实测见到的状态码；其他状态码原样显示
STATUS_LABELS = {"W": "待发货（仓库处理中）", "D": "已发货"}
KEEP = ("order_code", "reference_no", "order_status", "shipping_method", "carrier_name", "tracking_no",
        "warehouse_code", "wp_code", "date_create", "date_shipping", "ascan_time", "dscan_time",
        "carrier_estimated_delivery_time", "estimated_arrival_date", "abnormal_problem_reason")
SAVE_DRAFT, SAVE_SUBMIT = 1, 2  # 新增查件单 save_type：1 保存（谷仓后台草稿）/ 2 提交

_gate = threading.Lock()
_next_at = 0.0


class GoodcangError(RuntimeError):
    pass


def _wait_slot(interval_ms: int) -> None:
    """谷仓按密钥限流：本进程内所有请求串行、相邻至少间隔 interval_ms。"""
    global _next_at
    with _gate:
        now = time.monotonic()
        wait = max(0.0, _next_at - now)
        _next_at = max(now, _next_at) + interval_ms / 1000
    if wait:
        time.sleep(wait)


def _slim(row: dict) -> dict:
    out = {k: row.get(k) for k in KEEP}
    out["status_label"] = STATUS_LABELS.get(row.get("order_status"), f"状态 {row.get('order_status')}")
    numbers = [row.get("tracking_no")] + [b.get("tracking_number") for b in row.get("orderBoxInfo") or []]
    out["tracking_numbers"] = list(dict.fromkeys(n.strip() for n in numbers if n and n.strip()))
    out["items"] = [{"sku": i.get("product_sku"), "quantity": i.get("quantity"), "item_id": i.get("item_id")}
                    for i in row.get("items") or []]
    return out


class GoodcangClient:
    def __init__(self, settings: Settings | None = None, http: httpx.Client | None = None):
        self.s = settings or get_settings()
        self.http = http or httpx.Client(timeout=30)

    def _post(self, path: str, body: dict) -> dict:
        if not self.s.goodcang_ready:
            raise GoodcangError("谷仓接口未配置（GOODCANG_APP_TOKEN / GOODCANG_APP_KEY）")
        url = self.s.goodcang_endpoint.rstrip("/") + path
        headers = {"Content-Type": "application/json;charset=UTF-8", "app-token": self.s.goodcang_app_token,
                   "app-key": self.s.goodcang_app_key}
        for attempt in range(3):
            _wait_slot(self.s.goodcang_min_request_interval_ms)
            try:
                resp = self.http.post(url, json=body, headers=headers)
            except httpx.HTTPError as e:
                raise GoodcangError(f"连接谷仓失败：{type(e).__name__}") from None
            if resp.status_code == 429 and attempt < 2:
                time.sleep(3 * (attempt + 1))
                continue
            if resp.status_code != 200:
                raise GoodcangError(f"谷仓接口返回 HTTP {resp.status_code}")
            try:
                return resp.json()
            except ValueError:
                raise GoodcangError("谷仓接口返回的不是 JSON") from None
        raise GoodcangError("谷仓接口限流，重试后仍失败")

    def orders_by_reference(self, reference_no: str) -> list[dict]:
        """按平台订单号（参考号）查出库单；查不到返回空列表。"""
        data = self._post("/order/get_order_list", {"page": 1, "pageSize": 20, "code_type": "reference_no",
                                                     "order_code": reference_no.strip()})
        if str(data.get("ask", "")).lower() != "success":
            raise GoodcangError(f"谷仓查询失败：{str(data.get('message') or '')[:200]}")
        rows = data.get("data") or []
        ref = reference_no.strip()
        # 只认参考号完全一致的（防止接口忽略单号时把别的订单当成这一单）
        return [_slim(r) for r in rows if (r.get("reference_no") or "").strip() == ref]

    # ------------------------------------------------------------ 查件单（只读）
    def ticket_types(self, sm_code: str | None = None) -> list[dict]:
        """某物流方式支持的查件类型 [{value, name}]；不传物流方式返回全部 13 种。"""
        data = self._post("/assistant/logistic_ticket_type_list", {"sm_code": sm_code} if sm_code else {})
        if data.get("code") != 0:
            raise GoodcangError(f"谷仓查件类型查询失败：{str(data.get('message') or '')[:200]}")
        return data.get("data") or []

    def ticket_list(self, time_start: str, time_end: str, page: int = 1, page_size: int = 200) -> tuple[list[dict], int]:
        """按创建时间（北京时间，跨度不超过 186 天）分页取查件单，含在谷仓后台手动建的。返回（本页, 总数）。"""
        data = self._post("/assistant/logistic_ticket_list", {
            "code_type": "io_code", "page": page, "page_size": page_size,
            "time_type": "add_time", "time_start": time_start, "time_end": time_end})
        if data.get("code") != 0:
            raise GoodcangError(f"谷仓查件单查询失败：{str(data.get('message') or '')[:200]}")
        body = data.get("data") or {}
        return body.get("list") or [], int(body.get("total") or 0)

    # ------------------------------------------------------------ 写接口：新增查件单（客服在工作台点提交时才调用）
    def create_ticket(self, *, order_code: str, reference_no: str, tracking_no: str, it_type_id: int,
                      io_desc: str, emails: list[str], save_type: int = SAVE_SUBMIT) -> str:
        """新增查件单并返回查件单号。save_type=2 提交给谷仓客服处理；1 只在谷仓后台保存草稿。"""
        desc = io_desc.strip()[:300]
        if len(desc) < 5:
            raise GoodcangError("查件需求至少 5 个字")
        if not emails:
            raise GoodcangError("没有配置查件单通知邮箱（LOGISTICS_TICKET_NOTIFY_EMAILS）")
        data = self._post("/assistant/create_logistic_ticket", {
            "save_type": save_type, "order_code": order_code, "reference_no": reference_no,
            "tracking_no": tracking_no, "it_type_id": int(it_type_id), "io_desc": desc,
            "customer_emails_list": emails[:10], "attachment_id_list": []})
        if data.get("code") != 0:
            raise GoodcangError(f"谷仓建查件单失败：{str(data.get('message') or '')[:200]}")
        io_code = ((data.get("data") or {}).get("io_code") or "").strip()
        if not io_code:
            raise GoodcangError("谷仓建查件单没有返回查件单号")
        return io_code

    # ------------------------------------------------------------ 库存（只读）
    def inventory(self, skus: list[str]) -> list[dict]:
        """区域仓维度库存（V1 接口，ask=Success）。SKU 要用谷仓商品 SKU（JMH- 开头，见 followup.warehouse_sku）。"""
        skus = list(dict.fromkeys(s for s in skus if s))[:200]
        if not skus:
            return []
        out, page = [], 1
        while True:
            data = self._post("/inventory/get_product_inventory", {"page": page, "pageSize": 200, "product_sku_arr": skus})
            if str(data.get("ask", "")).lower() != "success":
                raise GoodcangError(f"谷仓库存查询失败：{str(data.get('message') or '')[:200]}")
            rows = data.get("data") or []
            out += [{"sku": r.get("product_sku"), "warehouse_code": r.get("warehouse_code"),
                     "warehouse_desc": r.get("warehouse_desc"), "sellable": _int(r.get("sellable")),
                     "reserved": _int(r.get("reserved")), "pending": _int(r.get("pending")),
                     "onway": _int(r.get("total_onway")), "freeze": r.get("product_freeze_status_text")} for r in rows]
            if not rows or page * 200 >= int(data.get("count") or 0):
                return out
            page += 1


def _int(v) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0
