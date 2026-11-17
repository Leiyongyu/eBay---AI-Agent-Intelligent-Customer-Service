"""提示词文件加载与版本（第 16.1、16.4 节）。版本号取文件内容哈希，草稿记录所用版本。"""
from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path

PROMPT_DIR = Path(__file__).resolve().parent


@lru_cache
def load_prompt(name: str) -> str:
    return (PROMPT_DIR / f"{name}.md").read_text(encoding="utf-8").strip()


def prompt_version(*names: str) -> str:
    h = hashlib.sha256()
    for n in names:
        h.update(load_prompt(n).encode("utf-8"))
    return f"pv-{h.hexdigest()[:10]}"
