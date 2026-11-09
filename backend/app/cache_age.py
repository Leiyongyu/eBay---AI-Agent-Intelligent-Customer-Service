"""按请求决定“缓存多久以内的数据可以直接用”。

eBay 订单、刊登、包裹和谷仓出库单都缓存在进程内存里，默认时长由各自模块决定（eBay 10 分钟、谷仓 GOODCANG_CACHE_SECONDS）。
回复页右侧栏（相关信息、客户资料）只是给客服看，用 `with max_age(PANEL_CACHE_SECONDS)` 放宽到 1 小时，打开会话不用每次实时调接口；
点“刷新”时用 `max_age(0)` 强制重查并更新缓存。AI 起草回复走默认时长，不受影响（不会拿一小时前的发货状态回复买家）。
"""
from __future__ import annotations

import contextvars
from contextlib import contextmanager

_max_age: contextvars.ContextVar[float | None] = contextvars.ContextVar("cache_max_age", default=None)


def current(default: float) -> float:
    """这次请求允许的缓存时长（秒）：没有特别指定就用调用方的默认值。"""
    v = _max_age.get()
    return default if v is None else v


@contextmanager
def max_age(seconds: float):
    token = _max_age.set(max(0.0, float(seconds)))
    try:
        yield
    finally:
        _max_age.reset(token)
