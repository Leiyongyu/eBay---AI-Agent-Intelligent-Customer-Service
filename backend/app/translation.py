"""买家来信自动翻译成中文（AUTO_TRANSLATE_BUYER_MESSAGES）：工作台显示原文 + 中文译文。

- 新拉到的买家消息（含推送触发）入队翻译；打开会话时，还没翻译的买家消息也入队（每次最多 OPEN_LIMIT 条，新的先译）。
- 后台单线程逐条翻译，结果写进 messages.zh_translation；中文来信、空消息不翻译。译文只供理解，不作为证据。
- 每次翻译计入用量统计（用途 translate）。模型没配置时什么也不做。
"""
from __future__ import annotations

import logging
import queue
import threading
import time

from sqlalchemy import select

from . import usage as usage_log
from .config import get_settings
from .db import session_scope
from .guards.language import mostly_chinese
from .models import Message

log = logging.getLogger(__name__)
OPEN_LIMIT = 20
MAX_CHARS = 4000


def needs_translation(m: Message) -> bool:
    return (m.sender_role == "BUYER" and not m.zh_translation and bool((m.body or "").strip())
            and not mostly_chinese(m.body or ""))


class BuyerTranslator:
    def __init__(self):
        self._q: queue.Queue[int] = queue.Queue()
        self._queued: set[int] = set()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def enabled(self) -> bool:
        return get_settings().auto_translate_buyer_messages

    def start(self) -> bool:
        if not self.enabled:
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="buyer-translate", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        self._q.put(0)

    def pending(self, ids: list[int]) -> set[int]:
        with self._lock:
            return {i for i in ids if i in self._queued}

    def enqueue(self, ids: list[int]) -> None:
        if not self.enabled or not self._thread or not self._thread.is_alive():
            return
        with self._lock:
            for i in ids:
                if i and i not in self._queued:
                    self._queued.add(i)
                    self._q.put(i)

    def _loop(self) -> None:
        while not self._stop.is_set():
            mid = self._q.get()
            if not mid:
                continue
            try:
                self.translate_one(mid)
            except Exception:  # noqa: BLE001  单条失败不影响后面的
                log.exception("买家消息 %s 翻译失败", mid)
                time.sleep(2)
            finally:
                with self._lock:
                    self._queued.discard(mid)

    @staticmethod
    def translate_one(message_id: int, provider=None) -> bool:
        from .providers.chat import get_chat_provider

        llm = provider or get_chat_provider()
        if llm is None:
            return False
        with session_scope() as db:
            m = db.get(Message, message_id)
            if m is None or not needs_translation(m):
                return False
            text, store_id, conv_id = (m.body or "")[:MAX_CHARS], m.store_id, m.conversation_id
        name, model = usage_log.chat_model(get_settings())
        started = time.perf_counter()
        try:
            out, usage = llm.translate_with_usage(text, "中文")
        except Exception as e:  # noqa: BLE001
            with session_scope() as db:
                usage_log.record(db, "translate", provider=name, model=model, ok=False, error=type(e).__name__,
                                 duration_ms=int((time.perf_counter() - started) * 1000), store_id=store_id,
                                 conversation_id=conv_id)
            raise
        with session_scope() as db:
            usage_log.record(db, "translate", provider=name, model=model, usage=usage,
                             duration_ms=int((time.perf_counter() - started) * 1000), store_id=store_id,
                             conversation_id=conv_id)
            m = db.get(Message, message_id)
            if m is not None and not m.zh_translation and out.strip():
                m.zh_translation = out.strip()
        return True


def untranslated_ids(db, conversation_id: int, limit: int = OPEN_LIMIT) -> list[int]:
    rows = db.scalars(select(Message).where(Message.conversation_id == conversation_id, Message.sender_role == "BUYER",
                                            Message.zh_translation.is_(None))
                      .order_by(Message.conversation_seq.desc()).limit(limit * 2)).all()
    return [m.id for m in rows if needs_translation(m)][:limit]


translator = BuyerTranslator()
