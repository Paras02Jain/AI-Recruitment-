"""
net_check.py — quick connectivity diagnostics for Gmail sending and Google Calendar.

"timed out" almost always means the network (college / office Wi-Fi, VPN, firewall,
antivirus "email shield") is blocking the port, not that EduHire is broken.
This page-level checker tells you which hosts/ports are reachable.
"""
from __future__ import annotations
import socket
import time


def _probe(host: str, port: int, timeout: float = 4.0) -> tuple[bool, str]:
    t0 = time.time()
    try:
        socket.getaddrinfo(host, port, socket.AF_INET)
    except Exception as e:
        return False, f"DNS lookup failed ({e})"
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, f"{(time.time() - t0) * 1000:.0f} ms"
    except Exception as e:
        return False, (str(e) or type(e).__name__)


CHECKS = {
    "gmail": [
        ("Internet (www.google.com:443)", "www.google.com", 443),
        ("Gmail SMTP SSL (smtp.gmail.com:465)", "smtp.gmail.com", 465),
        ("Gmail SMTP STARTTLS (smtp.gmail.com:587)", "smtp.gmail.com", 587),
    ],
    "calendar": [
        ("Internet (www.google.com:443)", "www.google.com", 443),
        ("Google sign-in (accounts.google.com:443)", "accounts.google.com", 443),
        ("Google token server (oauth2.googleapis.com:443)", "oauth2.googleapis.com", 443),
        ("Google Calendar API (www.googleapis.com:443)", "www.googleapis.com", 443),
    ],
}

ADVICE = {
    "gmail": ("If both 465 and 587 fail but the internet works, your network blocks outgoing email. "
              "Switch to a mobile hotspot, turn off VPN / antivirus email protection, or use the "
              "'Open in Gmail' button, which works over normal HTTPS."),
    "calendar": ("If the Google hosts fail, your network or VPN is blocking Google APIs. Try a mobile hotspot. "
                 "If they all pass, the problem is the sign-in (credentials.json / test user / expired token). "
                 "You can always use 'Download .ics' instead."),
}


def render(st, kind: str) -> None:
    """Expander with a 'Run check' button. kind = 'gmail' | 'calendar'."""
    with st.expander("🔌 Connection check — use this if sending / scheduling fails or times out"):
        if st.button("Run connection check", key=f"netcheck_{kind}"):
            all_ok = True
            for label, host, port in CHECKS[kind]:
                ok, info = _probe(host, port)
                all_ok &= ok
                st.write(f"{'✅' if ok else '❌'} {label} — {info}")
            if all_ok:
                st.success("Network looks fine. If it still fails, the problem is the login (see the error message).")
            else:
                st.warning(ADVICE[kind])
