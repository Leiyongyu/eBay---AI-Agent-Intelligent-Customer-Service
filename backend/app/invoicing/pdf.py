"""发票 PDF（reportlab），两种版式：

- 普通发票（NORMAL）：照 eBay 后台下载的订单单据排版——店铺名、收货地址 / 卖家地址 / 买家注册地址三栏、
  “VAT Paid: OSS - …”（eBay 代收增值税用的 eBay 税号）、订单号与销售记录号、带税率列的商品表、
  “MwSt. (von eBay eingezogen)”合计和脚注；顶部店铺链接和店铺二维码，收货地址下买家电话和 eBay 转发邮箱，
  左下角卖家留言。不印 eBay 标志（eBay 的商标，这份单据是店铺开的）。语言按站点。
- 增值税发票（VAT）：照店铺的 Excel 发票模板（湘彦-DE-VAT 发票模板）——粗外框的表格，卖方公司与地址、
  邮编、卖方税号，发票号与开票日期，收票人与收货地址，商品明细，小计/运费/VAT/折扣/总价，销售渠道与订单号。

文字里有中文等 Arial / Times 没有的字时，自动换成宋体（Windows 自带）显示。
"""
from __future__ import annotations

import io
import os
import re
from functools import lru_cache
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from ..config import get_settings

FONT_DIR = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
CJK_RE = re.compile(r"[\u2E80-\u9FFF\uAC00-\uD7AF\uF900-\uFAFF\uFF00-\uFFEF]+")
MONTHS = {"de": "Jan Feb März Apr Mai Juni Juli Aug Sep Okt Nov Dez", "en": "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec",
          "fr": "janv. févr. mars avr. mai juin juil. août sept. oct. nov. déc.",
          "it": "gen feb mar apr mag giu lug ago set ott nov dic", "es": "ene feb mar abr may jun jul ago sept oct nov dic"}


# ------------------------------------------------------------------ 字体
def _register(name: str, path: str, **kw) -> bool:
    if not os.path.exists(path):
        return False
    pdfmetrics.registerFont(TTFont(name, path, **kw))
    return True


@lru_cache
def fonts() -> dict[str, str]:
    """sans/sans_bold：普通发票（Arial）；serif/serif_bold：增值税发票（Times New Roman）；cjk：中文回退（宋体）。"""
    custom = get_settings().invoice_pdf_font
    out = {"sans": "Helvetica", "sans_bold": "Helvetica-Bold", "serif": "Times-Roman", "serif_bold": "Times-Bold",
           "cjk": "Helvetica"}
    if custom and _register("InvCustom", custom):
        out.update(sans="InvCustom", sans_bold="InvCustom", serif="InvCustom", serif_bold="InvCustom")
    else:
        if _register("InvSans", os.path.join(FONT_DIR, "arial.ttf")):
            out["sans"] = "InvSans"
            out["sans_bold"] = "InvSansBold" if _register("InvSansBold", os.path.join(FONT_DIR, "arialbd.ttf")) else "InvSans"
        if _register("InvSerif", os.path.join(FONT_DIR, "times.ttf")):
            out["serif"] = "InvSerif"
            out["serif_bold"] = "InvSerifBold" if _register("InvSerifBold", os.path.join(FONT_DIR, "timesbd.ttf")) else "InvSerif"
    for fname, kw in (("simsun.ttc", {"subfontIndex": 0}), ("simhei.ttf", {}), ("msyh.ttc", {"subfontIndex": 0})):
        try:
            if _register("InvCJK", os.path.join(FONT_DIR, fname), **kw):
                out["cjk"] = "InvCJK"
                break
        except Exception:  # noqa: BLE001 — 某些字体格式 reportlab 读不了，换下一个
            continue
    return out


def _markup(text: str) -> str:
    """转义并把中文等字段换成中文字体。"""
    cjk = fonts()["cjk"]
    out, pos = [], 0
    for m in CJK_RE.finditer(text):
        out.append(escape(text[pos:m.start()]))
        out.append(f'<font name="{cjk}">{escape(m.group(0))}</font>')
        pos = m.end()
    out.append(escape(text[pos:]))
    return "".join(out).replace("\n", "<br/>")


def _p(text: str | None, style: ParagraphStyle) -> Paragraph:
    return Paragraph(_markup(text or ""), style)


def _money(v: str | None, lang: str, comma: bool) -> str:
    if v in (None, ""):
        return ""
    s = f"{float(v):,.2f}"
    return s.replace(",", "\x00").replace(".", ",").replace("\x00", ".") if comma else s


def _date(iso: str | None, lang: str, style: str) -> str:
    if not iso or len(iso) < 10:
        return iso or ""
    y, m, d = iso[:4], int(iso[5:7]), int(iso[8:10])
    if style == "long":  # 30. Okt 2026（eBay 单据）
        mon = MONTHS.get(lang, MONTHS["en"]).split()[m - 1]
        return f"{d}. {mon} {y}" if lang == "de" else f"{d} {mon} {y}"
    return f"{d:02d}.{m:02d}.{y}" if lang == "de" else f"{d:02d}/{m:02d}/{y}"


def _rate(r) -> str:
    return str(int(r)) if float(r).is_integer() else str(r)


def _qr(url: str, size: float):
    """店铺链接二维码（reportlab 自带，不用外部库）。"""
    from reportlab.graphics.barcode.qr import QrCodeWidget
    from reportlab.graphics.shapes import Drawing

    w = QrCodeWidget(url, barBorder=0)
    x0, y0, x1, y1 = w.getBounds()
    d = Drawing(size, size, transform=[size / (x1 - x0), 0, 0, size / (y1 - y0), 0, 0])
    d.add(w)
    d.hAlign = "CENTER"
    return d


def _service_label(code: str, L: dict) -> str:
    """配送服务代码 → 单据上的写法：eBay 单据把“其他快递”类显示成 Sonstige / Other；其余去掉站点前缀、下划线换空格。"""
    if re.search(r"other|sonstig|autre|altro|otro", code or "", re.I):
        return L["other_service"]
    return re.sub(r"^(UK|DE|FR|IT|ES|AT|US)_", "", code or "").replace("_", " ")


# ------------------------------------------------------------------ 普通发票：照 eBay 订单单据
NORMAL = {
    "de": {"title": "RECHNUNG", "ship_to": "Lieferadresse", "sender": "Absenderadresse", "reg": "Anmeldeadresse des Käufers",
           "user": "Nutzername", "order": "Bestellung", "order_date": "Bestelldatum", "record": "Verkaufsprotokoll Nr.",
           "cols": ("Artikel", "Stückzahl", "Artikelpreis", "MwSt.-Satz", "Artikel insgesamt"), "item_no": "Artikelnr.",
           "service": "Vom Käufer ausgewählter Versandservice", "subtotal": "Zwischensumme", "shipping": "Versand",
           "discount": "Rabatt", "vat": "MwSt. (von eBay eingezogen)", "tax": "Steuer (von eBay eingezogen)",
           "vat_incl": "enthaltene MwSt.", "total": "Gesamtbetrag", "qr": "Link zu Shop-QR-Code",
           "msg_from": "Eine Nachricht von {name}", "thanks": "Vielen Dank für Ihren Einkauf! Ich wünsche Ihnen viel Freude damit!",
           "other_service": "Sonstige",
           "note": "Gesamtbetrag einschl. der von eBay eingezogenen Mehrwertsteuer. eBay hat diese MwSt. vom Betrag "
                   "abgezogen, den der Käufer bezahlt hat, und führt sie an die zuständigen Steuerbehörden ab."},
    "en": {"title": "INVOICE", "ship_to": "Ship to", "sender": "Sender address", "reg": "Buyer's registered address",
           "user": "Username", "order": "Order", "order_date": "Order date", "record": "Sales record no.",
           "cols": ("Item", "Quantity", "Item price", "VAT rate", "Item total"), "item_no": "Item no.",
           "service": "Shipping service selected by buyer", "subtotal": "Subtotal", "shipping": "Shipping",
           "discount": "Discount", "vat": "VAT (collected by eBay)", "tax": "Tax (collected by eBay)",
           "vat_incl": "VAT included", "total": "Total", "qr": "Link to shop QR code",
           "msg_from": "A message from {name}", "thanks": "Thank you for your purchase! I hope you enjoy it!",
           "other_service": "Other",
           "note": "Total including the VAT collected by eBay. eBay deducted this VAT from the amount paid by the buyer "
                   "and remits it to the relevant tax authorities."},
    "fr": {"title": "FACTURE", "ship_to": "Adresse de livraison", "sender": "Adresse de l'expéditeur",
           "reg": "Adresse d'inscription de l'acheteur", "user": "Pseudo", "order": "Commande", "order_date": "Date de commande",
           "record": "N° d'enregistrement de vente", "cols": ("Objet", "Quantité", "Prix", "Taux de TVA", "Total objet"),
           "item_no": "N° d'objet", "service": "Service de livraison choisi par l'acheteur", "subtotal": "Sous-total",
           "shipping": "Livraison", "discount": "Remise", "vat": "TVA (perçue par eBay)", "tax": "Taxe (perçue par eBay)",
           "vat_incl": "dont TVA", "total": "Total", "qr": "Lien vers le QR code de la boutique",
           "msg_from": "Un message de {name}", "thanks": "Merci pour votre achat ! Profitez-en bien !",
           "other_service": "Autre",
           "note": "Total TVA perçue par eBay incluse. eBay a déduit cette TVA du montant payé par l'acheteur et la reverse "
                   "aux autorités fiscales compétentes."},
    "it": {"title": "FATTURA", "ship_to": "Indirizzo di spedizione", "sender": "Indirizzo del mittente",
           "reg": "Indirizzo di registrazione dell'acquirente", "user": "Nome utente", "order": "Ordine",
           "order_date": "Data dell'ordine", "record": "N. registrazione vendita",
           "cols": ("Oggetto", "Quantità", "Prezzo", "Aliquota IVA", "Totale oggetto"), "item_no": "N. oggetto",
           "service": "Servizio di spedizione scelto dall'acquirente", "subtotal": "Subtotale", "shipping": "Spedizione",
           "discount": "Sconto", "vat": "IVA (riscossa da eBay)", "tax": "Imposta (riscossa da eBay)",
           "vat_incl": "di cui IVA", "total": "Totale", "qr": "Link al codice QR del negozio",
           "msg_from": "Un messaggio da {name}", "thanks": "Grazie per l'acquisto! Le auguro di goderselo!",
           "other_service": "Altro",
           "note": "Totale comprensivo dell'IVA riscossa da eBay. eBay ha trattenuto questa IVA dall'importo pagato "
                   "dall'acquirente e la versa alle autorità fiscali competenti."},
    "es": {"title": "FACTURA", "ship_to": "Dirección de envío", "sender": "Dirección del remitente",
           "reg": "Dirección registrada del comprador", "user": "Usuario", "order": "Pedido", "order_date": "Fecha del pedido",
           "record": "N.º de registro de venta", "cols": ("Artículo", "Cantidad", "Precio", "Tipo de IVA", "Total artículo"),
           "item_no": "N.º de artículo", "service": "Servicio de envío elegido por el comprador", "subtotal": "Subtotal",
           "shipping": "Envío", "discount": "Descuento", "vat": "IVA (recaudado por eBay)", "tax": "Impuesto (recaudado por eBay)",
           "vat_incl": "IVA incluido", "total": "Total", "qr": "Enlace al código QR de la tienda",
           "msg_from": "Un mensaje de {name}", "thanks": "¡Gracias por su compra! ¡Que lo disfrute!",
           "other_service": "Otro",
           "note": "Total con el IVA recaudado por eBay incluido. eBay ha deducido este IVA del importe pagado por el "
                   "comprador y lo ingresa en las autoridades fiscales competentes."},
}


def render_normal(doc: dict, number: str) -> bytes:
    f = fonts()
    lang = doc["language"]
    L = NORMAL.get(lang, NORMAL["en"])
    comma = lang != "en"
    cur = doc["currency"]
    gray = colors.HexColor("#707070")
    base = ParagraphStyle("n", fontName=f["sans"], fontSize=9, leading=12)
    bold = ParagraphStyle("nb", parent=base, fontName=f["sans_bold"])
    small = ParagraphStyle("ns", parent=base, fontSize=7.5, leading=10, textColor=gray)
    shop = ParagraphStyle("shop", parent=base, fontName=f["sans_bold"], fontSize=15, leading=19, alignment=TA_CENTER,
                          textColor=gray)
    title = ParagraphStyle("t", parent=bold, fontSize=9, alignment=TA_RIGHT, textColor=gray)
    order = ParagraphStyle("o", parent=bold, fontSize=14, leading=18)
    right_gray = ParagraphStyle("rg", parent=base, fontSize=11, leading=14, alignment=TA_RIGHT, textColor=gray)
    num = ParagraphStyle("num", parent=base, alignment=TA_RIGHT)
    num_bold = ParagraphStyle("numb", parent=bold, alignment=TA_RIGHT)
    head_r = ParagraphStyle("hr", parent=bold, alignment=TA_RIGHT)
    money = lambda v: f"{cur} {_money(v, lang, comma)}"  # noqa: E731

    url = doc.get("store_url")
    head = [_p(doc.get("store_name") or "", shop)]
    if url:
        head.append(_p(url, ParagraphStyle("url", parent=bold, fontSize=8.5, alignment=TA_CENTER)))
    right = [_p(L["title"], title)]
    if url:  # 和 eBay 单据一样：右上角“店铺二维码”
        qr_label = ParagraphStyle("qrl", parent=base, fontSize=8, leading=10, alignment=TA_CENTER)
        right += [Spacer(1, 4 * mm), _p(L["qr"], qr_label), Spacer(1, 1.5 * mm), _qr(url, 18 * mm)]
    top = Table([["", head, right]], colWidths=[40 * mm, 100 * mm, 40 * mm])
    top.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("ALIGN", (2, 0), (2, 0), "CENTER")]))

    s = doc["seller"]
    contact = s.get("contact") if s.get("contact") and s.get("contact", "").lower() != (s.get("name") or "").lower() else ""
    seller_lines = [s.get("name") or "", contact or "", s.get("address") or "", s.get("postcode") or ""]
    ship = [_p(L["ship_to"], bold), _p("\n".join(doc["ship_to"]), base)]
    for ref in doc.get("ebay_tax_reference") or []:
        ship.append(_p(f"VAT Paid: {ref}", base))
    addresses = Table([[ship, [_p(L["sender"], bold), _p("\n".join(x for x in seller_lines if x), base)],
                        [_p(L["reg"], bold), _p("\n".join(doc["bill_to"]), base)]]],
                      colWidths=[70 * mm, 55 * mm, 55 * mm])
    addresses.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 4)]))
    buyer = [_p(doc[k], base) for k in ("buyer_phone", "buyer_email") if doc.get(k)]  # 同 eBay 单据：电话、eBay 转发邮箱
    if doc.get("buyer_username"):
        buyer.append(_p(f"{L['user']}: {doc['buyer_username']}", base))
    if doc.get("buyer_vat"):
        buyer.append(_p(f"VAT: {doc['buyer_vat']}", base))

    order_tbl = Table([[_p(f"{L['order']}: {doc['order_id']}", order),
                        _p(f"{L['order_date']}: {_date(doc.get('order_date'), lang, 'long')}", right_gray)]],
                      colWidths=[110 * mm, 70 * mm])
    order_tbl.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "BOTTOM"), ("LEFTPADDING", (0, 0), (-1, -1), 0)]))
    record = _p(f"{L['record']}: {doc['sales_record']}", ParagraphStyle("rec", parent=base, fontSize=10, textColor=gray)) \
        if doc.get("sales_record") else Spacer(1, 1)

    c = L["cols"]
    rows = [[_p(c[0], bold), _p(c[1], head_r), _p(c[2], head_r), _p(c[3], head_r), _p(c[4], head_r)]]
    for li in doc["lines"]:
        desc = li["title"] + (f" ({L['item_no']}: {li['item_id']})" if li.get("item_id") else "")
        rate = f"{_rate(doc['vat_rate'])}%" if doc.get("vat_rate") is not None else ""
        rows.append([_p(desc, base), _p(str(li["quantity"]), num), _p(money(li["unit_price"]), num), _p(rate, num),
                     _p(money(li["amount"]), num)])
    items = Table(rows, colWidths=[80 * mm, 22 * mm, 25 * mm, 23 * mm, 30 * mm], repeatRows=1)
    items.setStyle(TableStyle([("LINEABOVE", (0, 0), (-1, 0), 0.5, colors.HexColor("#CCCCCC")),
                               ("LINEBELOW", (0, 0), (-1, 0), 0.5, colors.HexColor("#CCCCCC")),
                               ("LINEBELOW", (0, -1), (-1, -1), 0.5, colors.HexColor("#CCCCCC")),
                               ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("TOPPADDING", (0, 1), (-1, -1), 8),
                               ("BOTTOMPADDING", (0, 1), (-1, -1), 8)]))

    sums = [[_p(L["subtotal"], base), _p(money(doc["subtotal"]), num)],
            [_p(L["shipping"], base), _p(money(doc["shipping"]), num)]]
    if doc.get("discount"):
        sums.append([_p(L["discount"], base), _p("-" + money(doc["discount"]), num)])
    mode = doc.get("tax_mode")
    if mode == "ebay_collected":
        for t in doc.get("ebay_taxes") or []:
            sums.append([_p(L["vat"] if t["type"] == "VAT" else L["tax"], base), _p(money(t["amount"]), num)])
    star = "**" if mode == "ebay_collected" else ""
    sums.append([_p(L["total"] + star, bold), _p(money(doc["total"]), num_bold)])
    if mode == "included" and doc.get("vat_amount"):
        sums.append([_p(f"{L['vat_incl']} {_rate(doc['vat_rate'])}%", small), _p(money(doc["vat_amount"]), num)])
    service = _p(f"{L['service']}: {_service_label(doc['shipping_service'], L)}",
                 ParagraphStyle("sv", parent=base, alignment=TA_RIGHT, textColor=gray)) \
        if doc.get("shipping_service") else Spacer(1, 1)
    sum_tbl = Table(sums, colWidths=[50 * mm, 30 * mm])
    sum_tbl.setStyle(TableStyle([("TOPPADDING", (0, 0), (-1, -1), 1), ("BOTTOMPADDING", (0, 0), (-1, -1), 1)]))
    # 左下角卖家留言（同 eBay 单据“Eine Nachricht von …”），署名用店铺 eBay 注册人
    sender = s.get("contact") or s.get("name") or doc.get("store_name") or ""
    message = [_p(L["msg_from"].format(name=sender), bold), Spacer(1, 2 * mm), _p(L["thanks"], base)]
    bottom = Table([[message, sum_tbl]], colWidths=[95 * mm, 85 * mm])
    bottom.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "BOTTOM"), ("LEFTPADDING", (0, 0), (0, 0), 4)]))

    story = [top, Spacer(1, 8 * mm), addresses, Spacer(1, 3 * mm), *buyer, Spacer(1, 8 * mm), order_tbl, record,
             Spacer(1, 5 * mm), items, Spacer(1, 3 * mm), service, Spacer(1, 3 * mm), bottom]
    if s.get("footer"):
        story += [Spacer(1, 6 * mm), _p(s["footer"], small)]
    if mode == "ebay_collected":
        story += [Spacer(1, 10 * mm), _p("**" + L["note"], small)]
    return _build(story, f"{L['title']} {doc['order_id']}", s.get("name"))


# ------------------------------------------------------------------ 增值税发票：照店铺 Excel 模板
VAT = {
    "de": {"title": "Rechnung", "address": "Address", "postcode": "Postcode", "vat_no": "USt-IDNr",
           "number": "Rechnungsnummer:", "date": "Rechnungsdatum:", "to": "Nach:", "ship_to": "Versand nach:",
           "cols": ("Produkt-ID", "Beschreibungen", "Mengen", "Stückpreis ({cur})", "Gesamtpreis ({cur})"),
           "subtotal": "Stückpreis", "shipping": "Versandkosten", "vat": "VAT {r}%", "discount": "Discount",
           "total": "Gesamtpreis", "channel": "Vertriebskanal:", "order": "OrderID:", "buyer_vat": "USt-IdNr. Käufer"},
    "en": {"title": "INVOICE", "address": "Address", "postcode": "Postcode", "vat_no": "VAT Registration Number:",
           "number": "Invoice Number:", "date": "Date of Invoice:", "to": "To:", "ship_to": "Ship To:",
           "cols": ("Product ID", "Descriptions", "Quantities", "Unit Price ({cur})", "Amount ({cur})"),
           "subtotal": "Sub Total", "shipping": "Shipping", "vat": "VAT {r}%", "discount": "Discount", "total": "TOTAL",
           "channel": "Sales Channel:", "order": "Order ID:", "buyer_vat": "Buyer VAT No."},
    "fr": {"title": "FACTURE", "address": "Adresse", "postcode": "Code postal", "vat_no": "Numéro de TVA :",
           "number": "Numéro de facture :", "date": "Date de facture :", "to": "À :", "ship_to": "Livrer à :",
           "cols": ("Réf. produit", "Descriptions", "Quantités", "Prix unitaire ({cur})", "Montant ({cur})"),
           "subtotal": "Sous-total", "shipping": "Frais de port", "vat": "TVA {r}%", "discount": "Remise", "total": "TOTAL",
           "channel": "Canal de vente :", "order": "N° de commande :", "buyer_vat": "N° TVA client"},
    "it": {"title": "FATTURA", "address": "Indirizzo", "postcode": "Codice postale", "vat_no": "Numero di partita IVA:",
           "number": "Numero di fattura:", "date": "Data della fattura:", "to": "A:", "ship_to": "Spedire a:",
           "cols": ("Codice prodotto", "Descrizioni", "Le quantità", "Prezzo unitario ({cur})", "Importo ({cur})"),
           "subtotal": "Totale parziale", "shipping": "Spese di spedizione", "vat": "IVA {r}%", "discount": "Sconto",
           "total": "TOTALE", "channel": "Canale di vendita:", "order": "ID:", "buyer_vat": "P. IVA cliente"},
    "es": {"title": "FACTURA", "address": "Dirección", "postcode": "Código postal", "vat_no": "Número de IVA:",
           "number": "Número de factura:", "date": "Fecha de la factura:", "to": "A:", "ship_to": "Enviar a:",
           "cols": ("Código del producto", "Descripciones", "Cantidades", "Precio unitario ({cur})", "Importe ({cur})"),
           "subtotal": "Subtotal", "shipping": "Gastos de envío", "vat": "IVA {r}%", "discount": "Descuento",
           "total": "TOTAL", "channel": "Canal de ventas:", "order": "Número de pedido:", "buyer_vat": "NIF-IVA cliente"},
}
# 模板列宽（Excel 第 B–F 列：4384、7104、9408、4992、5088）按 180mm 等比
VAT_COLS = [w * 180 / 30976 * mm for w in (4384, 7104, 9408, 4992, 5088)]


def render_vat(doc: dict, number: str) -> bytes:
    f = fonts()
    lang = doc["language"]
    L = VAT.get(lang, VAT["en"])
    cur = doc["currency"]
    base = ParagraphStyle("v", fontName=f["serif"], fontSize=9, leading=11.5)
    company = ParagraphStyle("vc", parent=base, fontName=f["serif_bold"], fontSize=10.5, leading=13)
    title = ParagraphStyle("vt", parent=base, fontName=f["serif_bold"], fontSize=24, leading=28, alignment=TA_CENTER)
    num = ParagraphStyle("vn", parent=base, alignment=TA_RIGHT)
    num_bold = ParagraphStyle("vnb", parent=num, fontName=f["serif_bold"])
    small = ParagraphStyle("vs", parent=base, fontSize=8, leading=10)
    label = ParagraphStyle("vl", parent=base, fontSize=8.5)
    m = lambda v: _money(v, lang, False)  # noqa: E731
    s = doc["seller"]

    buyer = list(doc["bill_to"][:2]) if doc["bill_to"] else []
    to_cell = [_p("\n".join(buyer), base)]
    if doc.get("buyer_vat"):
        to_cell.append(_p(f"{L['buyer_vat']}: {doc['buyer_vat']}", small))
    rows = [
        ["", _p(f"[{s.get('name') or ''}]", company), "", _p(L["title"], title), ""],
        ["", "", "", "", ""],
        ["", _p(f"{L['address']}: {s.get('address') or ''}", base), "", "", ""],
        ["", "", "", "", ""],
        ["", _p(f"{L['postcode']}:{s.get('postcode') or ''}", base), "", _p(L["number"], label), _p(number, base)],
        ["", _p(L["vat_no"], base), _p(s.get("vat_number") or "", base), _p(L["date"], label),
         _p(_date(doc["issue_date"], lang, "short"), base)],
        [_p(L["to"], base), to_cell, _p(L["ship_to"], base), _p("\n".join(doc["ship_to"]), base), ""],
        [_p(c.format(cur=cur), base) for c in L["cols"]],
    ]
    first_item = len(rows)
    for li in doc["lines"]:
        rows.append([_p(li.get("item_id") or "", base), _p(li["title"], base), _p(str(li["quantity"]), base),
                     _p(m(li["unit_price"]), num), _p(m(li["amount"]), num)])
    while len(rows) < first_item + 4:  # 模板固定 4 行明细
        rows.append(["", "", "", "", ""])
    first_sum = len(rows)
    vat_label = L["vat"].format(r=_rate(doc["vat_rate"])) if doc.get("vat_rate") is not None else L["vat"].format(r="")
    if doc.get("tax_mode") == "included":
        vat_label += " (incl.)"
    sums = [(L["subtotal"], m(doc["subtotal"])), (L["shipping"], m(doc["shipping"])), (vat_label, m(doc.get("vat_amount")))]
    if doc.get("discount"):
        sums.append((L["discount"], "-" + m(doc["discount"])))
    sums.append((L["total"], m(doc["total"])))
    for i, (label, value) in enumerate(sums):
        last = i == len(sums) - 1
        rows.append(["", "", "", _p(label, base), _p(value, num_bold if last else num)])
    footer_row = len(rows)
    rows.append([_p(L["channel"], base), _p(doc["sales_channel"].replace("ebay.", "eBay."), base),
                 _p(f"{L['order']}{doc.get('seller_account') or ''}", base), _p(doc["order_id"], base), ""])
    if s.get("footer"):
        rows.append([_p(s["footer"], small), "", "", "", ""])

    thin, thick = 0.6, 1.6
    st = [
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("BOX", (0, 0), (-1, footer_row), thick, colors.black),
        ("SPAN", (1, 0), (2, 1)), ("SPAN", (3, 0), (4, 3)), ("SPAN", (1, 2), (2, 3)), ("SPAN", (1, 4), (2, 4)),
        ("LINEBEFORE", (3, 0), (3, 3), thin, colors.black), ("LINEBELOW", (3, 3), (4, 3), thin, colors.black),
        ("GRID", (3, 4), (4, 5), thin, colors.black),
        ("SPAN", (3, 6), (4, 6)),
        ("GRID", (0, 6), (-1, first_sum - 1), thin, colors.black),
        ("SPAN", (0, first_sum), (2, first_sum + len(sums) - 1)),
        ("GRID", (3, first_sum), (4, first_sum + len(sums) - 1), thin, colors.black),
        ("LINEABOVE", (0, first_sum), (2, first_sum), thin, colors.black),
        ("SPAN", (3, footer_row), (4, footer_row)),
        ("GRID", (0, footer_row), (-1, footer_row), thin, colors.black),
        ("LINEABOVE", (0, footer_row), (-1, footer_row), thick, colors.black),
        ("ROWBACKGROUNDS", (0, first_item - 1), (-1, first_item - 1), [colors.HexColor("#F2F2F2")]),
        ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]
    if s.get("footer"):
        st.append(("SPAN", (0, footer_row + 1), (4, footer_row + 1)))
    heights = [None] * len(rows)
    heights[6] = 26 * mm  # 收票人 / 收货地址行（模板 78pt）
    t = Table(rows, colWidths=VAT_COLS, rowHeights=heights)
    t.setStyle(TableStyle(st))
    return _build([t], f"{L['title']} {number}", s.get("name"))


def _build(story: list, title: str, author: str | None) -> bytes:
    buf = io.BytesIO()
    SimpleDocTemplate(buf, pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm, topMargin=15 * mm,
                      bottomMargin=15 * mm, title=title, author=author or "", invariant=1).build(story)
    return buf.getvalue()


def render(doc: dict, number: str) -> bytes:
    return render_vat(doc, number) if doc.get("kind") == "VAT" else render_normal(doc, number)
