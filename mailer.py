"""
mailer.py — ONE reliable way to send email from EduHire
=======================================================
Used by: candidate email drafts, pipeline stage notifications, interview summary emails.

Tries, in order:
  1. Gmail SMTP, port 465 (SSL)         needs the 16-char App Password
  2. Gmail SMTP, port 587 (STARTTLS)    needs the 16-char App Password
  3. Gmail API over HTTPS (port 443)    needs credentials.json (same Google Cloud project as Calendar)

Why step 3 exists: many college / office networks and VPNs block SMTP ports ("timed out") but
allow normal HTTPS, which is what Google Calendar already uses.
If SMTP was found blocked, later sends try the Gmail API first for 10 minutes (no repeated 30 s waits).

send_mail(...) -> {"success": bool, "message": str, "via": "smtp-465" | "smtp-587" | "gmail-api" | ""}
"""
from __future__ import annotations

import base64
import os
import pickle
import smtplib
import socket
import ssl
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

GMAIL_SMTP_HOST     = "smtp.gmail.com"
GMAIL_SMTP_PORT     = 465
GMAIL_STARTTLS_PORT = 587
_CONNECT_TIMEOUT    = 15
_APP_DIR            = os.path.dirname(os.path.abspath(__file__))
GMAIL_SEND_SCOPE    = "https://www.googleapis.com/auth/gmail.send"
GMAIL_TOKEN_FILE    = os.path.join(_APP_DIR, "gmail_token.pkl")
_SMTP_BLOCKED_FOR   = 600          # seconds to skip SMTP after both ports failed
_state = {"smtp_blocked_until": 0.0}


# ── SMTP (IPv4 only, 465 then 587) ───────────────────────────────────────────

def _ipv4_socket(host: str, port: int, timeout: float):
    """IPv4 only: a broken IPv6 route is a classic cause of 'timed out'."""
    last = None
    for _f, _t, _p, _c, addr in socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM):
        try:
            return socket.create_connection(addr, timeout)
        except OSError as e:
            last = e
    raise last or OSError(f"no IPv4 address for {host}")


class _SMTP4(smtplib.SMTP):
    def _get_socket(self, host, port, timeout):
        return _ipv4_socket(host, port, timeout)


class _SMTP4_SSL(smtplib.SMTP_SSL):
    def _get_socket(self, host, port, timeout):
        return self.context.wrap_socket(_ipv4_socket(host, port, timeout), server_hostname=self._host)


def _open_smtp(mode: str, host: str, timeout: float):
    ctx = ssl.create_default_context()
    if mode == "ssl":
        return _SMTP4_SSL(host, GMAIL_SMTP_PORT, context=ctx, timeout=timeout)
    server = _SMTP4(host, GMAIL_STARTTLS_PORT, timeout=timeout)
    server.ehlo()
    server.starttls(context=ctx)
    server.ehlo()
    return server


# ── Gmail API (HTTPS) ────────────────────────────────────────────────────────

def _resolve_creds(path: str) -> str:
    try:
        from calendar_scheduler import _resolve_creds_path
        return _resolve_creds_path(path)
    except Exception:
        return path


def gmail_api_available(credentials_path: str = "credentials.json") -> bool:
    """True if the Google libraries are installed and we have a token or a credentials.json."""
    try:
        import googleapiclient, google_auth_oauthlib   # noqa: F401
    except ImportError:
        return False
    return os.path.isfile(GMAIL_TOKEN_FILE) or os.path.isfile(_resolve_creds(credentials_path))


def _gmail_api_service(credentials_path: str):
    from google.auth.transport.requests import Request
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    creds = None
    if os.path.exists(GMAIL_TOKEN_FILE):
        try:
            with open(GMAIL_TOKEN_FILE, "rb") as f:
                creds = pickle.load(f)
        except Exception:
            creds = None
    if creds and not creds.valid and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception:
            creds = None
            try:
                os.remove(GMAIL_TOKEN_FILE)
            except OSError:
                pass
    if not creds or not creds.valid:
        path = _resolve_creds(credentials_path)
        if not os.path.isfile(path):
            raise FileNotFoundError(f"credentials.json not found (looked for '{credentials_path}' and in {_APP_DIR})")
        flow = InstalledAppFlow.from_client_secrets_file(path, [GMAIL_SEND_SCOPE])
        try:
            creds = flow.run_local_server(port=0, open_browser=True, timeout_seconds=180)
        except TypeError:
            creds = flow.run_local_server(port=0, open_browser=True)
        try:
            with open(GMAIL_TOKEN_FILE, "wb") as f:
                pickle.dump(creds, f)
        except Exception:
            pass
    try:
        import httplib2, google_auth_httplib2
        http = google_auth_httplib2.AuthorizedHttp(creds, http=httplib2.Http(timeout=30))
        return build("gmail", "v1", http=http, cache_discovery=False)
    except ImportError:
        return build("gmail", "v1", credentials=creds, cache_discovery=False)


def _gmail_hint(err) -> str:
    t = str(err)
    low = t.lower()
    try:
        from calendar_scheduler import explain_google_error
        t = explain_google_error(t)
    except Exception:
        pass
    if "gmail api has not been used" in low or "accessnotconfigured" in low or "insufficient" in low and "scope" in low:
        t += (" → Fix: Google Cloud Console → APIs & Services → Library → enable 'Gmail API' "
              "(and add the scope gmail.send on the OAuth consent screen if asked).")
    return t


def _send_via_gmail_api(msg, credentials_path: str) -> None:
    service = _gmail_api_service(credentials_path)
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    service.users().messages().send(userId="me", body={"raw": raw}).execute()


# ── Public API ───────────────────────────────────────────────────────────────

def send_mail(
    sender_email: str,
    to_email: str,
    subject: str,
    plain_body: str,
    html_body: Optional[str] = None,
    sender_name: str = "",
    app_password: str = "",
    credentials_path: str = "credentials.json",
) -> dict:
    to_email = (to_email or "").strip()
    sender_email = (sender_email or "").strip()
    if not to_email or "@" not in to_email:
        return {"success": False, "message": "❌ Please enter a valid recipient email address.", "via": ""}
    if not sender_email or "@gmail.com" not in sender_email.lower():
        return {"success": False, "message": "❌ Sender email must be a Gmail address (e.g. you@gmail.com).", "via": ""}

    pw = (app_password or "").replace(" ", "").strip()
    smtp_ok_input = len(pw) == 16
    api_ok = gmail_api_available(credentials_path)
    if not smtp_ok_input and not api_ok:
        return {"success": False, "via": "", "message": (
            "❌ No way to send: add the 16-character Gmail App Password in ⚙️ Settings "
            "(myaccount.google.com/apppasswords), or put credentials.json next to app.py to send through the Gmail API.")}

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = f"{sender_name} <{sender_email}>" if sender_name else sender_email
    msg["To"]      = to_email
    msg.attach(MIMEText(plain_body or "", "plain", "utf-8"))
    if html_body:
        msg.attach(MIMEText(html_body, "html", "utf-8"))

    notes: list[str] = []

    # 1-2. SMTP (skipped for a while if it was just found blocked and the API is available)
    skip_smtp = (not smtp_ok_input) or (api_ok and time.time() < _state["smtp_blocked_until"])
    if not skip_smtp:
        net_fail = 0
        for mode, label in (("ssl", "465"), ("starttls", "587")):
            server = None
            try:
                server = _open_smtp(mode, GMAIL_SMTP_HOST, _CONNECT_TIMEOUT)
                server.login(sender_email, pw)
                server.sendmail(sender_email, to_email, msg.as_string())
                _state["smtp_blocked_until"] = 0.0
                return {"success": True, "via": f"smtp-{label}", "message": f"✅ Email sent to {to_email}"}
            except smtplib.SMTPAuthenticationError:
                notes.append("Gmail rejected the App Password (check the 16-character code and that 2-Step Verification is ON)")
                break                                   # same login on 587 would fail too
            except smtplib.SMTPRecipientsRefused:
                return {"success": False, "via": "", "message": f"❌ Recipient address rejected: {to_email}"}
            except (smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError) as e:
                notes.append(f"port {label}: connection dropped ({e})"); net_fail += 1
            except smtplib.SMTPException as e:
                return {"success": False, "via": "", "message": f"❌ Gmail SMTP error: {e}"}
            except OSError as e:
                notes.append(f"port {label}: {str(e) or type(e).__name__}"); net_fail += 1
            finally:
                if server is not None:
                    try:
                        server.quit()
                    except Exception:
                        pass
        if net_fail == 2:
            _state["smtp_blocked_until"] = time.time() + _SMTP_BLOCKED_FOR

    # 3. Gmail API over HTTPS
    if api_ok:
        try:
            _send_via_gmail_api(msg, credentials_path)
            return {"success": True, "via": "gmail-api",
                    "message": f"✅ Email sent to {to_email} (via Gmail API; SMTP was unavailable)" if notes else f"✅ Email sent to {to_email} (via Gmail API)"}
        except Exception as e:
            err = getattr(e, "content", None)
            notes.append("Gmail API: " + _gmail_hint(e if not err else f"{e} {err.decode(errors='ignore')[:200]}"))

    advice = ("Your network is probably blocking outgoing email (college/office Wi-Fi, VPN, antivirus). "
              "Try a mobile hotspot, or use 'Open in Gmail' to send the draft manually."
              + ("" if api_ok else " Tip: put credentials.json next to app.py so EduHire can send through the Gmail API over HTTPS instead."))
    return {"success": False, "via": "", "message": "❌ Could not send — " + "; ".join(notes) + ". " + advice}
