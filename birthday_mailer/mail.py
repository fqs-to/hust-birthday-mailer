"""Render and send one greeting at a time over authenticated TLS SMTP."""

from __future__ import annotations

import html
import os
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import format_datetime, formataddr
from datetime import datetime, timezone, timedelta

from .contacts import DataError, Member, validate_email

BEIJING = timezone(timedelta(hours=8), "Asia/Shanghai")


def render_greeting(member: Member, organization: str) -> tuple[str, str, str]:
    subject = f"{member.name}，生日快乐！｜{organization}"
    salutation = member.name if member.name.endswith("同学") else f"{member.name}同学"
    signature = f"华中科技大学{organization}" if organization == "计算机学院资委会" else organization
    text = (f"亲爱的{salutation}：\n\n您好！\n\n"
            f"今天是您的生日，{organization}祝您生日快乐、万事胜意、天天开心、无限进步！\n\n"
            f"{signature}\n\n\n"
            "────────────────────\n"
            "投递说明\n"
            "这是一封自动送达的生日祝福。\n"
            "如生日或邮箱有误，或希望停止接收，请回复告知。\n")
    name = html.escape(salutation)
    org = html.escape(organization)
    sign = html.escape(signature)
    markup = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;background:#ffffff;color:#222222;font-family:'Microsoft YaHei','PingFang SC',Arial,sans-serif;">
<div style="max-width:640px;margin:0 auto;padding:24px 20px;font-size:16px;line-height:1.9;">
<p style="margin:0 0 20px;">亲爱的{name}：</p>
<p style="margin:0 0 20px;">您好！</p>
<p style="margin:0 0 28px;">今天是您的生日，{org}祝您生日快乐、万事胜意、天天开心、无限进步！</p>
<p style="margin:0;">{sign}</p>
<div style="margin-top:36px;padding-top:14px;border-top:1px solid #e1e5e9;color:#757575;font-size:12px;line-height:1.75;">
<p style="margin:0 0 4px;">投递说明</p>
<p style="margin:0;">这是一封自动送达的生日祝福。<br>如生日或邮箱有误，或希望停止接收，请回复告知。</p>
</div>
</div></body></html>"""
    return subject, text, markup


@dataclass(frozen=True)
class SMTPConfig:
    host: str
    port: int
    security: str
    username: str
    password: str
    sender: str
    sender_name: str

    @classmethod
    def from_env(cls) -> "SMTPConfig":
        required = ("SMTP_USERNAME", "SMTP_PASSWORD")
        if any(not os.environ.get(key) for key in required):
            raise DataError("请配置 SMTP_USERNAME 和 SMTP_PASSWORD")
        security = os.environ.get("SMTP_SECURITY", "ssl").lower()
        if security not in {"ssl", "starttls"}:
            raise DataError("SMTP_SECURITY 只能为 ssl 或 starttls；不支持明文发信")
        try:
            port = int(os.environ.get("SMTP_PORT") or (465 if security == "ssl" else 587))
            if not 1 <= port <= 65535:
                raise ValueError
        except ValueError as exc:
            raise DataError("SMTP_PORT 无效") from exc
        sender = validate_email(os.environ.get("SMTP_FROM") or os.environ["SMTP_USERNAME"])
        sender_name = os.environ.get("SMTP_FROM_NAME") or os.environ.get("ORGANIZATION", "计算机学院资委会")
        if any(ord(c) < 32 for c in sender_name):
            raise DataError("发件人名称含控制字符")
        return cls(os.environ.get("SMTP_HOST") or "mail.hust.edu.cn", port, security,
                   os.environ["SMTP_USERNAME"], os.environ["SMTP_PASSWORD"], sender, sender_name)


def build_message(member: Member, config: SMTPConfig, organization: str, message_id: str, *, test: bool = False) -> EmailMessage:
    subject, text, markup = render_greeting(member, organization)
    message = EmailMessage()
    message["From"] = formataddr((config.sender_name, config.sender))
    message["To"] = formataddr((member.name, member.email))
    message["Reply-To"] = config.sender
    message["Subject"] = ("【测试邮件】" if test else "") + subject
    message["Date"] = format_datetime(datetime.now(BEIJING))
    message["Message-ID"] = message_id
    message["Auto-Submitted"] = "auto-generated"
    message["X-Auto-Response-Suppress"] = "All"
    message.set_content(text)
    message.add_alternative(markup, subtype="html")
    return message


class SMTPTransport:
    def __init__(self, config: SMTPConfig):
        self.config = config
        self.connection: smtplib.SMTP | None = None

    def __enter__(self) -> "SMTPTransport":
        context = ssl.create_default_context()
        connection: smtplib.SMTP | None = None
        try:
            if self.config.security == "ssl":
                connection = smtplib.SMTP_SSL(self.config.host, self.config.port, timeout=30, context=context)
            else:
                connection = smtplib.SMTP(self.config.host, self.config.port, timeout=30)
                connection.ehlo()
                connection.starttls(context=context)
                connection.ehlo()
            connection.login(self.config.username, self.config.password)
            self.connection = connection
            return self
        except Exception:
            if connection is not None:
                connection.close()
            raise

    def send(self, message: EmailMessage) -> None:
        if self.connection is None:
            raise RuntimeError("SMTP connection is not open")
        refused = self.connection.send_message(message, from_addr=self.config.sender)
        if refused:
            raise smtplib.SMTPRecipientsRefused(refused)

    def __exit__(self, *args: object) -> None:
        if self.connection:
            # QUIT errors after DATA was accepted must not change a sent record.
            try:
                self.connection.quit()
            except (OSError, smtplib.SMTPException):
                self.connection.close()
