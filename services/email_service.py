"""Envio de mensagens transacionais por SMTP."""

import logging
import os
import smtplib
from email.message import EmailMessage

logger = logging.getLogger(__name__)


def send_otp_email(email: str, code: str, purpose: str) -> None:
    host = os.getenv("SMTP_HOST", "").strip()
    port = int(os.getenv("SMTP_PORT", "587"))
    username = os.getenv("SMTP_USERNAME", "").strip()
    password = os.getenv("SMTP_PASSWORD", "")
    from_email = os.getenv("SMTP_FROM_EMAIL", username).strip()
    from_name = os.getenv("SMTP_FROM_NAME", "Fretix")
    use_tls = os.getenv("SMTP_USE_TLS", "true").lower() in {"1", "true", "yes", "on"}
    expiry = int(os.getenv("OTP_EXPIRE_MINUTES", "10"))
    if not host or not from_email:
        logger.error("SMTP não configurado; não foi possível enviar OTP para %s", email)
        return

    reset = purpose == "password_reset"
    subject = "Código para redefinir a sua senha" if reset else "Verifique o seu email"
    action = "redefinir a sua senha" if reset else "verificar a sua conta"
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = f"{from_name} <{from_email}>"
    message["To"] = email
    message.set_content(
        f"Use o código {code} para {action} no Fretix. "
        f"O código expira em {expiry} minutos."
    )

    with smtplib.SMTP(host, port, timeout=20) as smtp:
        if use_tls:
            smtp.starttls()
        if username:
            smtp.login(username, password)
        smtp.send_message(message)
