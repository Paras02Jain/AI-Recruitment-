"""
calendar_scheduler.py — EduHire Google Calendar Integration
=============================================================
Auto-schedules interview slots for shortlisted candidates on Google Calendar.

BUG FIX (v2):
  Root cause of wrong times: generate_slots() was producing NAIVE datetimes
  (no tzinfo). Calling .isoformat() on a naive datetime gives e.g.
  "2026-04-29T09:00:00" with NO UTC offset, so Google Calendar treated it as
  UTC and displayed 09:00 UTC → 03:30 IST (visible as 3:30 AM on the calendar).
  Fix: all slot datetimes are now timezone-AWARE in the user's chosen timezone
  (default Asia/Kolkata = IST = UTC+05:30). .isoformat() now produces
  "2026-04-29T09:00:00+05:30" which Google Calendar interprets correctly.

Features:
  - OAuth2 flow via credentials.json (desktop flow) with saved token
  - Auto-detects free/busy slots using Calendar API freebusy query
  - Creates calendar events with Google Meet link (conferencing)
  - Sends email invitations directly from Calendar
  - Supports bulk scheduling of all shortlisted candidates
  - Returns event links for display in the UI

Dependencies (add to requirements.txt):
  google-auth>=2.28.0
  google-auth-oauthlib>=1.2.0
  google-api-python-client>=2.120.0
  (zoneinfo is stdlib Python 3.9+; no extra install needed)

Usage:
  scheduler = CalendarScheduler(credentials_json_path="credentials.json")
  results   = scheduler.schedule_interviews(candidates, config)
"""

from __future__ import annotations

import datetime
import json
import os
import pickle
from typing import Any

# -- zoneinfo (stdlib Python 3.9+) -------------------------------------------
try:
    from zoneinfo import ZoneInfo
    _ZONEINFO_AVAILABLE = True
except ImportError:
    try:
        from backports.zoneinfo import ZoneInfo   # pip install backports.zoneinfo
        _ZONEINFO_AVAILABLE = True
    except ImportError:
        _ZONEINFO_AVAILABLE = False
        ZoneInfo = None  # type: ignore

# -- Google API imports (graceful fallback if not installed) ------------------
try:
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
    _GOOGLE_AVAILABLE = True
except ImportError:
    _GOOGLE_AVAILABLE = False

SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/calendar.events",
]

_APP_DIR     = os.path.dirname(os.path.abspath(__file__))
TOKEN_FILE   = os.path.join(_APP_DIR, "calendar_token.pkl")   # always next to the app, whatever folder you launch from
TIMEZONE_IST = "Asia/Kolkata"
_HTTP_TIMEOUT = 30            # seconds for Google API calls (prevents endless hanging)


def _resolve_creds_path(path: str) -> str:
    """Find credentials.json: as typed, relative to the current folder, then next to the app."""
    path = (path or "credentials.json").strip().strip('"')
    for cand in (path, os.path.join(_APP_DIR, path), os.path.join(_APP_DIR, os.path.basename(path))):
        if os.path.isfile(cand):
            return os.path.abspath(cand)
    return path


def explain_google_error(err) -> str:
    """Turn raw Google / network errors into a fix the user can act on."""
    t = str(err)
    low = t.lower()
    if "access_denied" in low or "not completed the google verification" in low or "test user" in low:
        return (t + " → Fix: Google Cloud Console → APIs & Services → OAuth consent screen → Test users → "
                    "add the Gmail you sign in with.")
    if "accessnotconfigured" in low or "has not been used in project" in low or "api has not been enabled" in low:
        return t + " → Fix: enable 'Google Calendar API' in Google Cloud Console → APIs & Services → Library."
    if "invalid_grant" in low or "token has been expired or revoked" in low:
        return (t + " → Fix: delete calendar_token.pkl and sign in again. (Apps in 'Testing' mode "
                    "expire tokens after 7 days.)")
    if "redirect_uri_mismatch" in low or "invalid_client" in low:
        return t + " → Fix: create the OAuth client as type 'Desktop app' and download credentials.json again."
    if "timed out" in low or "timeout" in low or "name or service not known" in low or "connection" in low:
        return (t + " → Network problem: your Wi-Fi / VPN may block Google APIs. Try a mobile hotspot, "
                    "or use 'Download .ics' instead.")
    return t


# =============================================================================
# INTERNAL HELPERS
# =============================================================================

def _get_tz(tz_name: str):
    """
    Return a timezone object for the given IANA name.
    Uses zoneinfo (stdlib 3.9+). Falls back to a fixed UTC+5:30 offset.
    """
    if _ZONEINFO_AVAILABLE and ZoneInfo is not None:
        try:
            return ZoneInfo(tz_name)
        except Exception:
            pass
    # Fallback: fixed IST offset — correct for Asia/Kolkata
    return datetime.timezone(datetime.timedelta(hours=5, minutes=30))


def _localize(dt: datetime.datetime, tz_name: str) -> datetime.datetime:
    """
    Attach timezone info to a naive datetime (makes it timezone-aware).
    If dt is already aware, return it unchanged.

    This is the KEY FIX for the UTC bug: naive datetimes have no UTC offset,
    so Google Calendar treats them as UTC. Attaching the local tz produces
    an isoformat() like "2026-04-29T09:00:00+05:30" which is unambiguous.
    """
    if dt.tzinfo is not None:
        return dt
    return dt.replace(tzinfo=_get_tz(tz_name))


def _duration_label(minutes: int) -> str:
    if minutes < 60:
        return f"{minutes} min"
    h, m = divmod(minutes, 60)
    return f"{h}h {m}min" if m else f"{h}h"


# =============================================================================
# FREE/BUSY CHECKER
# =============================================================================

def check_free_slots(
    service,
    calendar_id: str,
    slots: list[tuple[datetime.datetime, datetime.datetime]],
) -> list[tuple[datetime.datetime, datetime.datetime]]:
    """
    Given a list of timezone-AWARE (start, end) datetime pairs, return only
    those slots that are FREE on the given calendar.
    Uses the Calendar freebusy API (single batch call).
    """
    if not slots:
        return []

    time_min = min(s[0] for s in slots)
    time_max = max(s[1] for s in slots)

    body = {
        "timeMin": time_min.isoformat(),
        "timeMax": time_max.isoformat(),
        "timeZone": TIMEZONE_IST,
        "items": [{"id": calendar_id}],
    }

    try:
        result       = service.freebusy().query(body=body).execute()
        busy_periods = result.get("calendars", {}).get(calendar_id, {}).get("busy", [])
    except Exception as e:
        print(f"[WARN] freebusy query failed: {e}. Treating all slots as free.")
        return slots

    def _is_busy(start: datetime.datetime, end: datetime.datetime) -> bool:
        for b in busy_periods:
            # Google returns busy times in UTC (RFC3339 with Z suffix)
            b_start = datetime.datetime.fromisoformat(b["start"].replace("Z", "+00:00"))
            b_end   = datetime.datetime.fromisoformat(b["end"].replace("Z", "+00:00"))
            # Both start/end are now timezone-aware; Python compares correctly across zones
            if start < b_end and end > b_start:
                return True
        return False

    return [s for s in slots if not _is_busy(s[0], s[1])]


# =============================================================================
# SLOT GENERATOR  (THE CORE FIX IS HERE)
# =============================================================================

def generate_slots(
    start_date: datetime.date,
    num_slots: int,
    duration_minutes: int = 45,
    working_hours: tuple[int, int] = (9, 17),
    skip_weekends: bool = True,
    gap_minutes: int = 15,
    timezone: str = TIMEZONE_IST,
) -> list[tuple[datetime.datetime, datetime.datetime]]:
    """
    Generate `num_slots` TIMEZONE-AWARE interview time slots starting from
    `start_date`, respecting working hours and weekends.

    THE FIX: datetime.combine(..., tzinfo=tz) creates aware datetimes from
    the start. Previously this used naive datetimes which caused Google
    Calendar to misinterpret the times as UTC instead of IST.

    Returns list of (start_dt, end_dt) timezone-aware datetime pairs.
    """
    tz         = _get_tz(timezone)
    slots      = []

    # Create a TIMEZONE-AWARE start datetime (this is the fix)
    current = datetime.datetime.combine(
        start_date,
        datetime.time(working_hours[0], 0),
        tzinfo=tz,            # <-- KEY: attach timezone here
    )

    slot_delta = datetime.timedelta(minutes=duration_minutes + gap_minutes)
    day_end_h  = working_hours[1]

    while len(slots) < num_slots:
        # Skip weekends
        if skip_weekends and current.weekday() >= 5:   # Sat=5, Sun=6
            next_day = current.date() + datetime.timedelta(days=1)
            current  = datetime.datetime.combine(
                next_day,
                datetime.time(working_hours[0], 0),
                tzinfo=tz,    # <-- KEY: keep timezone-aware when advancing days
            )
            continue

        end_dt = current + datetime.timedelta(minutes=duration_minutes)

        # Check if slot would exceed working hours
        if end_dt.hour > day_end_h or (end_dt.hour == day_end_h and end_dt.minute > 0):
            next_day = current.date() + datetime.timedelta(days=1)
            current  = datetime.datetime.combine(
                next_day,
                datetime.time(working_hours[0], 0),
                tzinfo=tz,    # <-- KEY: keep timezone-aware
            )
            continue

        slots.append((current, end_dt))
        current = current + slot_delta

    return slots


# =============================================================================
# CALENDAR EVENT CREATOR
# =============================================================================

def create_interview_event(
    service,
    calendar_id: str,
    candidate_name: str,
    candidate_email: str | None,
    interviewer_name: str,
    interviewer_email: str,
    start_dt: datetime.datetime,
    end_dt: datetime.datetime,
    job_title: str = "Teacher Position",
    school_name: str = "Our School",
    add_meet: bool = True,
    timezone: str = TIMEZONE_IST,
    notes: str = "",
) -> dict:
    """
    Create a Google Calendar event for one interview slot.

    start_dt / end_dt should be timezone-aware datetimes (from generate_slots).
    If naive datetimes are passed, they are localised here as a safety net.

    .isoformat() on an aware datetime now produces:
        "2026-04-29T09:00:00+05:30"   ← correct: Google sees IST, shows 9 AM
    Previously (naive):
        "2026-04-29T09:00:00"          ← wrong:  Google assumed UTC, showed 2:30 AM IST
    """
    # Safety net: localise naive datetimes before calling .isoformat()
    start_dt = _localize(start_dt, timezone)
    end_dt   = _localize(end_dt,   timezone)

    description_parts = [
        f"Interview for: {job_title}",
        f"School: {school_name}",
        f"Candidate: {candidate_name}",
        f"Interviewer: {interviewer_name}",
        "",
        "Please review the candidate's profile before the meeting.",
    ]
    if notes:
        description_parts += ["", f"Notes: {notes}"]

    attendees = [{"email": interviewer_email, "displayName": interviewer_name}]
    if candidate_email:
        attendees.append({"email": candidate_email, "displayName": candidate_name})

    event_body: dict[str, Any] = {
        "summary": f"Interview: {candidate_name} — {job_title}",
        "description": "\n".join(description_parts),
        "start": {
            "dateTime": start_dt.isoformat(),   # e.g. "2026-04-29T09:00:00+05:30"
            "timeZone": timezone,
        },
        "end": {
            "dateTime": end_dt.isoformat(),
            "timeZone": timezone,
        },
        "attendees": attendees,
        "reminders": {
            "useDefault": False,
            "overrides": [
                {"method": "email",  "minutes": 24 * 60},
                {"method": "popup",  "minutes": 30},
            ],
        },
        "status": "confirmed",
    }

    if add_meet:
        event_body["conferenceData"] = {
            "createRequest": {
                "requestId": (
                    f"eduhire-{candidate_name.replace(' ', '-').lower()}"
                    f"-{start_dt.strftime('%Y%m%d%H%M')}"
                ),
                "conferenceSolutionKey": {"type": "hangoutsMeet"},
            }
        }

    try:
        created = service.events().insert(
            calendarId=calendar_id,
            body=event_body,
            conferenceDataVersion=1 if add_meet else 0,
            sendUpdates="all",                       # Google emails every attendee except the organizer
        ).execute()
        return {"success": True, "event": created}
    except HttpError as e:
        error_body = json.loads(e.content.decode()) if e.content else {}
        msg = error_body.get("error", {}).get("message", str(e))
        return {"success": False, "error": explain_google_error(msg)}
    except Exception as e:
        return {"success": False, "error": explain_google_error(e)}


# =============================================================================
# MAIN SCHEDULER CLASS
# =============================================================================

class CalendarScheduler:
    """
    High-level interface for scheduling candidate interviews on Google Calendar.
    Instantiate once per Streamlit session, then call schedule_interviews().
    """

    def __init__(
        self,
        credentials_json_path: str = "credentials.json",
        calendar_id: str = "primary",
    ):
        self.credentials_json_path = credentials_json_path
        self.calendar_id           = calendar_id
        self._service              = None
        self._creds                = None
        self.error                 = None if _GOOGLE_AVAILABLE else (
            "Google API libraries not installed. "
            "Run: pip install google-auth google-auth-oauthlib google-api-python-client"
        )

    # -- Authentication -------------------------------------------------------

    def authenticate(self) -> bool:
        """
        Authenticate using the saved token or the OAuth2 browser sign-in.
        Returns True if successful; otherwise sets self.error with a fix-it hint.
        """
        if not _GOOGLE_AVAILABLE:
            return False

        creds = None
        if os.path.exists(TOKEN_FILE):
            try:
                with open(TOKEN_FILE, "rb") as f:
                    creds = pickle.load(f)
            except Exception:
                creds = None

        if creds and not creds.valid and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception:
                creds = None                       # revoked / expired -> sign in again
                try:
                    os.remove(TOKEN_FILE)
                except OSError:
                    pass

        if not creds or not creds.valid:
            cred_path = _resolve_creds_path(self.credentials_json_path)
            if not os.path.isfile(cred_path):
                self.error = (
                    f"credentials.json not found (looked for '{self.credentials_json_path}' in the current "
                    f"folder and in {_APP_DIR}). Download it from Google Cloud Console → APIs & Services → "
                    "Credentials → OAuth client (Desktop app), and put it next to app.py."
                )
                return False
            try:
                flow = InstalledAppFlow.from_client_secrets_file(cred_path, SCOPES)
                try:
                    creds = flow.run_local_server(port=0, open_browser=True, timeout_seconds=180)
                except TypeError:                  # older google-auth-oauthlib without timeout_seconds
                    creds = flow.run_local_server(port=0, open_browser=True)
            except Exception as e:
                self.error = "Google sign-in failed: " + explain_google_error(e)
                return False
            try:
                with open(TOKEN_FILE, "wb") as f:
                    pickle.dump(creds, f)
            except Exception:
                pass

        try:
            try:
                import httplib2
                import google_auth_httplib2
                http = google_auth_httplib2.AuthorizedHttp(creds, http=httplib2.Http(timeout=_HTTP_TIMEOUT))
                self._service = build("calendar", "v3", http=http, cache_discovery=False)
            except ImportError:
                self._service = build("calendar", "v3", credentials=creds, cache_discovery=False)
            self._creds = creds
            self.error  = None
            return True
        except Exception as e:
            self.error = "Could not start the Calendar service: " + explain_google_error(e)
            return False

    @property
    def is_authenticated(self) -> bool:
        return self._service is not None and self.error is None

    def get_calendar_list(self) -> list[dict]:
        if not self.is_authenticated:
            return []
        try:
            result = self._service.calendarList().list().execute()
            return result.get("items", [])
        except Exception:
            return []

    # -- Bulk scheduler -------------------------------------------------------

    def schedule_interviews(
        self,
        candidates: list[dict],
        config: dict,
    ) -> list[dict]:
        """
        Schedule interview slots for all shortlisted candidates.

        config keys:
          start_date, duration_minutes, working_hours, skip_weekends,
          gap_minutes, interviewer_name, interviewer_email, school_name,
          job_title, add_meet, check_availability, timezone

        Returns list of result dicts, one per candidate.
        """
        if not self.is_authenticated:
            return [{"name": c.get("name", "?"), "success": False,
                     "error": self.error or "Not authenticated"} for c in candidates]

        start_date        = config.get("start_date",        datetime.date.today() + datetime.timedelta(days=1))
        duration_minutes  = config.get("duration_minutes",  45)
        working_hours     = config.get("working_hours",     (9, 17))
        skip_weekends     = config.get("skip_weekends",     True)
        gap_minutes       = config.get("gap_minutes",       15)
        interviewer_name  = config.get("interviewer_name",  "Hiring Manager")
        interviewer_email = config.get("interviewer_email", "")
        school_name       = config.get("school_name",       "Our School")
        job_title         = config.get("job_title",         "Teacher Position")
        add_meet          = config.get("add_meet",          True)
        check_avail       = config.get("check_availability",True)
        timezone          = config.get("timezone",          TIMEZONE_IST)

        n_candidates = len(candidates)
        buffer_size  = max(n_candidates * 2, n_candidates + 5)

        raw_slots = generate_slots(
            start_date       = start_date,
            num_slots        = buffer_size,
            duration_minutes = duration_minutes,
            working_hours    = working_hours,
            skip_weekends    = skip_weekends,
            gap_minutes      = gap_minutes,
            timezone         = timezone,      # <-- timezone passed through
        )

        free_slots = (
            check_free_slots(self._service, self.calendar_id, raw_slots)
            if check_avail else raw_slots
        )

        if len(free_slots) < n_candidates:
            free_slots = raw_slots

        free_slots = free_slots[:n_candidates]

        results = []

        for i, candidate in enumerate(candidates):
            name  = candidate.get("name",  f"Candidate {i+1}")
            email = candidate.get("email", "")

            if i >= len(free_slots):
                results.append({"name": name, "email": email,
                                 "success": False, "error": "No available slot found"})
                continue

            slot_start, slot_end = free_slots[i]

            result = create_interview_event(
                service           = self._service,
                calendar_id       = self.calendar_id,
                candidate_name    = name,
                candidate_email   = email or None,
                interviewer_name  = interviewer_name,
                interviewer_email = interviewer_email,
                start_dt          = slot_start,
                end_dt            = slot_end,
                job_title         = job_title,
                school_name       = school_name,
                add_meet          = add_meet,
                timezone          = timezone,
            )

            if result["success"]:
                ev        = result["event"]
                meet_link = ""
                for ep in ev.get("conferenceData", {}).get("entryPoints", []):
                    if ep.get("entryPointType") == "video":
                        meet_link = ep.get("uri", "")
                        break

                results.append({
                    "name":       name,
                    "email":      email,
                    "start":      slot_start,
                    "end":        slot_end,
                    "event_link": ev.get("htmlLink", ""),
                    "meet_link":  meet_link,
                    "event_id":   ev.get("id", ""),
                    "success":    True,
                    "error":      "",
                })
            else:
                results.append({
                    "name":    name,
                    "email":   email,
                    "start":   slot_start,
                    "end":     slot_end,
                    "success": False,
                    "error":   result.get("error", "Unknown error"),
                })

        return results

    # -- Single-candidate convenience -----------------------------------------

    def schedule_one(
        self,
        candidate_name: str,
        candidate_email: str,
        slot_start: datetime.datetime,
        slot_end: datetime.datetime,
        config: dict,
    ) -> dict:
        """Schedule a single candidate at a manually chosen slot."""
        if not self.is_authenticated:
            return {"success": False, "error": self.error or "Not authenticated"}

        tz = config.get("timezone", TIMEZONE_IST)
        return create_interview_event(
            service           = self._service,
            calendar_id       = self.calendar_id,
            candidate_name    = candidate_name,
            candidate_email   = candidate_email or None,
            interviewer_name  = config.get("interviewer_name",  "Hiring Manager"),
            interviewer_email = config.get("interviewer_email", ""),
            start_dt          = _localize(slot_start, tz),
            end_dt            = _localize(slot_end,   tz),
            job_title         = config.get("job_title",  "Teacher Position"),
            school_name       = config.get("school_name","Our School"),
            add_meet          = config.get("add_meet",   True),
            timezone          = tz,
        )


# =============================================================================
# AVAILABILITY PREVIEW (no events created)
# =============================================================================

def preview_slots(
    service,
    calendar_id: str,
    config: dict,
    num_candidates: int,
) -> list[dict]:
    """
    Preview available slots without creating events.
    Returns list of {start, end, available} dicts (datetimes timezone-aware).
    """
    tz = config.get("timezone", TIMEZONE_IST)
    raw_slots  = generate_slots(
        start_date       = config.get("start_date",       datetime.date.today() + datetime.timedelta(days=1)),
        num_slots        = num_candidates * 2,
        duration_minutes = config.get("duration_minutes", 45),
        working_hours    = config.get("working_hours",    (9, 17)),
        skip_weekends    = config.get("skip_weekends",    True),
        gap_minutes      = config.get("gap_minutes",      15),
        timezone         = tz,
    )
    free_slots = check_free_slots(service, calendar_id, raw_slots)
    free_set   = set((s, e) for s, e in free_slots)

    return [
        {"start": s, "end": e, "available": (s, e) in free_set}
        for s, e in raw_slots[:num_candidates * 2]
    ]


# =============================================================================
# OFFLINE FALLBACK: .ics calendar file (no Google sign-in, no API, no network)
# =============================================================================

def _ics_escape(text: str) -> str:
    return (str(text or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
            .replace("\r\n", "\\n").replace("\n", "\\n"))


def _ics_fold(line: str) -> str:
    """RFC 5545: lines longer than 75 octets are folded with CRLF + space."""
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return line
    out, cur = [], b""
    for ch in line:
        b = ch.encode("utf-8")
        if len(cur) + len(b) > (75 if not out else 74):
            out.append(cur.decode("utf-8"))
            cur = b
        else:
            cur += b
    out.append(cur.decode("utf-8"))
    return "\r\n ".join(out)


def generate_ics(candidates: list[dict], config: dict) -> tuple[str, list[dict]]:
    """
    Build a standards-compliant .ics file with one interview per candidate.
    Same slot rules as the Google scheduler (working hours, weekends, gap) but it cannot check
    your existing calendar. Open the file in Google Calendar / Outlook / Apple Calendar.
    Returns (ics_text, rows) where rows = [{"name","email","start","end"}].
    """
    import uuid
    tzname   = config.get("timezone", TIMEZONE_IST)
    slots    = generate_slots(
        start_date       = config.get("start_date", datetime.date.today() + datetime.timedelta(days=1)),
        num_slots        = max(len(candidates), 1),
        duration_minutes = config.get("duration_minutes", 45),
        working_hours    = config.get("working_hours", (9, 17)),
        skip_weekends    = config.get("skip_weekends", True),
        gap_minutes      = config.get("gap_minutes", 15),
        timezone         = tzname,
    )
    utc      = datetime.timezone.utc
    stamp    = datetime.datetime.now(utc).strftime("%Y%m%dT%H%M%SZ")
    school   = config.get("school_name", "Our School")
    job      = config.get("job_title", "Teacher Position")
    org_name = config.get("interviewer_name", "Hiring Manager")
    org_mail = config.get("interviewer_email", "")

    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//EduHire//Interview Scheduler//EN",
             "CALSCALE:GREGORIAN", "METHOD:PUBLISH"]
    rows = []
    for cand, (start, end) in zip(candidates, slots):
        name, mail = cand.get("name", "Candidate"), (cand.get("email") or "").strip()
        desc = (f"Interview for {job} at {school}.\nCandidate: {name}\nInterviewer: {org_name}\n"
                f"Please join 5 minutes early and keep your ID and certificates ready.")
        ev = ["BEGIN:VEVENT", f"UID:{uuid.uuid4()}@eduhire", f"DTSTAMP:{stamp}",
              f"DTSTART:{start.astimezone(utc).strftime('%Y%m%dT%H%M%SZ')}",
              f"DTEND:{end.astimezone(utc).strftime('%Y%m%dT%H%M%SZ')}",
              f"SUMMARY:{_ics_escape(f'Interview – {name} ({job}, {school})')}",
              f"DESCRIPTION:{_ics_escape(desc)}", "STATUS:CONFIRMED"]
        if org_mail:
            ev.append(f"ORGANIZER;CN={_ics_escape(org_name)}:mailto:{org_mail}")
        if mail and "@" in mail:
            ev.append(f"ATTENDEE;CN={_ics_escape(name)};RSVP=TRUE:mailto:{mail}")
        ev.append("END:VEVENT")
        lines += ev
        rows.append({"name": name, "email": mail, "start": start, "end": end})
    lines.append("END:VCALENDAR")
    return "\r\n".join(_ics_fold(l) for l in lines) + "\r\n", rows


# =============================================================================
# INTERVIEWER SUMMARY EMAIL (Google never emails the organizer about their own events)
# =============================================================================

def build_interview_summary(results: list[dict], config: dict) -> tuple[str, str, str]:
    """Returns (subject, plain_text, html) listing every scheduled interview with Meet / Calendar links."""
    school = config.get("school_name", "Our School")
    job    = config.get("job_title", "Teacher Position")
    who    = config.get("interviewer_name", "Hiring Manager")
    ok     = [r for r in results if r.get("success")]
    bad    = [r for r in results if not r.get("success")]
    fmt    = lambda d: d.strftime("%a %d %b %Y, %I:%M %p") if d else "—"
    subject = f"[{school}] {len(ok)} interview(s) scheduled for {job}"

    plain = [f"Hello {who},", "", f"{len(ok)} interview(s) were scheduled for {job} at {school} (IST).", ""]
    rows = []
    for r in ok:
        plain.append(f"- {r['name']}: {fmt(r.get('start'))} to {r['end'].strftime('%I:%M %p') if r.get('end') else '—'}")
        if r.get("meet_link"):  plain.append(f"    Meet: {r['meet_link']}")
        if r.get("event_link"): plain.append(f"    Calendar: {r['event_link']}")
        rows.append(
            f"<tr><td style='padding:8px 12px;border-bottom:1px solid #e5e7eb'><b>{r['name']}</b></td>"
            f"<td style='padding:8px 12px;border-bottom:1px solid #e5e7eb'>{fmt(r.get('start'))}</td>"
            f"<td style='padding:8px 12px;border-bottom:1px solid #e5e7eb'>"
            + (f"<a href='{r['meet_link']}'>Join Meet</a> · " if r.get("meet_link") else "")
            + (f"<a href='{r['event_link']}'>Open in Calendar</a>" if r.get("event_link") else "") + "</td></tr>")
    if bad:
        plain += ["", "Could not schedule:"] + [f"- {r['name']}: {r.get('error', 'unknown error')}" for r in bad]
    plain += ["", "— Sent by EduHire"]
    html = (f"<div style='font-family:Arial,sans-serif;color:#111'><p>Hello {who},</p>"
            f"<p><b>{len(ok)}</b> interview(s) were scheduled for <b>{job}</b> at <b>{school}</b> (IST).</p>"
            f"<table style='border-collapse:collapse;font-size:14px'>"
            f"<tr style='background:#f3f4f6'><th align='left' style='padding:8px 12px'>Candidate</th>"
            f"<th align='left' style='padding:8px 12px'>Time</th><th align='left' style='padding:8px 12px'>Links</th></tr>"
            + "".join(rows) + "</table>"
            + (("<p style='color:#b91c1c'>Could not schedule: " + ", ".join(f"{r['name']} ({r.get('error','error')})" for r in bad) + "</p>") if bad else "")
            + "<p style='color:#6b7280;font-size:12px'>Sent by EduHire</p></div>")
    return subject, "\n".join(plain), html
