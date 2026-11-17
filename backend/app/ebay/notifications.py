"""eBay 实时推送（Commerce Notification API，主题 NEW_MESSAGE）：买家发消息后 eBay 推到 EBAY_NOTIFY_ENDPOINT。

做法：推送只用来“触发”，收到后立刻拉取对应店铺（与定时拉取同一套入库代码），正文、图片、已读、
30 天保留、AI 自动回复全部沿用；定时拉取保留作兜底（电脑关机、隧道断开期间漏掉的推送）。

- 地址校验：eBay 建目的地时 GET ?challenge_code=…，回 sha256(challengeCode + verificationToken + endpoint)。
- 验签：X-EBAY-SIGNATURE 是 Base64 的 JSON {alg, kid, signature, digest}；按 kid 取 eBay 公钥（缓存 1 小时），
  ECDSA + SHA1 校验消息体（eBay 官方 SDK 对解析后的 JSON 紧凑序列化后校验；这里先试原始字节再试紧凑序列化）。
  不通过返回 412，不入库。
- 去重：按 notificationId；店铺识别：推送里出现的 eBay 用户名对应店铺账号，或会话 ID 已在库里；都识别不出就拉全部店铺。
- 订阅：一个目的地（全店共用）+ 每个店铺用自己的秘钥订阅一次 NEW_MESSAGE。只允许下面列出的几种 Notification API 调用。
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import threading
import time
from typing import Any

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from sqlalchemy import func, select

from ..config import get_settings
from ..db import session_scope, utcnow
from ..models import AuditEvent, Conversation, EbayNotification, EbayPushSubscription, Store
from .auth import MESSAGE_SCOPE, SCOPE_PREFIX, EbayAuthError, for_store

log = logging.getLogger(__name__)

TOPIC = "NEW_MESSAGE"
SUBSCRIPTION_SCOPE = SCOPE_PREFIX + "commerce.notification.subscription"
# 订阅 NEW_MESSAGE 的店铺令牌要同时带主题本身要求的 commerce.message，只带订阅权限会被拒（403 195011，2026-10-27 实测）
SUBSCRIBE_TOKEN_SCOPE = MESSAGE_SCOPE + " " + SUBSCRIPTION_SCOPE
DESTINATION_NAME = "ebay-cs-agent"
KEY_TTL_SECONDS = 3600
_USER_KEYS = ("username", "user_name", "userid", "user_id", "sender", "recipient", "seller", "buyer")


class SignatureError(Exception):
    pass


# ---------------------------------------------------------------- 地址校验与验签
def challenge_response(challenge_code: str) -> str:
    s = get_settings()
    if not (s.ebay_verification_token and s.ebay_notify_endpoint):
        raise RuntimeError("EBAY_VERIFICATION_TOKEN / EBAY_NOTIFY_ENDPOINT 未配置")
    return hashlib.sha256((challenge_code + s.ebay_verification_token + s.ebay_notify_endpoint).encode()).hexdigest()


_keys: dict[str, tuple[str, float]] = {}
_keys_lock = threading.Lock()


def _fetch_public_key(kid: str) -> str:
    from .browse_api import app_token_manager

    s = get_settings()
    resp = httpx.get(f"{s.ebay_api_base}/commerce/notification/v1/public_key/{kid}", timeout=20,
                     headers={"Authorization": f"Bearer {app_token_manager.access_token()}"})
    if resp.status_code != 200:
        raise SignatureError(f"取 eBay 公钥失败：HTTP {resp.status_code}")
    return resp.json()["key"]


_key_fetcher = _fetch_public_key


def set_public_key_fetcher(fn) -> None:
    """测试用：注入取公钥的函数；传 None 恢复默认并清空缓存。"""
    global _key_fetcher
    _key_fetcher = fn or _fetch_public_key
    with _keys_lock:
        _keys.clear()


def _public_key(kid: str) -> str:
    with _keys_lock:
        hit = _keys.get(kid)
        if hit and hit[1] > time.monotonic():
            return hit[0]
    key = _key_fetcher(kid)
    with _keys_lock:
        _keys[kid] = (key, time.monotonic() + KEY_TTL_SECONDS)
    return key


def _pem(key: str) -> bytes:
    """eBay 返回的 PEM 可能没有换行：取出中间的 Base64 重新排版。"""
    body = key.replace("-----BEGIN PUBLIC KEY-----", "").replace("-----END PUBLIC KEY-----", "")
    body = "".join(body.split())
    lines = [body[i:i + 64] for i in range(0, len(body), 64)]
    return ("-----BEGIN PUBLIC KEY-----\n" + "\n".join(lines) + "\n-----END PUBLIC KEY-----\n").encode()


def verify_signature(raw: bytes, header: str | None, payload: Any) -> None:
    if not header:
        raise SignatureError("缺少 X-EBAY-SIGNATURE")
    try:
        sig = json.loads(base64.b64decode(header).decode("ascii"))
        kid, signature = sig["kid"], base64.b64decode(sig["signature"])
    except Exception as e:
        raise SignatureError(f"X-EBAY-SIGNATURE 格式不对：{type(e).__name__}") from e
    key = serialization.load_pem_public_key(_pem(_public_key(kid)))
    if not isinstance(key, ec.EllipticCurvePublicKey):
        raise SignatureError("eBay 公钥不是 ECDSA 密钥")
    compact = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
    for data in (raw, compact):
        try:
            key.verify(signature, data, ec.ECDSA(hashes.SHA1()))
            return
        except InvalidSignature:
            continue
    raise SignatureError("签名校验不通过")


# ---------------------------------------------------------------- 收件
def _redact(obj: Any) -> Any:
    """消息正文类字段不入库（以拉取为准），只留结构与标识。"""
    if isinstance(obj, dict):
        return {k: ("<已省略>" if any(w in k.lower() for w in ("body", "text", "content", "subject")) else _redact(v))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact(v) for v in obj[:20]]
    return obj


def _walk(obj: Any, path: str = ""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _walk(v, k.lower())
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v, path)
    elif isinstance(obj, (str, int)) and path:
        yield path, str(obj)


def identify(db, payload: dict) -> tuple[set[str], str | None]:
    """推送对应的店铺 ID 集合与 eBay 会话 ID。识别不出店铺时返回空集合（调用方拉全部店铺）。"""
    names: set[str] = set()
    conv_id = None
    for key, value in _walk(payload.get("notification") or payload):
        if "conversation" in key and key.endswith("id"):
            conv_id = conv_id or value
        elif any(k in key for k in _USER_KEYS):
            names.add(value.lower())
    stores: set[str] = set()
    if names:
        stores |= set(db.scalars(select(Store.id).where(func.lower(Store.ebay_user_id).in_(names))))
    if conv_id:
        stores |= set(db.scalars(select(Conversation.store_id).where(Conversation.ebay_conversation_id == conv_id)))
    return stores, conv_id


def receive(payload: dict) -> dict:
    """验签通过后的推送：去重、记录、排队拉取。返回 {duplicate, stores, topic}。"""
    meta = payload.get("metadata") or {}
    note = payload.get("notification") or {}
    topic = meta.get("topic")
    nid = str(note.get("notificationId") or hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest())
    # EBAY_SYNC_ENABLED=false（暂停拉取，如只用模拟来信测试）时只记录、不拉取；仍返回成功，免得 eBay 因连续失败停用推送地址
    paused = not get_settings().ebay_sync_enabled or not get_settings().ebay_push_enabled  # 关了推送同样只记录
    status = "IGNORED" if topic != TOPIC else ("PAUSED" if paused else "QUEUED")
    with session_scope() as db:
        if db.scalar(select(EbayNotification.id).where(EbayNotification.notification_id == nid)):
            return {"duplicate": True, "topic": topic}
        stores, conv_id = identify(db, payload)
        row = EbayNotification(notification_id=nid, topic=topic, ebay_conversation_id=conv_id,
                               store_id=next(iter(stores)) if len(stores) == 1 else None,
                               status=status, payload=_redact(payload))
        db.add(row)
    if status == "QUEUED":
        push_worker.trigger(stores or None)
    return {"duplicate": False, "topic": topic, "stores": sorted(stores)}


class PushSyncWorker:
    """收到推送后在后台立刻拉取对应店铺；短时间内的多条推送合并成一次拉取。"""

    def __init__(self):
        self._pending: set[str] = set()
        self._all = False
        self._lock = threading.Lock()
        self._event = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_result: dict | None = None
        self.sync_factory = None  # 测试注入：返回 EbayMessageSync 的函数

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="ebay-push-sync", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._event.set()

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def trigger(self, store_ids: set[str] | None) -> None:
        with self._lock:
            if store_ids:
                self._pending |= set(store_ids)
            else:
                self._all = True
        self._event.set()

    def _take(self) -> tuple[bool, set[str]]:
        with self._lock:
            out = (self._all, set(self._pending))
            self._all, self._pending = False, set()
            self._event.clear()
            return out

    def run_once(self) -> dict | None:
        everything, stores = self._take()
        if not everything and not stores:
            return None
        from .sync import EbayMessageSync

        started = utcnow()
        sync = self.sync_factory() if self.sync_factory else EbayMessageSync()
        try:
            result = sync.sync_stores(None if everything else stores)
        except Exception as e:  # 拉取异常不影响下一次推送
            log.exception("推送触发的拉取失败")
            result = {"ok": False, "error": f"{type(e).__name__}: {str(e)[:300]}"}
        self.last_result = result
        with session_scope() as db:
            q = select(EbayNotification).where(EbayNotification.status == "QUEUED",
                                               EbayNotification.received_at <= started)
            if not everything:
                q = q.where((EbayNotification.store_id.in_(stores)) | (EbayNotification.store_id.is_(None)))
            for row in db.scalars(q):
                failed = (result.get("stores") or {}).get(row.store_id or "", {})
                ok = result.get("ok") or (row.store_id and failed.get("ok"))
                row.status, row.processed_at = ("PROCESSED" if ok else "ERROR"), utcnow()
                row.error = None if ok else (result.get("error") or "部分店铺拉取失败")[:2000]
        new = sum(t.get("messages_new", 0) for st in (result.get("stores") or {}).values()
                  for t in st.get("types", {}).values())
        log.info("推送触发拉取：%s 个店铺，新消息 %s 条", len(result.get("stores") or {}), new)
        return result

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._event.wait(30)
            if self._stop.is_set():
                break
            time.sleep(1)  # 同一时刻连着到的几条推送合并成一次拉取
            self.run_once()


push_worker = PushSyncWorker()


# ---------------------------------------------------------------- 订阅管理
class NotificationApi:
    """只开放推送订阅需要的几种调用：目的地查/建/启用、订阅查/建/启用/测试。"""

    def __init__(self, client: httpx.Client | None = None):
        self.http = client or httpx.Client(timeout=30)
        self.base = f"{get_settings().ebay_api_base}/commerce/notification/v1"

    def _call(self, method: str, path: str, token: str, body: dict | None = None) -> httpx.Response:
        assert method in ("GET", "POST", "PUT") and path.split("/")[1].split("?")[0] in ("destination", "subscription")
        resp = self.http.request(method, self.base + path, json=body,
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        if resp.status_code >= 400:
            try:
                errs = resp.json().get("errors") or []
                msg = "；".join(f"{e.get('errorId')} {e.get('message')}" for e in errs[:2])
            except ValueError:
                msg = resp.text[:200]
            raise RuntimeError(f"{method} {path.split('?')[0]} → HTTP {resp.status_code} {msg}")
        return resp

    @staticmethod
    def _created_id(resp: httpx.Response) -> str | None:
        loc = resp.headers.get("Location") or resp.headers.get("location") or ""
        return loc.rstrip("/").rsplit("/", 1)[-1] or None

    def ensure_destination(self, app_token: str, endpoint: str, verification_token: str) -> tuple[str, str]:
        """返回 (目的地 ID, 动作)：已有同地址的目的地就复用（停用的会启用），没有就新建（eBay 会马上来做地址校验）。"""
        found = self._call("GET", "/destination?limit=100", app_token).json().get("destinations") or []
        for d in found:
            if (d.get("deliveryConfig") or {}).get("endpoint") == endpoint:
                if d.get("status") != "ENABLED":
                    self._call("PUT", f"/destination/{d['destinationId']}", app_token, {
                        "name": d.get("name") or DESTINATION_NAME, "status": "ENABLED",
                        "deliveryConfig": {"endpoint": endpoint, "verificationToken": verification_token}})
                    return d["destinationId"], "enabled"
                return d["destinationId"], "exists"
        resp = self._call("POST", "/destination", app_token, {
            "name": DESTINATION_NAME, "status": "ENABLED",
            "deliveryConfig": {"endpoint": endpoint, "verificationToken": verification_token}})
        return self._created_id(resp), "created"

    def ensure_subscription(self, user_token: str, destination_id: str) -> tuple[str, str, str]:
        """返回 (订阅 ID, 状态, 动作)。"""
        subs = self._call("GET", "/subscription?limit=100", user_token).json().get("subscriptions") or []
        for sub in subs:
            if sub.get("topicId") == TOPIC and sub.get("destinationId") == destination_id:
                if sub.get("status") != "ENABLED":
                    self._call("POST", f"/subscription/{sub['subscriptionId']}/enable", user_token)
                    return sub["subscriptionId"], "ENABLED", "enabled"
                return sub["subscriptionId"], "ENABLED", "exists"
        resp = self._call("POST", "/subscription", user_token, {
            "topicId": TOPIC, "status": "ENABLED", "destinationId": destination_id,
            "payload": {"format": "JSON", "schemaVersion": "1.0", "deliveryProtocol": "HTTPS"}})
        return self._created_id(resp), "ENABLED", "created"

    def test(self, user_token: str, subscription_id: str) -> None:
        self._call("POST", f"/subscription/{subscription_id}/test", user_token)


def push_not_ready_reasons() -> list[str]:
    s = get_settings()
    out = []
    if not s.ebay_notify_endpoint:
        out.append("EBAY_NOTIFY_ENDPOINT 未配置（公网 HTTPS 推送地址）")
    elif not s.ebay_notify_endpoint.startswith("https://"):
        out.append("EBAY_NOTIFY_ENDPOINT 必须是 https 地址")
    if not s.ebay_verification_token:
        out.append("EBAY_VERIFICATION_TOKEN 未配置")
    elif not (32 <= len(s.ebay_verification_token) <= 80):
        out.append("EBAY_VERIFICATION_TOKEN 长度须为 32–80 个字符")
    return out


def setup(actor: str, send_test: bool = False, api: NotificationApi | None = None,
          app_token: str | None = None, user_token=None) -> dict:
    """建/复用推送目的地，并为每个对接的店铺订阅 NEW_MESSAGE。一个店铺失败不影响其他店铺。"""
    from .browse_api import app_token_manager
    from .stores import ebay_store_targets

    reasons = push_not_ready_reasons()
    if reasons:
        return {"ok": False, "error": "；".join(reasons)}
    s = get_settings()
    api = api or NotificationApi()
    user_token = user_token or (lambda store_id: for_store(store_id, SUBSCRIBE_TOKEN_SCOPE).access_token())
    try:
        dest_id, dest_action = api.ensure_destination(app_token or app_token_manager.access_token(),
                                                      s.ebay_notify_endpoint, s.ebay_verification_token)
    except Exception as e:
        return {"ok": False, "error": f"推送目的地创建失败（eBay 会先访问推送地址做校验，请确认 main.py 和隧道都在运行）：{e}"}
    result: dict = {"ok": True, "destination_id": dest_id, "destination": dest_action, "stores": {}}
    for target in ebay_store_targets():
        row: dict = {"name": target.name}
        try:
            token = user_token(target.store_id)
            sub_id, status, action = api.ensure_subscription(token, dest_id)
            row.update(subscription_id=sub_id, status=status, action=action)
            if send_test and sub_id:
                api.test(token, sub_id)
                row["test"] = "requested"
        except (EbayAuthError, RuntimeError, httpx.HTTPError) as e:
            row.update(status="ERROR", error=str(e)[:500])
            result["ok"] = False
        result["stores"][target.store_id] = row
        with session_scope() as db:
            sub = db.get(EbayPushSubscription, target.store_id) or EbayPushSubscription(store_id=target.store_id)
            sub.topic, sub.destination_id = TOPIC, dest_id
            sub.subscription_id = row.get("subscription_id") or sub.subscription_id
            sub.status, sub.last_error, sub.updated_at = row["status"], row.get("error"), utcnow()
            if row.get("test"):
                sub.last_test_at = utcnow()
            db.merge(sub)
    result["subscribed"] = sum(1 for r in result["stores"].values() if r.get("status") == "ENABLED")
    with session_scope() as db:
        db.add(AuditEvent(actor=actor, action="EBAY_PUSH_SETUP", detail={
            "destination": dest_action, "subscribed": result["subscribed"], "stores": len(result["stores"]),
            "errors": {k: v.get("error") for k, v in result["stores"].items() if v.get("error")}, "test": send_test}))
    return result


def recheck_destination(delay: float = 20) -> None:
    """启动后检查推送目的地：程序停着期间推送一直失败，eBay 可能把目的地停用，这里重新启用。

    只在已经建过订阅时做（只调 1–2 次接口）；等几秒让隧道先连上，因为启用时 eBay 会再做一次地址校验。
    """
    def run():
        time.sleep(delay)
        try:
            from .browse_api import app_token_manager

            with session_scope() as db:
                if not db.scalar(select(EbayPushSubscription.destination_id)
                                 .where(EbayPushSubscription.destination_id.is_not(None)).limit(1)):
                    return
            s = get_settings()
            dest_id, action = NotificationApi().ensure_destination(app_token_manager.access_token(),
                                                                   s.ebay_notify_endpoint, s.ebay_verification_token)
            (log.warning if action != "exists" else log.info)("推送目的地检查：%s（%s）", action, dest_id)
        except Exception as e:
            log.warning("推送目的地检查失败：%s", e)

    threading.Thread(target=run, name="ebay-push-recheck", daemon=True).start()


def status(db) -> dict:
    s = get_settings()
    subs = db.scalars(select(EbayPushSubscription)).all()
    day_ago = utcnow().replace(microsecond=0)
    from datetime import timedelta

    day_ago -= timedelta(hours=24)
    last = db.scalar(select(EbayNotification).order_by(EbayNotification.id.desc()).limit(1))
    per_store = dict(db.execute(select(EbayNotification.store_id, func.max(EbayNotification.received_at))
                                .group_by(EbayNotification.store_id)).all())
    return {
        "endpoint": s.ebay_notify_endpoint,
        "paused": not s.ebay_sync_enabled,
        "tunnel": _tunnel_status(),
        "reasons": push_not_ready_reasons(),
        "worker_running": push_worker.running,
        "destination_id": next((x.destination_id for x in subs if x.destination_id), None),
        "subscriptions": {"total": len(subs), "enabled": sum(x.status == "ENABLED" for x in subs),
                          "error": sum(x.status == "ERROR" for x in subs)},
        "stores": [{"store_id": x.store_id, "status": x.status, "error": x.last_error,
                    "last_received_at": _iso(per_store.get(x.store_id)), "last_test_at": _iso(x.last_test_at)}
                   for x in subs],
        "received_24h": db.scalar(select(func.count(EbayNotification.id))
                                  .where(EbayNotification.received_at >= day_ago)) or 0,
        "last_received_at": _iso(last.received_at) if last else None,
        "last_status": last.status if last else None,
        "last_sync": push_worker.last_result and {k: push_worker.last_result.get(k) for k in ("ok", "error")},
    }


def _tunnel_status() -> dict:
    from ..tunnel import tunnel

    return tunnel.status()


def _iso(dt) -> str | None:
    return dt.isoformat(timespec="seconds") + "Z" if dt else None
