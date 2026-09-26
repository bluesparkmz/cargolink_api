"""Envio de mensagens transacionais por SMTP."""

import logging
import os
import smtplib
from email.message import EmailMessage

import requests

logger = logging.getLogger(__name__)


def send_otp_email(email: str, code: str, purpose: str) -> None:
    api_key = os.getenv("BREVO_API_KEY", "").strip()
    from_email = os.getenv("BREVO_FROM_EMAIL", os.getenv("SMTP_FROM_EMAIL", "")).strip()
    from_name = os.getenv("BREVO_FROM_NAME", os.getenv("SMTP_FROM_NAME", "Fretix"))
    expiry = int(os.getenv("OTP_EXPIRE_MINUTES", "10"))
    reset = purpose == "password_reset"
    subject = "Código para redefinir a sua senha" if reset else "Verifique o seu email"
    action = "redefinir a sua senha" if reset else "verificar a sua conta"
    content = (
        f"Use o código {code} para {action} no Fretix. "
        f"O código expira em {expiry} minutos."
    )

    if api_key:
        if not from_email:
            raise RuntimeError("BREVO_FROM_EMAIL não está configurado")
        response = requests.post(
            "https://api.brevo.com/v3/smtp/email",
            headers={
                "accept": "application/json",
                "api-key": api_key,
                "content-type": "application/json",
            },
            json={
                "sender": {"name": from_name, "email": from_email},
                "to": [{"email": email}],
                "subject": subject,
                "textContent": content,
            },
            timeout=20,
        )
        response.raise_for_status()
        logger.info("OTP enviado pelo Brevo para %s", email)
        return

    host = os.getenv("SMTP_HOST", "").strip()
    port = int(os.getenv("SMTP_PORT", "587"))
    username = os.getenv("SMTP_USERNAME", "").strip()
    password = os.getenv("SMTP_PASSWORD", "")
    from_email = os.getenv("SMTP_FROM_EMAIL", username).strip()
    from_name = os.getenv("SMTP_FROM_NAME", "Fretix")
    use_tls = os.getenv("SMTP_USE_TLS", "true").lower() in {"1", "true", "yes", "on"}
    if not host or not from_email:
        logger.error("SMTP não configurado; não foi possível enviar OTP para %s", email)
        return

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = f"{from_name} <{from_email}>"
    message["To"] = email
    message.set_content(content)

    with smtplib.SMTP(host, port, timeout=20) as smtp:
        if use_tls:
            smtp.starttls()
        if username:
            smtp.login(username, password)
        smtp.send_message(message)
