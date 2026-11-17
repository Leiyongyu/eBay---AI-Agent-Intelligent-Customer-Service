"""知识条款导入：读取 JSONL（一行一个条款，字段与 knowledge_chunks 表一致）。

按 chunk_id 幂等写入；content_hash 未变化的条款跳过，沿用文件里的 review_state。
正式知识库走工作台 Excel 上传（knowledge/excel_import.py），本模块只供 KNOWLEDGE_SEED_FILE 与脚本使用。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from sqlalchemy.orm import Session

from ..models import KnowledgeChunk
from .tokenize import search_text

log = logging.getLogger(__name__)

FIELDS = ("clause_id", "document_id", "version", "title", "text", "store_id", "marketplace_id", "audience",
          "language", "topic", "priority", "effective_at", "expires_at", "review_state", "sku", "source",
          "content_hash")


def import_jsonl(db: Session, path: Path) -> dict:
    stats = {"inserted": 0, "updated": 0, "unchanged": 0}
    with path.open(encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            chunk_id = row.get("chunk_id") or row["clause_id"]
            values = {k: row.get(k) for k in FIELDS}
            values["search_text"] = search_text(values["text"] or "")
            existing = db.get(KnowledgeChunk, chunk_id)
            if existing is None:
                db.add(KnowledgeChunk(chunk_id=chunk_id, **values))
                stats["inserted"] += 1
            elif existing.content_hash != values["content_hash"] or existing.review_state != values["review_state"]:
                for k, v in values.items():
                    setattr(existing, k, v)
                stats["updated"] += 1
            else:
                stats["unchanged"] += 1
    db.flush()
    log.info("知识导入 %s：%s", path.name, stats)
    return stats
