"""买家个人信息脱敏（PII）：姓名、地址、邮箱、电话在进模型、进日志 / 审计、进评测集之前换成占位符，
草稿回来再还原（第二批 B7）。

- 已知值优先：订单、收件信息里能拿到的全名、地址行、城市 + 邮编、电话、邮箱，原样（不区分大小写）替换；
- 再按规则兜底：邮箱、电话（7 位以上的数字串，允许 + 空格 - . 括号）、带门牌号的街道地址（Straße 12、12 Main Street 等）；
- 同一个值在一段对话里始终用同一个占位符（<NAME_1>、<ADDR_1>、<EMAIL_1>、<PHONE_1>），模型能看出“是同一个人 / 同一个地址”；
- 订单号、运单号、商品号、SKU、金额不脱敏（回复要用，也不是个人信息）；8 位以上的纯数字只在像电话的格式里才算电话。
用法：
    m = PiiMasker(known=known_pii(order_dict))
    safe = m.mask(text)            # 发给模型 / 写日志
    reply = m.restore(model_text)  # 模型回复里的占位符还原成原值
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

KINDS = ("NAME", "ADDR", "EMAIL", "PHONE")
EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
# 电话：+ 或 0 开头、7–15 位数字，中间可有空格 - . / 括号；排除订单号（12-34567-89012）这类固定格式
PHONE = re.compile(r"(?<![\w-])(?:\+|00)?\(?\d{1,4}\)?(?:[ .\-/]?\(?\d{2,5}\)?){2,5}(?![\w-])")
ORDER_LIKE = re.compile(r"^\d{2}-\d{5}-\d{5}$|^\d{12}-\d{10,16}$")
# 以日期开头的（日期、日期 + 时间如 “2026-11-14 17:03”）不是电话
DATE_LIKE = re.compile(r"^\d{4}[-./]\d{1,2}[-./]\d{1,2}(?!\d)|^\d{1,2}[-./]\d{1,2}[-./]\d{2,4}(?!\d)")
# 街道地址：门牌号在后（德、法、意、西常见）或在前（英美）
STREET = re.compile(
    r"\b(?:[A-ZÄÖÜ][\w\-äöüß.]+(?:\s[\w\-äöüß.]+){0,3}\s?(?:stra(?:ss|ß)e|str\.|weg|allee|platz|gasse|ring|damm|"
    r"via|viale|piazza|corso|calle|avenida|plaza|rue|avenue|boulevard|chemin|place)\s+\d{1,5}\s?[a-zA-Z]?)\b"
    r"|\b\d{1,5}\s?[a-zA-Z]?\s+(?:[A-Z][\w'\-]+\s){1,4}(?:street|st\.?|road|rd\.?|avenue|ave\.?|lane|ln\.?|drive|dr\.?|"
    r"close|way|court|ct\.?|place|crescent|terrace|boulevard|blvd\.?)\b",
    re.I,
)
PLACEHOLDER = re.compile(r"<(NAME|ADDR|EMAIL|PHONE)_(\d+)>")


@dataclass
class PiiMasker:
    """一段对话（一次起草）用一个实例：占位符编号在实例内稳定。"""
    known: dict[str, list[str]] = field(default_factory=dict)  # {"NAME": [...], "ADDR": [...], "EMAIL": [...], "PHONE": [...]}
    mapping: dict[str, str] = field(default_factory=dict)       # 占位符 → 原值
    _rev: dict[tuple[str, str], str] = field(default_factory=dict)

    def _token(self, kind: str, value: str) -> str:
        key = (kind, value.strip().lower())
        if key not in self._rev:
            n = sum(1 for k, _ in self._rev if k == kind) + 1
            ph = f"<{kind}_{n}>"
            self._rev[key], self.mapping[ph] = ph, value.strip()
        return self._rev[key]

    def mask(self, text: str | None) -> str:
        if not text:
            return text or ""
        out = text
        # 已知值：长的先换，免得“Hauptstraße 12”被“Hauptstraße”先换掉一半
        pairs = sorted(((k, v.strip()) for k in KINDS for v in self.known.get(k) or [] if v and len(v.strip()) >= 3),
                       key=lambda kv: -len(kv[1]))
        for kind, v in pairs:
            out = re.sub(re.escape(v), lambda m, k=kind: self._token(k, m.group(0)), out, flags=re.I)
        out = EMAIL.sub(lambda m: self._token("EMAIL", m.group(0)), out)
        out = STREET.sub(lambda m: self._token("ADDR", m.group(0)), out)

        def phone(m: re.Match) -> str:
            s = m.group(0)
            digits = re.sub(r"\D", "", s)
            if len(digits) < 7 or len(digits) > 15 or ORDER_LIKE.match(s.strip()) or DATE_LIKE.match(s.strip()):
                return s
            if not re.search(r"[ +()\-./]", s) and not s.startswith(("0", "+")):
                return s  # 一整串数字（商品号、运单号）不当电话
            return self._token("PHONE", s)
        out = PHONE.sub(phone, out)
        return out

    def mask_obj(self, obj):
        """递归脱敏 dict / list 里的字符串（工具结果、评测快照）。"""
        if isinstance(obj, str):
            return self.mask(obj)
        if isinstance(obj, list):
            return [self.mask_obj(x) for x in obj]
        if isinstance(obj, dict):
            return {k: self.mask_obj(v) for k, v in obj.items()}
        return obj

    def restore(self, text: str | None) -> str:
        """模型回复里的占位符换回原值；不认识的占位符原样留着（交给校验去拦）。"""
        if not text:
            return text or ""
        return PLACEHOLDER.sub(lambda m: self.mapping.get(m.group(0), m.group(0)), text)

    def leftover(self, text: str | None) -> list[str]:
        """还原之后还留着的占位符（模型编了一个不存在的编号）。"""
        return [m.group(0) for m in PLACEHOLDER.finditer(text or "")]


# 订单 / 收件信息里可能出现个人信息的字段名（Fulfillment getOrder、本系统订单快照、演示数据都按这些名字找）
NAME_KEYS = ("fullName", "full_name", "name", "buyer_name", "ship_to_name", "recipient", "contactName", "firstName", "lastName")
ADDR_KEYS = ("addressLine1", "addressLine2", "address", "address_line1", "address_line2", "street", "street1", "street2",
             "ship_to_address", "shipping_address")
EMAIL_KEYS = ("email", "buyer_email", "emailAddress")
PHONE_KEYS = ("phone", "phoneNumber", "primaryPhone", "buyer_phone")


def known_pii(*objs) -> dict[str, list[str]]:
    """从订单 / 收件信息（任意嵌套的 dict / list）里收集已知的个人信息值。"""
    out: dict[str, list[str]] = {k: [] for k in KINDS}

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if isinstance(v, str) and v.strip():
                    if k in NAME_KEYS:
                        out["NAME"].append(v)
                    elif k in ADDR_KEYS:
                        out["ADDR"].append(v)
                    elif k in EMAIL_KEYS:
                        out["EMAIL"].append(v)
                    elif k in PHONE_KEYS:
                        out["PHONE"].append(v)
                elif isinstance(v, dict) and k in ("phone", "primaryPhone") and v.get("phoneNumber"):
                    out["PHONE"].append(str(v["phoneNumber"]))
                else:
                    walk(v)
        elif isinstance(o, list):
            for x in o:
                walk(x)

    for o in objs:
        walk(o)
    return {k: list(dict.fromkeys(v)) for k, v in out.items()}
