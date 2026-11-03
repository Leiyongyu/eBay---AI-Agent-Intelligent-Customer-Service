"""AgentRunner.run(job_id)（第 17.2 节）：建立可信运行上下文，执行图，返回结果；从不返回“已发送”。"""
from __future__ import annotations

import time
from datetime import timedelta

from sqlalchemy import select

from ..config import get_settings
from ..db import session_scope, utcnow
from ..intents import RULE_VERSION
from ..models import AgentRun, Conversation, ReplyJob, Store
from ..prompts import prompt_version
from ..providers.chat import get_chat_provider
from ..providers.vision import get_vision_provider
from ..sources import get_source_for_store
from .graph import build_fixed_pipeline
from .nodes import PROMPT_NAMES
from .schemas import RuntimeContext
from .state import AgentDeps


class AgentRunner:
    def run(self, job_id: int) -> dict:
        settings = get_settings()
        with session_scope() as db:
            job = db.get(ReplyJob, job_id)
            conv = db.get(Conversation, job.conversation_id)
            store = db.get(Store, job.store_id)
            ctx = RuntimeContext(
                store_id=job.store_id,
                conversation_id=conv.id,
                buyer_key=conv.buyer_key,
                marketplace_id=conv.marketplace_id,
                item_id=conv.item_id,
                job_id=job.id,
                run_generation=job.run_generation,
            )
            run = db.scalar(select(AgentRun).where(AgentRun.job_id == job.id,
                                                   AgentRun.run_generation == job.run_generation))
            if run is None:
                run = AgentRun(job_id=job.id, run_generation=job.run_generation, thread_id=ctx.thread_id,
                               mode=settings.agent_mode)
                db.add(run)
            run.state = "RUNNING"
            run.started_at = utcnow()
            run.deadline_at = run.started_at + timedelta(seconds=settings.effective_agent_deadline_seconds)
            run.prompt_version = prompt_version(*PROMPT_NAMES)
            run.model_config_version = settings.model_config_version()
            run.rule_version = RULE_VERSION
            db.flush()

            deps = AgentDeps(
                run=ctx, db=db, settings=settings, source=get_source_for_store(job.store_id), llm=get_chat_provider(),
                store=store, run_id=run.id, deadline=time.monotonic() + settings.effective_agent_deadline_seconds,
                vision=get_vision_provider(),
            )
            try:
                final = build_fixed_pipeline().invoke({}, context=deps)
            except Exception as e:
                db.rollback()
                run = db.get(AgentRun, run.id)
                if run is not None:
                    run.state, run.error, run.finished_at = "ERROR", f"{type(e).__name__}: {e}"[:2000], utcnow()
                    db.commit()
                raise
            run.state = "DONE"
            run.finished_at = utcnow()
            result = final.get("result", {})
            return {"job_id": job.id, "run_id": run.id, "decision": result.get("decision"),
                    "reasons": result.get("reasons", []), "has_draft": result.get("has_draft", False)}
