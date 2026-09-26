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
    title = "Redefinir a sua senha" if reset else "Confirme o seu email"
    introduction = (
        "Recebemos um pedido para redefinir a senha da sua conta."
        if reset
        else "Obrigado por criar a sua conta. Confirme o email para continuar no Fretix."
    )
    html_content = f"""<!doctype html>
<html lang="pt">
  <body style="margin:0;padding:0;background:#f3f4f6;font-family:Arial,Helvetica,sans-serif;color:#111827;">
    <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#f3f4f6;padding:32px 12px;">
      <tr><td align="center">
        <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="max-width:560px;background:#ffffff;border-radius:20px;overflow:hidden;border:1px solid #e5e7eb;">
          <tr>
            <td style="background:#0b0f14;padding:26px 32px;text-align:center;">
              <div style="font-size:30px;font-weight:900;letter-spacing:1px;color:#ffffff;">FRET<span style="color:#ffc107;">IX</span></div>
              <div style="margin-top:6px;font-size:12px;color:#9ca3af;">Transporte simples, seguro e conectado</div>
            </td>
          </tr>
          <tr>
            <td style="padding:34px 32px 18px;text-align:center;">
              <div style="display:inline-block;background:#fff8db;color:#8a6500;border-radius:999px;padding:7px 13px;font-size:12px;font-weight:700;">CÓDIGO DE SEGURANÇA</div>
              <h1 style="margin:20px 0 10px;font-size:25px;line-height:1.25;color:#111827;">{title}</h1>
              <p style="margin:0 auto;max-width:430px;color:#6b7280;font-size:15px;line-height:1.6;">{introduction}</p>
            </td>
          </tr>
          <tr>
            <td style="padding:14px 32px;text-align:center;">
              <div style="background:#0b0f14;border:2px solid #ffc107;border-radius:16px;padding:22px 12px;">
                <div style="color:#9ca3af;font-size:11px;font-weight:700;letter-spacing:1px;">SEU CÓDIGO</div>
                <div style="margin-top:8px;color:#ffc107;font-size:40px;font-weight:900;letter-spacing:9px;user-select:all;">{code}</div>
              </div>
              <p style="margin:12px 0 0;color:#6b7280;font-size:12px;">Toque e mantenha pressionado sobre o código para copiar.</p>
            </td>
          </tr>
          <tr>
            <td style="padding:14px 32px 34px;">
              <div style="background:#fff8db;border-left:4px solid #ffc107;border-radius:10px;padding:14px 16px;color:#5f4b00;font-size:13px;line-height:1.5;">
                Este código expira em <strong>{expiry} minutos</strong>. Nunca partilhe este código com outra pessoa.
              </div>
              <p style="margin:22px 0 0;color:#9ca3af;font-size:12px;line-height:1.55;text-align:center;">Se não solicitou esta ação, ignore este email. A sua conta continuará protegida.</p>
            </td>
          </tr>
          <tr>
            <td style="background:#f9fafb;border-top:1px solid #e5e7eb;padding:18px 24px;text-align:center;color:#9ca3af;font-size:11px;">
              © 2026 Fretix · Maputo, Moçambique<br/>Mensagem automática — não responda a este email.
            </td>
          </tr>
        </table>
      </td></tr>
    </table>
  </body>
</html>"""

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
                "htmlContent": html_content,
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
