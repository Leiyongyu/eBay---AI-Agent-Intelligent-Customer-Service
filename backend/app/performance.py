"""绩效统计：按客服汇总回复量、响应时长、AI 草稿采纳情况和其他处理动作。

口径（按本机日期闭区间 [start, end]，只统计当前账号负责的店铺）：
- 回复：发件箱里客服提交的消息（source = manual），按提交时间算；已发出 / 撤回 / 没发出分开数。
- 响应时长：买家来信生成回复任务 → 客服提交回复，只算关联了回复任务的回复；超过 24 小时算超时（eBay 要求 24 小时内回复）。
- AI 采纳：和这个任务里最后一版 AI 草稿比——一字不改直接发 = 直接采纳，改过再发 = 修改后发送，没有 AI 草稿 = 人工撰写。
- AI 自动回复（source = auto）单独作为一行，便于和人工对比。
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from statistics import median

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import AuditEvent, Draft, Message, OutboundMessage, ReplyJob
from .sending.service import AUTO_ACTOR

LATE_SECONDS = 24 * 3600
ACTIONS = {"CONVERSATION_MARKED": "marks", "AFTERSALES_MARK": "aftersales", "HANDOFF_CHANGED": "handoffs",
           "LOGISTICS_TICKET_SUBMITTED": "tickets", "INVOICE_SENT": "invoices"}


def _local_day(dt: datetime) -> date:
    return dt.replace(tzinfo=timezone.utc).astimezone().date()


def _norm(text: str | None) -> str:
    return " ".join((text or "").split())


def _new() -> dict:
    return {"submitted": 0, "sent": 0, "recalled": 0, "failed": 0, "pending": 0, "conversations": set(),
            "response": [], "late": 0, "adopted": 0, "edited": 0, "own": 0,
            **{k: 0 for k in ACTIONS.values()}}


def _finish(actor: str, b: dict, is_ai: bool = False) -> dict:
    resp = b["response"]
    with_job = b["adopted"] + b["edited"] + b["own"]
    return {
        "actor": actor, "is_ai": is_ai,
        "submitted": b["submitted"], "sent": b["sent"], "recalled": b["recalled"], "failed": b["failed"],
        "pending": b["pending"], "conversations": len(b["conversations"]),
        "avg_response_seconds": int(sum(resp) / len(resp)) if resp else None,
        "median_response_seconds": int(median(resp)) if resp else None,
        "late": b["late"], "on_time_rate": round(1 - b["late"] / len(resp), 4) if resp else None,
        "adopted": b["adopted"], "edited": b["edited"], "own": b["own"],
        "adoption_rate": round(b["adopted"] / with_job, 4) if with_job else None,
        **{k: b[k] for k in ACTIONS.values()},
    }


def summary(db: Session, start: date, end: date, stores: list[str] | None) -> dict:
    """stores 为 None 表示全部店铺（管理员 / 全部店铺账号），否则只统计这些店铺。"""
    lo = datetime.combine(start - timedelta(days=1), datetime.min.time())  # UTC 前后放宽一天，再按本机日期精确过滤
    hi = datetime.combine(end + timedelta(days=2), datetime.min.time())

    def scoped(q, col):
        return q if stores is None else q.where(col.in_(stores))

    obs = [o for o in db.scalars(scoped(
        select(OutboundMessage).where(OutboundMessage.created_at >= lo, OutboundMessage.created_at < hi,
                                      OutboundMessage.source.in_(("manual", "auto"))),
        OutboundMessage.store_id)) if start <= _local_day(o.created_at) <= end]

    job_ids = {o.job_id for o in obs if o.job_id}
    job_created = dict(db.execute(select(ReplyJob.id, ReplyJob.created_at).where(ReplyJob.id.in_(job_ids))).all()) \
        if job_ids else {}
    agent_text: dict[int, str] = {}
    if job_ids:  # 每个任务最后一版 AI 草稿
        for d in db.scalars(select(Draft).where(Draft.job_id.in_(job_ids), Draft.author == "agent").order_by(Draft.id)):
            agent_text[d.job_id] = d.text

    people: dict[str, dict] = defaultdict(_new)
    ai = _new()
    days = {start + timedelta(days=i): {"manual": 0, "auto": 0} for i in range((end - start).days + 1)}
    for o in obs:
        is_auto = o.source == "auto"
        b = ai if is_auto else people[o.actor]
        b["submitted"] += 1
        if o.state == "SENT":
            b["sent"] += 1
        elif o.state == "RECALLED":
            b["recalled"] += 1
        elif o.state in ("FAILED", "UNCERTAIN"):
            b["failed"] += 1
        else:
            b["pending"] += 1
        if o.state != "RECALLED":
            b["conversations"].add(o.conversation_id)
            days[_local_day(o.created_at)]["auto" if is_auto else "manual"] += 1
        if o.job_id and o.state != "RECALLED":
            created = job_created.get(o.job_id)
            if created:
                secs = max(0, int((o.created_at - created).total_seconds()))
                b["response"].append(secs)
                b["late"] += secs > LATE_SECONDS
            draft = agent_text.get(o.job_id)
            if draft is None:
                b["own"] += 1
            elif _norm(draft) == _norm(o.body):
                b["adopted"] += 1
            else:
                b["edited"] += 1

    events = scoped(select(AuditEvent.actor, AuditEvent.action, AuditEvent.created_at)
                    .where(AuditEvent.created_at >= lo, AuditEvent.created_at < hi,
                           AuditEvent.action.in_(tuple(ACTIONS))), AuditEvent.store_id)
    for actor, action, at in db.execute(events):
        if start <= _local_day(at) <= end and actor and actor != AUTO_ACTOR and not actor.startswith("AI"):
            people[actor][ACTIONS[action]] += 1

    buyer_msgs = sum(1 for (at,) in db.execute(scoped(
        select(Message.sent_at).where(Message.sender_role == "BUYER", Message.direction == "IN",
                                      Message.sent_at >= lo, Message.sent_at < hi), Message.store_id))
        if start <= _local_day(at) <= end)

    rows = sorted((_finish(a, b) for a, b in people.items()), key=lambda r: (-r["submitted"], r["actor"]))
    ai_row = _finish(AUTO_ACTOR, ai, is_ai=True)
    team = _new()
    for b in people.values():
        for k, v in b.items():
            if isinstance(v, set):
                team[k] |= v
            else:
                team[k] += v
    team_row = _finish("全部客服", team)
    replied = team_row["submitted"] - team_row["recalled"] + ai_row["submitted"] - ai_row["recalled"]
    return {
        "start": start.isoformat(), "end": end.isoformat(),
        "totals": {
            "buyer_messages": buyer_msgs,
            "replies": replied,
            "manual": team_row["submitted"] - team_row["recalled"],
            "auto": ai_row["submitted"] - ai_row["recalled"],
            "auto_share": round((ai_row["submitted"] - ai_row["recalled"]) / replied, 4) if replied else None,
            "avg_response_seconds": team_row["avg_response_seconds"],
            "on_time_rate": team_row["on_time_rate"],
            "adoption_rate": team_row["adoption_rate"],
            "staff": len(rows),
        },
        "team": team_row,
        "people": rows,
        "ai": ai_row,
        "days": [{"date": d.isoformat(), **v} for d, v in days.items()],
    }

