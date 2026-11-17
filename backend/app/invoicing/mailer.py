"""发票邮件：企业微信邮箱（腾讯企业邮）SMTP。账号密码只在 .env；INVOICE_MAIL_ENABLED=false 时不真正发送。"""
from __future__ import annotations

import re
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

from ..config import get_settings

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,24}")

SUBJECTS = {
    "de": ("Ihre Rechnung{ref} zur eBay-Bestellung {order}",
           "Guten Tag,\n\nanbei erhalten Sie die Rechnung{ref} zu Ihrer eBay-Bestellung {order}.\n\n"
           "Vielen Dank für Ihren Einkauf."),
    "en": ("Your invoice{ref} for eBay order {order}",
           "Hello,\n\nPlease find attached invoice{ref} for your eBay order {order}.\n\n"
           "Thank you for your purchase."),
    "fr": ("Votre facture{ref} pour la commande eBay {order}",
           "Bonjour,\n\nVeuillez trouver ci-joint la facture{ref} de votre commande eBay {order}.\n\n"
           "Merci pour votre achat."),
    "it": ("La sua fattura{ref} per l'ordine eBay {order}",
           "Buongiorno,\n\nin allegato trova la fattura{ref} relativa al suo ordine eBay {order}.\n\n"
           "Grazie per il suo acquisto."),
    "es": ("Su factura{ref} del pedido de eBay {order}",
           "Hola,\n\nAdjuntamos la factura{ref} de su pedido de eBay {order}.\n\n"
           "Gracias por su compra."),
}  # 正文不带落款（“Kind regards + 公司名”不要）；发件人显示名仍用 INVOICE_MAIL_FROM_NAME 或店铺公司名
FILE_PREFIX = {"de": "Rechnung", "en": "Invoice", "fr": "Facture", "it": "Fattura", "es": "Factura"}


class MailError(RuntimeError):
    pass


def valid_email(v: str | None) -> bool:
    return bool(v) and EMAIL_RE.fullmatch(v.strip()) is not None


def compose(*, to: str, number: str, order_id: str, language: str, sign: str, pdf: bytes,
            show_number: bool = True) -> EmailMessage:
    """show_number=False：普通发票上不印发票号，邮件里也只写订单号。"""
    s = get_settings()
    subject, body = SUBJECTS.get(language, SUBJECTS["en"])
    msg = EmailMessage()
    ref = f" {number}" if show_number else ""
    msg["Subject"] = subject.format(ref=ref, order=order_id)
    msg["From"] = formataddr((s.invoice_mail_from_name or sign, s.invoice_smtp_user or ""))
    msg["To"] = to
    msg["Message-ID"] = make_msgid()
    msg.set_content(body.format(ref=ref, order=order_id, sign=sign))
    msg.add_attachment(pdf, maintype="application", subtype="pdf",
                       filename=f"{FILE_PREFIX.get(language, 'Invoice')}_{number}.pdf")
    return msg


def send(msg: EmailMessage) -> None:
    """真正发出（调用前先确认 invoice_mail_not_ready_reasons() 为空）。失败抛 MailError（不含密码）。"""
    s = get_settings()
    rcpt = [msg["To"]] + ([s.invoice_mail_bcc] if s.invoice_mail_bcc else [])
    try:
        if s.invoice_smtp_port == 465:
            with smtplib.SMTP_SSL(s.invoice_smtp_host, s.invoice_smtp_port, timeout=30,
                                  context=ssl.create_default_context()) as smtp:
                smtp.login(s.invoice_smtp_user, s.invoice_smtp_password)
                smtp.send_message(msg, to_addrs=rcpt)
        else:
            with smtplib.SMTP(s.invoice_smtp_host, s.invoice_smtp_port, timeout=30) as smtp:
                smtp.starttls(context=ssl.create_default_context())
                smtp.login(s.invoice_smtp_user, s.invoice_smtp_password)
                smtp.send_message(msg, to_addrs=rcpt)
    except smtplib.SMTPAuthenticationError:
        raise MailError("邮箱登录失败：检查 INVOICE_SMTP_USER 和客户端专用密码，并确认企业邮箱已开启 SMTP 服务") from None
    except smtplib.SMTPRecipientsRefused:
        raise MailError("收件邮箱被邮件服务器拒收，请核对买家邮箱") from None
    except (smtplib.SMTPException, OSError) as e:
        raise MailError(f"发送失败：{type(e).__name__} {str(e)[:200]}") from None
