"""
database.py — EduHire Persistence Layer (SQLite)
=================================================
Adds permanent storage to EduHire. Uses only Python's built-in sqlite3,
so no new dependency and no server to install. Data lives in `eduhire.db`
next to app.py.

Tables
------
screening_runs   one row per "Screen Candidates" click (JD, config, KPIs)
candidates       the TALENT POOL: one row per unique person (dedup by resume hash)
run_results      a candidate's score/rank/filter outcome inside one run
pipeline         current hiring stage per candidate per run
stage_history    audit trail of every stage move
notes            call / meeting notes
fairness_runs    fairness-audit snapshot per run (for trend tracking)
email_log        record of emails sent

Design rules
------------
* Every public function swallows DB errors and returns a safe default, so a
  database problem can never crash the Streamlit app.
* JSON-ish fields are stored as TEXT (json.dumps) to keep the schema simple.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import sqlite3
from contextlib import contextmanager
from typing import Any, Optional

DB_PATH = os.environ.get(
    "EDUHIRE_DB_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "eduhire.db"),
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS screening_runs (
    run_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at      TEXT NOT NULL,
    role_title      TEXT,
    school_name     TEXT,
    jd_text         TEXT,
    jd_hash         TEXT,
    config_json     TEXT,
    scoring_method  TEXT,
    total_screened  INTEGER,
    qualified_count INTEGER,
    filtered_count  INTEGER,
    top_score       REAL,
    avg_score       REAL,
    bias_risk       TEXT
);

CREATE TABLE IF NOT EXISTS candidates (
    candidate_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    resume_hash   TEXT UNIQUE,
    name          TEXT,
    email         TEXT,
    phone         TEXT,
    resume_text   TEXT,
    exp_years     INTEGER,
    has_degree    INTEGER,
    has_cert      INTEGER,
    is_premier    INTEGER,
    skills_json   TEXT,
    first_seen    TEXT,
    last_seen     TEXT,
    times_screened INTEGER DEFAULT 1,
    status        TEXT DEFAULT 'active'      -- active | hired | do_not_contact
);

CREATE TABLE IF NOT EXISTS run_results (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id         INTEGER NOT NULL REFERENCES screening_runs(run_id) ON DELETE CASCADE,
    candidate_id   INTEGER NOT NULL REFERENCES candidates(candidate_id) ON DELETE CASCADE,
    cand_key       TEXT NOT NULL,
    score          REAL,
    score_method   TEXT,
    rank           INTEGER,
    filtered       INTEGER,
    fail_reasons   TEXT,
    matched_kw     TEXT,
    missing_kw     TEXT,
    ats_json       TEXT,
    justification  TEXT,
    strengths_json TEXT,
    red_flags_json TEXT,
    UNIQUE(run_id, cand_key)
);

CREATE TABLE IF NOT EXISTS pipeline (
    run_id        INTEGER NOT NULL REFERENCES screening_runs(run_id) ON DELETE CASCADE,
    cand_key      TEXT NOT NULL,
    candidate_id  INTEGER NOT NULL,
    stage         TEXT NOT NULL,
    last_contact  TEXT,
    PRIMARY KEY (run_id, cand_key)
);

CREATE TABLE IF NOT EXISTS stage_history (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id    INTEGER NOT NULL,
    cand_key  TEXT NOT NULL,
    stage     TEXT NOT NULL,
    ts        TEXT NOT NULL,
    note      TEXT
);

CREATE TABLE IF NOT EXISTS notes (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id    INTEGER NOT NULL,
    cand_key  TEXT NOT NULL,
    ts        TEXT NOT NULL,
    author    TEXT,
    text      TEXT
);

CREATE TABLE IF NOT EXISTS fairness_runs (
    run_id            INTEGER PRIMARY KEY REFERENCES screening_runs(run_id) ON DELETE CASCADE,
    created_at        TEXT,
    premier_pass_rate REAL,
    nonpremier_pass_rate REAL,
    gap_points        REAL,
    flags_count       INTEGER,
    audit_json        TEXT
);

CREATE TABLE IF NOT EXISTS email_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id     INTEGER,
    cand_key   TEXT,
    to_email   TEXT,
    kind       TEXT,           -- shortlist | rejection | stage_change
    subject    TEXT,
    sent_at    TEXT,
    success    INTEGER
);

CREATE TABLE IF NOT EXISTS app_settings (
    key    TEXT PRIMARY KEY,
    value  TEXT
);

CREATE INDEX IF NOT EXISTS idx_results_run   ON run_results(run_id);
CREATE INDEX IF NOT EXISTS idx_results_cand  ON run_results(candidate_id);
CREATE INDEX IF NOT EXISTS idx_hist_cand     ON stage_history(run_id, cand_key);
CREATE INDEX IF NOT EXISTS idx_notes_cand    ON notes(run_id, cand_key);
"""


# ── Connection helpers ────────────────────────────────────────────────────────

@contextmanager
def _conn():
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def init_db() -> bool:
    """Create tables if missing. Safe to call on every app start."""
    try:
        with _conn() as con:
            con.executescript(_SCHEMA)
        return True
    except Exception:
        return False


def db_available() -> bool:
    try:
        with _conn() as con:
            con.execute("SELECT 1")
        return True
    except Exception:
        return False


def _hash_text(text: str) -> str:
    norm = re.sub(r"\s+", " ", (text or "").strip().lower())
    return hashlib.sha256(norm.encode("utf-8", "ignore")).hexdigest()


_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
# Indian mobile: optional +91 / 91 / 0, then 10 digits starting 6-9, allowing one space/dash in the middle
_PHONE_RE = re.compile(r"(?<!\d)(?:\+?91[\s.-]?|0)?([6-9]\d{4}[\s.-]?\d{5})(?!\d)")


def _extract_contact(text: str) -> tuple[str, str]:
    """Best-effort email + phone from resume text. Returns ('', '') if not present."""
    t = (text or "")[:6000]
    t = re.sub(r"\s*[\[(]\s*at\s*[\])]\s*", "@", t, flags=re.I)          # name [at] mail.com
    t = re.sub(r"\s*[\[(]\s*dot\s*[\])]\s*", ".", t, flags=re.I)
    t = re.sub(r"(?i)mailto:", " ", t)
    m_e = _EMAIL_RE.search(t)
    m_p = _PHONE_RE.search(t)
    phone = re.sub(r"[\s.-]", "", m_p.group(1)) if m_p else ""
    return (m_e.group(0).lower() if m_e else ""), phone


def guess_role_title(jd_text: str, default: str = "Open Position") -> str:
    """Pull a sensible role title out of a pasted JD ('hiring a Mathematics Teacher for ...')."""
    text = (jd_text or "").strip()
    if not text:
        return default
    m = re.search(r"(?i:hiring|seeking|looking for|recruiting|vacancy for|opening for)\s+(?:an?\s+|the\s+)?"
                  r"([A-Z][\w&/ -]{3,60}?)(?:\s+(?:for|to|at|in|who|with)\b|[.,:;\n]|$)", text)
    if m:
        return m.group(1).strip()[:80]
    kw = re.compile(r"teacher|lecturer|professor|faculty|coordinator|principal|manager|executive|"
                    r"assistant|counsel+or|instructor|trainer|officer|analyst|engineer", re.I)
    raw = [ln.strip() for ln in text.splitlines() if ln.strip()]
    lines = [ln for ln in raw if not ln.endswith(":") and not ln.startswith(("-", "•", "*")) and 4 <= len(ln) <= 80]
    for ln in lines:
        if kw.search(ln):
            return ln
    return lines[0] if lines else default


# ── Writing: screening runs ──────────────────────────────────────────────────

def save_screening_run(
    results: list[dict],
    jd_text: str,
    config: dict,
    role_title: str = "",
    school_name: str = "",
    bias_risk: str = "",
    fairness_audit: Optional[dict] = None,
) -> Optional[int]:
    """
    Persist one full screening run + upsert every candidate into the talent pool.
    Returns run_id (or None if the DB is unavailable).
    """
    try:
        init_db()
        qualified = [r for r in results if not r.get("filtered")]
        top = max((r["score"] for r in qualified), default=0)
        avg = (sum(r["score"] for r in qualified) / len(qualified)) if qualified else 0
        method = results[0].get("score_method", "") if results else ""

        with _conn() as con:
            cur = con.execute(
                """INSERT INTO screening_runs
                   (created_at, role_title, school_name, jd_text, jd_hash, config_json,
                    scoring_method, total_screened, qualified_count, filtered_count,
                    top_score, avg_score, bias_risk)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (_now(), role_title, school_name, jd_text, _hash_text(jd_text),
                 json.dumps(config), method, len(results), len(qualified),
                 len(results) - len(qualified), top, avg, bias_risk),
            )
            run_id = cur.lastrowid

            for r in results:
                text = r.get("text", "") or ""
                rh = _hash_text(text) if text else _hash_text(f"{r.get('name')}|{r.get('upload_pos')}")
                email, phone = _extract_contact(text)
                now = _now()

                row = con.execute(
                    "SELECT candidate_id FROM candidates WHERE resume_hash=?", (rh,)
                ).fetchone()
                if not row and text:
                    # Migration: older rows were saved without resume text. Upgrade that
                    # same person in place (keeps history) instead of creating a duplicate.
                    old = con.execute(
                        "SELECT candidate_id FROM candidates "
                        "WHERE name=? AND COALESCE(resume_text,'')='' ORDER BY candidate_id LIMIT 1",
                        (r.get("name", "Unknown"),)).fetchone()
                    if old:
                        con.execute("UPDATE candidates SET resume_hash=?, resume_text=? WHERE candidate_id=?",
                                    (rh, text, old["candidate_id"]))
                        row = old
                if row:
                    cid = row["candidate_id"]
                    con.execute(
                        """UPDATE candidates SET last_seen=?, times_screened=times_screened+1,
                           exp_years=?, has_degree=?, has_cert=?, is_premier=?, skills_json=?
                           WHERE candidate_id=?""",
                        (now, r.get("exp", 0), int(bool(r.get("has_deg"))),
                         int(bool(r.get("has_cert"))), int(bool(r.get("is_premier"))),
                         json.dumps(r.get("tags", [])), cid),
                    )
                    con.execute(
                        """UPDATE candidates SET
                           email = CASE WHEN COALESCE(email,'')='' THEN ? ELSE email END,
                           phone = CASE WHEN COALESCE(phone,'')='' THEN ? ELSE phone END
                           WHERE candidate_id=?""", (email, phone, cid))
                else:
                    cur2 = con.execute(
                        """INSERT INTO candidates
                           (resume_hash, name, email, phone, resume_text, exp_years,
                            has_degree, has_cert, is_premier, skills_json, first_seen, last_seen)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (rh, r.get("name", "Unknown"), email, phone, text, r.get("exp", 0),
                         int(bool(r.get("has_deg"))), int(bool(r.get("has_cert"))),
                         int(bool(r.get("is_premier"))), json.dumps(r.get("tags", [])),
                         now, now),
                    )
                    cid = cur2.lastrowid

                cand_key = f"{r.get('name', 'unknown')}_{r.get('upload_pos', 0)}"
                con.execute(
                    """INSERT OR REPLACE INTO run_results
                       (run_id, candidate_id, cand_key, score, score_method, rank, filtered,
                        fail_reasons, matched_kw, missing_kw, ats_json, justification,
                        strengths_json, red_flags_json)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (run_id, cid, cand_key, r.get("score", 0), r.get("score_method", ""),
                     r.get("rank"), int(bool(r.get("filtered"))),
                     json.dumps(r.get("fail_reasons", [])),
                     json.dumps(r.get("matched_kw", [])), json.dumps(r.get("missing_kw", [])),
                     json.dumps(r.get("ats_breakdown", {}), default=str),
                     r.get("justification", ""),
                     json.dumps(r.get("groq_strengths", [])),
                     json.dumps(r.get("groq_red_flags", []))),
                )
                stage = "Rejected" if r.get("filtered") else "Screened"
                con.execute(
                    "INSERT OR REPLACE INTO pipeline VALUES (?,?,?,?,?)",
                    (run_id, cand_key, cid, stage, now),
                )
                con.execute(
                    "INSERT INTO stage_history (run_id,cand_key,stage,ts,note) VALUES (?,?,?,?,?)",
                    (run_id, cand_key, stage, now, "Auto-initialised from screening"),
                )

            if fairness_audit:
                _save_fairness(con, run_id, fairness_audit)
        return run_id
    except Exception:
        return None


def _save_fairness(con, run_id: int, audit: dict) -> None:
    tiers = audit.get("institution_tier") or {}
    prem = tiers.get("Premier", {}) if isinstance(tiers, dict) else {}
    non = tiers.get("Non-Premier", {}) if isinstance(tiers, dict) else {}
    pp = prem.get("pass_rate")
    npp = non.get("pass_rate")
    gap = abs(pp - npp) if isinstance(pp, (int, float)) and isinstance(npp, (int, float)) else None
    con.execute(
        "INSERT OR REPLACE INTO fairness_runs VALUES (?,?,?,?,?,?,?)",
        (run_id, _now(), pp, npp, gap, len(audit.get("disparities", []) or []),
         json.dumps(audit, default=str)),
    )


def save_fairness_audit(run_id: int, audit: dict) -> None:
    try:
        with _conn() as con:
            _save_fairness(con, run_id, audit)
    except Exception:
        pass


# ── Writing: pipeline events ─────────────────────────────────────────────────

def update_stage(run_id: int, cand_key: str, new_stage: str, note: str = "") -> None:
    try:
        now = _now()
        with _conn() as con:
            con.execute(
                "UPDATE pipeline SET stage=?, last_contact=? WHERE run_id=? AND cand_key=?",
                (new_stage, now, run_id, cand_key),
            )
            con.execute(
                "INSERT INTO stage_history (run_id,cand_key,stage,ts,note) VALUES (?,?,?,?,?)",
                (run_id, cand_key, new_stage, now, note or f"Moved to {new_stage}"),
            )
            if new_stage == "Offer":
                con.execute(
                    """UPDATE candidates SET status='hired' WHERE candidate_id=
                       (SELECT candidate_id FROM pipeline WHERE run_id=? AND cand_key=?)""",
                    (run_id, cand_key),
                )
    except Exception:
        pass


def add_note_db(run_id: int, cand_key: str, text: str, author: str = "Recruiter") -> None:
    try:
        now = _now()
        with _conn() as con:
            con.execute(
                "INSERT INTO notes (run_id,cand_key,ts,author,text) VALUES (?,?,?,?,?)",
                (run_id, cand_key, now, author or "Recruiter", text),
            )
            con.execute(
                "UPDATE pipeline SET last_contact=? WHERE run_id=? AND cand_key=?",
                (now, run_id, cand_key),
            )
    except Exception:
        pass


def log_email(run_id: Optional[int], cand_key: str, to_email: str, kind: str,
              subject: str, success: bool) -> None:
    if not run_id:                 # run was not saved -> do not store candidate emails either
        return
    try:
        with _conn() as con:
            con.execute(
                "INSERT INTO email_log (run_id,cand_key,to_email,kind,subject,sent_at,success) "
                "VALUES (?,?,?,?,?,?,?)",
                (run_id, cand_key, to_email, kind, subject, _now(), int(success)),
            )
    except Exception:
        pass


# ── Reading: restore a run into session_state ────────────────────────────────

def load_pipeline_state(run_id: int) -> dict:
    """Rebuild the exact dict shape pipeline_tracker expects in st.session_state['pipeline']."""
    out: dict[str, dict] = {}
    try:
        with _conn() as con:
            rows = con.execute(
                """SELECT p.cand_key, p.stage, p.last_contact, c.name,
                          rr.score, rr.rank, rr.filtered
                   FROM pipeline p
                   JOIN candidates c ON c.candidate_id = p.candidate_id
                   JOIN run_results rr ON rr.run_id=p.run_id AND rr.cand_key=p.cand_key
                   WHERE p.run_id=?""", (run_id,)).fetchall()
            for r in rows:
                key = r["cand_key"]
                hist = con.execute(
                    "SELECT stage, ts, note FROM stage_history WHERE run_id=? AND cand_key=? ORDER BY id",
                    (run_id, key)).fetchall()
                notes = con.execute(
                    "SELECT text, ts, author FROM notes WHERE run_id=? AND cand_key=? ORDER BY id",
                    (run_id, key)).fetchall()
                out[key] = {
                    "stage": r["stage"],
                    "history": [dict(h) for h in hist],
                    "notes": [dict(n) for n in notes],
                    "last_contact": r["last_contact"],
                    "name": r["name"],
                    "score": r["score"],
                    "filtered": bool(r["filtered"]),
                    "rank": r["rank"],
                }
    except Exception:
        pass
    return out


def load_run_results(run_id: int) -> list[dict]:
    """Rebuild the `results` list (same keys backend.ResumeScreener.screen returns)."""
    results: list[dict] = []
    try:
        with _conn() as con:
            rows = con.execute(
                """SELECT rr.*, c.name, c.exp_years, c.has_degree, c.has_cert, c.is_premier,
                          c.skills_json, c.resume_text
                   FROM run_results rr JOIN candidates c ON c.candidate_id = rr.candidate_id
                   WHERE rr.run_id=?
                   ORDER BY rr.filtered, COALESCE(rr.rank, 9999), rr.score DESC""",
                (run_id,)).fetchall()
            for i, r in enumerate(rows, start=1):
                pos = 0
                m = re.search(r"_(\d+)$", r["cand_key"])
                if m:
                    pos = int(m.group(1))
                results.append({
                    "name": r["name"], "score": r["score"], "score_method": r["score_method"],
                    "filtered": bool(r["filtered"]), "tags": json.loads(r["skills_json"] or "[]"),
                    "exp": r["exp_years"], "has_deg": bool(r["has_degree"]),
                    "has_cert": bool(r["has_cert"]), "is_premier": bool(r["is_premier"]),
                    "matched_kw": json.loads(r["matched_kw"] or "[]"),
                    "missing_kw": json.loads(r["missing_kw"] or "[]"),
                    "fail_reasons": json.loads(r["fail_reasons"] or "[]"),
                    "justification": r["justification"] or "", "upload_pos": pos,
                    "rank": r["rank"], "text": r["resume_text"] or "",
                    "ats_breakdown": json.loads(r["ats_json"] or "{}"),
                    "groq_strengths": json.loads(r["strengths_json"] or "[]"),
                    "groq_red_flags": json.loads(r["red_flags_json"] or "[]"),
                    "groq_reasoning": "",
                })
    except Exception:
        pass
    return results


def get_run(run_id: int) -> Optional[dict]:
    try:
        with _conn() as con:
            r = con.execute("SELECT * FROM screening_runs WHERE run_id=?", (run_id,)).fetchone()
            return dict(r) if r else None
    except Exception:
        return None


# ── Reading: dashboards / history ────────────────────────────────────────────

def list_runs(limit: int = 50) -> list[dict]:
    try:
        with _conn() as con:
            rows = con.execute(
                "SELECT run_id, created_at, role_title, school_name, total_screened, "
                "qualified_count, filtered_count, top_score, avg_score, scoring_method, bias_risk, "
                "(SELECT COUNT(*) FROM run_results rr WHERE rr.run_id=screening_runs.run_id) AS stored "
                "FROM screening_runs ORDER BY run_id DESC LIMIT ?", (limit,)).fetchall()
            return [dict(r) for r in rows]
    except Exception:
        return []


def search_talent_pool(query: str = "", min_exp: int = 0, only_cert: bool = False,
                       only_degree: bool = False, only_premier: bool = False,
                       skill: str = "", status: str = "", limit: int = 200) -> list[dict]:
    """Search every candidate ever screened — the reusable talent pool."""
    sql = """SELECT c.candidate_id, c.name, c.email, c.phone, c.exp_years, c.has_degree,
                    c.has_cert, c.is_premier, c.skills_json, c.first_seen, c.last_seen,
                    c.times_screened, c.status,
                    (SELECT MAX(score) FROM run_results WHERE candidate_id=c.candidate_id) AS best_score
             FROM candidates c WHERE c.exp_years >= ?"""
    args: list[Any] = [min_exp]
    if query:
        sql += " AND (c.name LIKE ? OR c.resume_text LIKE ?)"
        args += [f"%{query}%", f"%{query}%"]
    if skill:
        sql += " AND c.skills_json LIKE ?"
        args.append(f"%{skill}%")
    if only_cert:
        sql += " AND c.has_cert=1"
    if only_degree:
        sql += " AND c.has_degree=1"
    if only_premier:
        sql += " AND c.is_premier=1"
    if status:
        sql += " AND c.status=?"
        args.append(status)
    sql += " ORDER BY best_score DESC, c.last_seen DESC LIMIT ?"
    args.append(limit)
    try:
        with _conn() as con:
            return [dict(r) for r in con.execute(sql, args).fetchall()]
    except Exception:
        return []


def candidate_history(candidate_id: int) -> list[dict]:
    """Every run this person appeared in (re-applicant tracking)."""
    try:
        with _conn() as con:
            rows = con.execute(
                """SELECT sr.run_id, sr.created_at, sr.role_title, rr.score, rr.rank,
                          rr.filtered, p.stage
                   FROM run_results rr
                   JOIN screening_runs sr ON sr.run_id = rr.run_id
                   LEFT JOIN pipeline p ON p.run_id=rr.run_id AND p.cand_key=rr.cand_key
                   WHERE rr.candidate_id=? ORDER BY sr.run_id DESC""", (candidate_id,)).fetchall()
            return [dict(r) for r in rows]
    except Exception:
        return []


def fairness_trend() -> list[dict]:
    """Longitudinal fairness data across all hiring cycles (Recommendation 4)."""
    try:
        with _conn() as con:
            rows = con.execute(
                """SELECT f.run_id, sr.created_at, sr.role_title, f.premier_pass_rate,
                          f.nonpremier_pass_rate, f.gap_points, f.flags_count, f.audit_json
                   FROM fairness_runs f JOIN screening_runs sr ON sr.run_id=f.run_id
                   ORDER BY f.run_id""").fetchall()
            return [dict(r) for r in rows]
    except Exception:
        return []


def funnel_summary() -> dict:
    """Overall hiring funnel across all runs: how many candidates sit in each stage."""
    try:
        with _conn() as con:
            rows = con.execute("SELECT stage, COUNT(*) n FROM pipeline GROUP BY stage").fetchall()
            return {r["stage"]: r["n"] for r in rows}
    except Exception:
        return {}


def db_stats() -> dict:
    try:
        with _conn() as con:
            q = lambda s: con.execute(s).fetchone()[0]
            return {
                "runs": q("SELECT COUNT(*) FROM screening_runs"),
                "candidates": q("SELECT COUNT(*) FROM candidates"),
                "repeat_applicants": q("SELECT COUNT(*) FROM candidates WHERE times_screened>1"),
                "notes": q("SELECT COUNT(*) FROM notes"),
                "emails": q("SELECT COUNT(*) FROM email_log"),
                "size_kb": round(os.path.getsize(DB_PATH) / 1024, 1) if os.path.exists(DB_PATH) else 0,
            }
    except Exception:
        return {"runs": 0, "candidates": 0, "repeat_applicants": 0, "notes": 0, "emails": 0, "size_kb": 0}


# ── Settings & data retention (basic privacy controls) ───────────────────────

RETENTION_OPTIONS = {"Never (keep until I delete)": 0, "30 days": 30, "60 days": 60,
                     "90 days": 90, "180 days": 180, "1 year": 365}
DEFAULT_RETENTION_DAYS = 60          # used until someone saves a different period on the Privacy tab


def get_setting(key: str, default: str = "") -> str:
    try:
        with _conn() as con:
            r = con.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
            return r["value"] if r else default
    except Exception:
        return default


def set_setting(key: str, value: str) -> None:
    try:
        with _conn() as con:
            con.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES (?,?)", (key, str(value)))
    except Exception:
        pass


def get_retention_days() -> int:
    """Days after which candidate data is auto-deleted. 0 = never."""
    try:
        return int(get_setting("retention_days", str(DEFAULT_RETENTION_DAYS)))
    except ValueError:
        return DEFAULT_RETENTION_DAYS


def set_retention_days(days: int) -> None:
    set_setting("retention_days", int(days))


def _wipe_candidate_rows(con, where_runs: str = "", args: tuple = ()) -> None:
    """Delete personal rows for the given runs (all runs if where_runs is empty)."""
    cond = f"WHERE run_id IN (SELECT run_id FROM screening_runs {where_runs})" if where_runs else ""
    for table in ("stage_history", "notes", "email_log", "pipeline", "run_results"):
        con.execute(f"DELETE FROM {table} {cond}", args)


def purge_old_data(days: Optional[int] = None) -> dict:
    """
    Auto-delete: remove personal data older than `days` (default: the saved retention setting).

    Deleted : resume text, contact details, scores/justifications per candidate,
              pipeline stages, notes, stage history, email log.
    Kept    : anonymous run summaries (counts, averages, JD text) and fairness statistics,
              so the Fairness Trend keeps working without any personal data.
    """
    out = {"candidates": 0, "run_records": 0}
    days = get_retention_days() if days is None else int(days)
    if days <= 0:
        return out
    cutoff = (datetime.datetime.now() - datetime.timedelta(days=days)).isoformat(timespec="seconds")
    try:
        with _conn() as con:
            out["run_records"] = con.execute(
                "SELECT COUNT(*) FROM run_results WHERE run_id IN "
                "(SELECT run_id FROM screening_runs WHERE created_at < ?)", (cutoff,)).fetchone()[0]
            _wipe_candidate_rows(con, "WHERE created_at < ?", (cutoff,))
            stale = [r["candidate_id"] for r in con.execute(
                "SELECT candidate_id FROM candidates WHERE last_seen < ?", (cutoff,)).fetchall()]
            for cid in stale:
                keys = con.execute("SELECT run_id, cand_key FROM run_results WHERE candidate_id=?",
                                   (cid,)).fetchall()
                for k in keys:
                    for table in ("stage_history", "notes", "email_log"):
                        con.execute(f"DELETE FROM {table} WHERE run_id=? AND cand_key=?",
                                    (k["run_id"], k["cand_key"]))
                con.execute("DELETE FROM pipeline WHERE candidate_id=?", (cid,))
                con.execute("DELETE FROM candidates WHERE candidate_id=?", (cid,))
            out["candidates"] = len(stale)
        set_setting("last_purge", json.dumps({"at": _now(), "days": days, **out}))
    except Exception:
        pass
    return out


def last_purge_info() -> Optional[dict]:
    try:
        raw = get_setting("last_purge", "")
        return json.loads(raw) if raw else None
    except Exception:
        return None


def delete_all_personal_data() -> dict:
    """Wipe every candidate and all linked records. Anonymous run summaries / fairness stats stay."""
    out = {"candidates": 0, "run_records": 0}
    try:
        with _conn() as con:
            out["candidates"] = con.execute("SELECT COUNT(*) FROM candidates").fetchone()[0]
            out["run_records"] = con.execute("SELECT COUNT(*) FROM run_results").fetchone()[0]
            _wipe_candidate_rows(con)
            con.execute("DELETE FROM candidates")
    except Exception:
        pass
    return out


# ── Privacy / housekeeping ───────────────────────────────────────────────────

def delete_candidate(candidate_id: int) -> bool:
    """Right-to-erasure: removes the person and all their results, notes and history."""
    try:
        with _conn() as con:
            keys = con.execute(
                "SELECT run_id, cand_key FROM run_results WHERE candidate_id=?",
                (candidate_id,)).fetchall()
            for k in keys:
                con.execute("DELETE FROM stage_history WHERE run_id=? AND cand_key=?",
                            (k["run_id"], k["cand_key"]))
                con.execute("DELETE FROM notes WHERE run_id=? AND cand_key=?",
                            (k["run_id"], k["cand_key"]))
                con.execute("DELETE FROM email_log WHERE run_id=? AND cand_key=?",
                            (k["run_id"], k["cand_key"]))
            con.execute("DELETE FROM pipeline WHERE candidate_id=?", (candidate_id,))
            con.execute("DELETE FROM candidates WHERE candidate_id=?", (candidate_id,))
        return True
    except Exception:
        return False


def delete_run(run_id: int) -> bool:
    try:
        with _conn() as con:
            con.execute("DELETE FROM stage_history WHERE run_id=?", (run_id,))
            con.execute("DELETE FROM notes WHERE run_id=?", (run_id,))
            con.execute("DELETE FROM email_log WHERE run_id=?", (run_id,))
            con.execute("DELETE FROM screening_runs WHERE run_id=?", (run_id,))
        return True
    except Exception:
        return False


STATUS_LABELS = {
    "active":         ("🟢", "Active"),
    "do_not_contact": ("🚫", "Do not contact"),
    "hired":          ("🎉", "Hired"),
}


def get_candidate_by_key(run_id: Optional[int], cand_key: str) -> Optional[dict]:
    """Look up the talent-pool record (id, email, phone, status) for a candidate in a run."""
    if not run_id:
        return None
    try:
        with _conn() as con:
            r = con.execute(
                """SELECT c.candidate_id, c.name, c.email, c.phone, c.status
                   FROM run_results rr JOIN candidates c ON c.candidate_id=rr.candidate_id
                   WHERE rr.run_id=? AND rr.cand_key=?""", (run_id, cand_key)).fetchone()
            return dict(r) if r else None
    except Exception:
        return None


def get_candidate(candidate_id: int) -> Optional[dict]:
    try:
        with _conn() as con:
            r = con.execute("SELECT * FROM candidates WHERE candidate_id=?", (candidate_id,)).fetchone()
            return dict(r) if r else None
    except Exception:
        return None


def backfill_contacts() -> dict:
    """
    Re-read the stored resume text of every candidate and fill in a missing
    email / phone. Never overwrites a value that is already present.
    Returns counts so the UI can report what happened.
    """
    out = {"checked": 0, "email_filled": 0, "phone_filled": 0, "still_missing": 0}
    try:
        with _conn() as con:
            rows = con.execute(
                "SELECT candidate_id, resume_text, email, phone FROM candidates").fetchall()
            for r in rows:
                out["checked"] += 1
                e, p = _extract_contact(r["resume_text"] or "")
                new_e = r["email"] or e
                new_p = r["phone"] or p
                if new_e != (r["email"] or ""):
                    out["email_filled"] += 1
                if new_p != (r["phone"] or ""):
                    out["phone_filled"] += 1
                if not new_e and not new_p:
                    out["still_missing"] += 1
                con.execute("UPDATE candidates SET email=?, phone=? WHERE candidate_id=?",
                            (new_e, new_p, r["candidate_id"]))
    except Exception:
        pass
    return out


def set_candidate_status(candidate_id: int, status: str) -> None:
    try:
        with _conn() as con:
            con.execute("UPDATE candidates SET status=? WHERE candidate_id=?", (status, candidate_id))
    except Exception:
        pass


def export_talent_pool_csv() -> str:
    import csv
    import io
    rows = search_talent_pool(limit=100000)
    buf = io.StringIO()
    if rows:
        w = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    return buf.getvalue()
