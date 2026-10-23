"""SMTP 邮件发送（邮箱验证 / 找回密码 / 到期提醒）。

设计：
  1. 配置全部来自 Settings（SMTP_*），未配置时 `smtp_configured()` 返回 False；
  2. 同步发送（调用方放进线程池）—— 单 worker 架构下足够，不引入队列；
  3. 失败抛 EmailError，由调用方决定「注册仍成功 + 可重发」还是报错；
  4. 支持 465 SSL（SMTP_USE_SSL=true）与 587 STARTTLS（false）。

测试用：直接 monkeypatch `send_email` / `_deliver`，不连真实 SMTP。
"""

from __future__ import annotations

import logging
import smtplib
import ssl
from email.message import EmailMessage

from app.config import get_settings

logger = logging.getLogger(__name__)


class EmailError(Exception):
    """邮件发送失败。"""


def smtp_configured() -> bool:
    s = get_settings()
    return bool(s.smtp_host and s.smtp_from)


def _deliver(to: str, subject: str, text_body: str,
             html_body: str = '') -> None:
    """真正连接 SMTP 并发送（可被测试替换）。"""
    s = get_settings()
    if not smtp_configured():
        raise EmailError('SMTP 未配置（SMTP_HOST / SMTP_FROM 为空）')

    msg = EmailMessage()
    msg['Subject'] = subject
    msg['From'] = s.smtp_from
    msg['To'] = to
    msg.set_content(text_body)
    if html_body:
        msg.add_alternative(html_body, subtype='html')

    context = ssl.create_default_context()
    try:
        if s.smtp_use_ssl:
            wire = smtplib.SMTP_SSL(s.smtp_host, s.smtp_port,
                                    timeout=s.smtp_timeout, context=context)
        else:
            wire = smtplib.SMTP(s.smtp_host, s.smtp_port,
                                timeout=s.smtp_timeout)
        with wire as server:
            if not s.smtp_use_ssl:
                server.ehlo()
                server.starttls(context=context)
                server.ehlo()
            if s.smtp_user:
                server.login(s.smtp_user, s.smtp_password)
            server.send_message(msg)
    except (smtplib.SMTPException, OSError) as e:
        raise EmailError(f'SMTP 发送失败: {type(e).__name__}: {e}') from e


def send_email(to: str, subject: str, text_body: str,
               html_body: str = '') -> None:
    """发送一封邮件。失败抛 EmailError（不吞异常）。"""
    _deliver(to, subject, text_body, html_body)
    logger.info('邮件已发送 to=%s subject=%s', to, subject)


def verification_email(verify_url: str, expires_hours: int,
                       brand: str = '经营诊断') -> tuple[str, str, str]:
    """构造验证邮件 (subject, text, html)。"""
    subject = f'{brand} - 请验证你的邮箱'
    text = (
        f'欢迎注册{brand}。\n\n'
        f'请打开下面的链接完成邮箱验证：\n{verify_url}\n\n'
        f'链接 {expires_hours} 小时内有效，且只能使用一次。\n'
        f'验证成功后将发放注册赠送的诊断次数。\n\n'
        f'如果这不是你的操作，请忽略本邮件。\n'
    )
    html = (
        f'<div style="font-family:-apple-system,\'Microsoft YaHei\',sans-serif;'
        f'max-width:520px;margin:24px auto;padding:24px;border:1px solid #e5e7eb;'
        f'border-radius:12px">'
        f'<h2 style="margin:0 0 12px;color:#1a3d2b">验证你的邮箱</h2>'
        f'<p>欢迎注册<b>{brand}</b>。点击下方按钮完成验证，'
        f'验证成功后发放注册赠送的诊断次数。</p>'
        f'<p style="margin:24px 0">'
        f'<a href="{verify_url}" style="background:#2c6e49;color:#fff;'
        f'padding:12px 22px;border-radius:8px;text-decoration:none">'
        f'完成邮箱验证</a></p>'
        f'<p style="color:#8c959f;font-size:12px">'
        f'链接 {expires_hours} 小时内有效，只能使用一次。'
        f'如果这不是你的操作，请忽略本邮件。</p>'
        f'<p style="color:#8c959f;font-size:12px;word-break:break-all">'
        f'{verify_url}</p></div>'
    )
    return subject, text, html