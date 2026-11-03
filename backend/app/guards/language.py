"""回复语言：以买家本次（最新一条）来信的语言为准，不跟着历史消息或知识条款的语言走。

目前只对中文做确定判断（汉字占字母类字符 30% 以上）；其他语言交给模型按最新一条来信判断。
"""
from __future__ import annotations


def mostly_chinese(text: str) -> bool:
    letters = [ch for ch in text or "" if ch.isalpha()]
    return bool(letters) and sum("一" <= ch <= "鿿" for ch in letters) / len(letters) > 0.3


def reply_language_hint(latest_buyer_text: str) -> str:
    """填进提示词 {reply_language} 的说明。"""
    if mostly_chinese(latest_buyer_text):
        return "中文（买家最新一条来信是中文；即使之前的聊天记录或知识条款是其他语言，也用中文回复，language 填 zh）"
    return "买家最新一条来信所用的语言（以 <current_task> 里最后一条为准，不看之前聊天记录和知识条款的语言）"


def language_mismatch(latest_buyer_text: str, reply_text: str) -> bool:
    """买家最新一条是中文而回复不是中文，或反过来：回复语言答错了。"""
    if not latest_buyer_text.strip() or not reply_text.strip():
        return False
    return mostly_chinese(latest_buyer_text) != mostly_chinese(reply_text)
