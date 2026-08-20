import json
import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError


def _enabled(name, default=False):
    return os.getenv(name, str(default)).strip().lower() == "true"

def _smtp_config():
    host=os.getenv("SMTP_HOST", "smtp.gmail.com")
    port=int(os.getenv("SMTP_PORT", "587"))
    use_tls=os.getenv("SMTP_USE_TLS", "true").strip().lower() == "true"
    sender=os.getenv("EMAIL_ADDRESS", "").strip()
    password=os.getenv("EMAIL_PASSWORD", "")
    name=os.getenv("EMAIL_SENDER", "Student Attendance")
    return host, port, use_tls, sender, password, name

def _send_mail(recipient, subject, body):
    host, port, use_tls, sender, password, sender_name = _smtp_config()
    if not sender or not password or not recipient:
        return False, "SMTP email settings are incomplete"
    try:
        msg=MIMEMultipart()
        msg["From"]=f"{sender_name} <{sender}>"
        msg["To"]=recipient
        msg["Subject"]=subject
        msg.attach(MIMEText(body, "plain"))
        with smtplib.SMTP(host, port, timeout=30) as server:
            if use_tls:
                server.starttls()
            server.login(sender, password)
            server.sendmail(sender, recipient, msg.as_string())
        return True, "Email sent"
    except Exception as exc:
        return False, str(exc)


def send_email_notification(user, attendance):
    if not _enabled("EMAIL_NOTIFICATIONS", False):
        return False, "Email notifications disabled"
    recipient=getattr(user, "parent_email", None) or user.email
    if not recipient or str(recipient).endswith("@student.local"):
        return False, "No student/parent email is configured"
    _,_,_,_,_,sender_name=_smtp_config()
    return _send_mail(recipient, f"Attendance marked: {attendance.date}", f"Hello,\n\n{user.name}'s attendance was recorded on {attendance.date} at {attendance.time_in}.\n\nRegards,\n{sender_name}")


def send_teacher_verification_email(teacher, verification_url, expires_hours=24):
    _,_,_,_,_,sender_name=_smtp_config()
    body=(f"Hello {teacher.name},\n\nYour Student Attendance teacher account has been created.\n\n"
          f"Verify your email using this link:\n\n{verification_url}\n\n"
          f"This link expires in {expires_hours} hours.\n\nIf you did not expect this account, ignore this email.\n\nRegards,\n{sender_name}")
    return _send_mail((teacher.email or "").strip(), "Verify your Student Attendance teacher account", body)


def send_teacher_password_reset_email(teacher, reset_url, expires_minutes=30):
    _,_,_,_,_,sender_name=_smtp_config()
    body=(f"Hello {teacher.name},\n\nA password reset was requested for your teacher account.\n\n"
          f"Use this secure link to choose a new password:\n\n{reset_url}\n\n"
          f"This link expires in {expires_minutes} minutes and can only be used once.\n\nIf you did not request this, ignore this email.\n\nRegards,\n{sender_name}")
    return _send_mail((teacher.email or "").strip(), "Reset your Student Attendance teacher password", body)


def send_sms_notification(user, message):
    if not _enabled("SMS_NOTIFICATIONS", False):
        return False, "SMS notifications disabled"
    to=getattr(user, "parent_phone", None) or getattr(user, "phone", None)
    sid=os.getenv("TWILIO_ACCOUNT_SID"); token=os.getenv("TWILIO_AUTH_TOKEN"); from_phone=os.getenv("TWILIO_PHONE_NUMBER")
    if not all([to,sid,token,from_phone]): return False, "SMS provider or parent phone is not configured"
    try:
        from twilio.rest import Client
        Client(sid,token).messages.create(body=message,from_=from_phone,to=to)
        return True,"SMS sent"
    except Exception as exc: return False,str(exc)


def send_telegram_message(bot_token, chat_id, text):
    if not bot_token or not chat_id: return False, "Telegram is not configured"
    payload=json.dumps({"chat_id":chat_id,"text":text}).encode("utf-8")
    request=Request(f"https://api.telegram.org/bot{bot_token}/sendMessage",data=payload,headers={"Content-Type":"application/json"},method="POST")
    try:
        with urlopen(request,timeout=15) as response: data=json.loads(response.read().decode("utf-8"))
        return (True,"Telegram sent") if data.get("ok") else (False,data.get("description","Telegram rejected the message"))
    except (HTTPError,URLError,TimeoutError) as exc: return False,str(exc)
    except Exception as exc: return False,str(exc)


def send_telegram_notification(user, attendance):
    if not _enabled("TELEGRAM_NOTIFICATIONS", False): return False,"Telegram notifications disabled"
    return send_telegram_message(os.getenv("TELEGRAM_BOT_TOKEN"),getattr(user,"telegram_chat_id",None),f"Attendance recorded\n\nStudent: {user.name}\nDate: {attendance.date}\nTime: {attendance.time_in}")
