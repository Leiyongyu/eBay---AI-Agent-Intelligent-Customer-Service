"""全文检索分词（第 15.4 节）：应用侧分词，数据库只做 'simple' 配置的全文匹配。

中文用 jieba 搜索引擎模式切词，英文/德文按字母数字切词并转小写；不做词干还原，
德语复合词靠向量检索补足。jieba 的词典缓存写到项目 data/jieba，不写系统临时目录（C 盘）。
"""
from __future__ import annotations

import logging
import re
import threading

from ..config import DATA_DIR

_WORD = re.compile(r"[0-9a-zäöüß]+(?:[-./][0-9a-zäöüß]+)*|[一-鿿]+", re.I)
_CJK = re.compile(r"[一-鿿]")
# 单个字母/数字和常见虚词、客套话对检索没有区分度（买家多用德语、英语来信）
_STOP = set("""
的 了 是 在 和 与 或 吗 呢 吧 啊 我 你 他 她 它 我们 您 请 谢谢 一下 这个 那个 什么 怎么 可以 能 会 要 有 没有 就 也 都 还
the a an and or to of in on for is are be it my your i you we do does did can could will would should this that these
those with at by from please hi hello dear thanks thank regards when what where how why who which if but not no yes
have has had was were been me our us they them their he she his her its there here as so just also any some all get
got am about into out up now then than too very much many more im ive dont
der die das und oder ist ich sie es ein eine einen einem einer eines mit für zu von den dem des nicht bitte auf an
hallo guten tag wann wird werden wurde bis können kann könnten möchte möchten würde mir mich mein meine meinen meinem
meiner ihr ihre ihnen ihren wir uns unser haben habe hat hatte sein bin sind war auch noch schon aber wenn dass wie was
wo warum so sehr nur dann als im am um aus bei nach vor über durch ohne kein keine diese dieser dieses diesen hier da ja
nein danke vielen dank gruß grüße freundlichen mfg zum zur man doch mal gibt geht ob sowie
""".split())

_lock = threading.Lock()
_jieba = None


def _get_jieba():
    global _jieba
    if _jieba is None:
        with _lock:
            if _jieba is None:
                import jieba

                cache_dir = DATA_DIR / "jieba"
                cache_dir.mkdir(parents=True, exist_ok=True)
                jieba.setLogLevel(logging.WARNING)
                jieba.dt.tmp_dir = str(cache_dir)
                jieba.initialize()
                _jieba = jieba
    return _jieba


def tokens(text: str) -> list[str]:
    """切词并去重（保持首次出现顺序）。"""
    out: list[str] = []
    seen: set[str] = set()
    for piece in _WORD.findall((text or "").lower()):
        words = _get_jieba().lcut_for_search(piece) if _CJK.match(piece) else [piece]
        for w in words:
            w = w.strip()
            if not w or w in _STOP or w in seen:
                continue
            if len(w) == 1 and not _CJK.match(w):
                continue
            seen.add(w)
            out.append(w)
    return out


def search_text(text: str) -> str:
    return " ".join(tokens(text))
