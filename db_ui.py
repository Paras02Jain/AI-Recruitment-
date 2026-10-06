"""
db_ui.py — Streamlit UI for the EduHire database
=================================================
Renders the "Database & Talent Pool" section:
  1. Past Screening Runs  (reload / delete)
  2. Talent Pool          (search every candidate ever screened, export, erase)
  3. Fairness Trend       (longitudinal bias tracking across hiring cycles)
  4. Hiring Funnel + DB stats
"""
from __future__ import annotations
import json
import database as db


def _load_run_into_session(st, run_id: int) -> bool:
    results = db.load_run_results(run_id)
    if not results:
        return False
    run = db.get_run(run_id) or {}
    st.session_state["screening_results"] = results
    st.session_state["screening_jd"] = run.get("jd_text", "")
    st.session_state["pipeline"] = db.load_pipeline_state(run_id)
    st.session_state["current_run_id"] = run_id
    st.session_state.pop("bias_report", None)
    try:
        from rag_engine import build_index
        st.session_state["rag_index"] = build_index(results)
    except Exception:
        st.session_state["rag_index"] = None
    return True


def render_database_section(st, C: dict, divider) -> None:
    st.markdown(
        f"""
<div style="background:linear-gradient(135deg,rgba(14,165,233,.12),rgba(16,185,129,.10));
     border:1px solid rgba(14,165,233,.3);border-radius:16px;padding:28px 36px;margin-bottom:24px;">
  <h2 style="margin:0;font-size:1.5rem;font-weight:800;color:{C['cream']};
      font-family:'DM Serif Display',Georgia,serif;">🗄️ Database & Talent Pool</h2>
  <p style="font-size:.83rem;color:{C['silver']};margin:4px 0 0;">
    Every screening run, candidate, stage change and note is saved permanently (SQLite) ·
    Reload old runs · Search past applicants · Track fairness over time
  </p>
</div>""",
        unsafe_allow_html=True,
    )

    if not db.db_available():
        st.warning("Database not reachable. Check write permission on the app folder.")
        return

    stats = db.db_stats()
    cols = st.columns(5)
    for col, (label, val) in zip(cols, [
        ("Screening runs", stats["runs"]), ("Candidates in pool", stats["candidates"]),
        ("Repeat applicants", stats["repeat_applicants"]), ("Notes logged", stats["notes"]),
        ("DB size (KB)", stats["size_kb"]),
    ]):
        col.metric(label, val)

    t_runs, t_pool, t_fair, t_funnel, t_priv = st.tabs(
        ["📁 Past Runs", "👥 Talent Pool", "⚖️ Fairness Trend", "🔻 Hiring Funnel", "🔒 Privacy & Retention"]
    )

    # ── 1. Past runs ─────────────────────────────────────────────────────────
    with t_runs:
        runs = db.list_runs()
        if not runs:
            st.info("No saved runs yet. Click **Screen Candidates** — the run is saved automatically.")
        else:
            import pandas as pd
            df = pd.DataFrame(runs).rename(columns={
                "run_id": "Run", "created_at": "Date", "role_title": "Role",
                "total_screened": "Screened", "qualified_count": "Qualified",
                "filtered_count": "Filtered", "top_score": "Top %", "avg_score": "Avg %",
                "scoring_method": "Method", "bias_risk": "JD bias", "stored": "Candidates stored",
            }).drop(columns=["school_name"], errors="ignore")
            for c in ("Top %", "Avg %"):
                df[c] = df[c].round(1)
            st.dataframe(df, use_container_width=True, hide_index=True)

            ids = [r["run_id"] for r in runs]
            sel = st.selectbox(
                "Select a run", ids, key="db_run_sel",
                format_func=lambda i: next(
                    f"Run #{r['run_id']} · {r['created_at'][:16]} · {r['role_title'] or 'Role'}"
                    for r in runs if r["run_id"] == i),
            )
            c1, c2, _ = st.columns([1, 1, 3])
            if c1.button("📂 Load this run", key="db_load_run", use_container_width=True):
                if _load_run_into_session(st, sel):
                    st.session_state["page"] = "results"      # open the restored run on the Results page
                    st.rerun()
                else:
                    st.error("No candidate data is stored for this run (it was auto-deleted or erased). "
                             "Only the anonymous summary and fairness statistics remain.")
            if c2.button("🗑️ Delete run", key="db_del_run", use_container_width=True):
                if db.delete_run(sel):
                    if st.session_state.get("current_run_id") == sel:
                        st.session_state.pop("current_run_id", None)
                    st.rerun()

    # ── 2. Talent pool ───────────────────────────────────────────────────────
    with t_pool:
        f1, f2, f3, f4 = st.columns([2, 1, 1, 2])
        q = f1.text_input("Search name or resume text", key="tp_q")
        min_exp = f2.number_input("Min exp (yrs)", 0, 40, 0, key="tp_exp")
        skill = f4.text_input("Skill contains (e.g. stem)", key="tp_skill")
        status_f = st.selectbox(
            "Status", ["All", "Active", "Do not contact", "Hired"], key="tp_status")
        _smap = {"All": "", "Active": "active", "Do not contact": "do_not_contact", "Hired": "hired"}
        k1, k2, k3 = st.columns(3)
        o_cert = k1.checkbox("Certified only", key="tp_cert")
        o_deg = k2.checkbox("Has teaching degree", key="tp_deg")
        o_prem = k3.checkbox("Premier institution", key="tp_prem")

        if st.button("🔄 Re-extract email / phone from saved resumes", key="tp_backfill",
                     help="Reads every stored resume again and fills missing email/phone. Never overwrites existing values."):
            r = db.backfill_contacts()
            st.session_state["tp_flash"] = (
                f"Checked {r['checked']} resumes · {r['email_filled']} emails and {r['phone_filled']} phones added · "
                f"{r['still_missing']} resumes contain no contact info.")
            st.rerun()
        if st.session_state.get("tp_flash"):
            st.success(st.session_state.pop("tp_flash"))

        pool = db.search_talent_pool(q, int(min_exp), o_cert, o_deg, o_prem, skill, _smap[status_f])
        st.caption(f"{len(pool)} candidate(s) match")
        if pool:
            import pandas as pd
            dfp = pd.DataFrame(pool)
            dfp["skills"] = dfp["skills_json"].apply(lambda s: ", ".join(json.loads(s or "[]")))
            for b in ("has_degree", "has_cert", "is_premier"):
                dfp[b] = dfp[b].map({1: "Yes", 0: "No"})
            dfp["best_score"] = dfp["best_score"].round(1)
            dfp["status"] = dfp["status"].apply(
                lambda x: " ".join(db.STATUS_LABELS.get(x, ("⚪", x))))
            show = dfp[["candidate_id", "name", "email", "phone", "exp_years", "has_degree",
                        "has_cert", "is_premier", "best_score", "times_screened",
                        "status", "last_seen", "skills"]]
            st.dataframe(show, use_container_width=True, hide_index=True)

            st.download_button("⬇️ Export talent pool (CSV)", db.export_talent_pool_csv(),
                               "eduhire_talent_pool.csv", "text/csv", key="tp_csv")

            st.markdown("**Candidate actions**")
            cid = st.selectbox(
                "Candidate", [p["candidate_id"] for p in pool], key="tp_sel",
                format_func=lambda i: next(f"{p['name']} (#{i})" for p in pool if p["candidate_id"] == i),
            )
            rec = db.get_candidate(cid) or {}
            ico, lbl = db.STATUS_LABELS.get(rec.get("status", "active"), ("⚪", "Unknown"))
            st.markdown(f"**Current status:** {ico} {lbl}")
            c_email, c_phone = rec.get("email") or "", rec.get("phone") or ""
            if c_email or c_phone:
                st.markdown(f"**Contact (from resume):** ✉️ {c_email or '—'}  ·  📞 {c_phone or '—'}")
            else:
                st.caption("No email / phone found in this candidate's resume text.")

            hist = db.candidate_history(cid)
            if hist:
                st.write("Application history:")
                st.dataframe(pd.DataFrame(hist), use_container_width=True, hide_index=True)

            st.caption(
                "🚫 **Do not contact** blocks the Send Email button for this person in every run and shows a "
                "warning in the Email and Pipeline sections (use for withdrawn / blacklisted / asked-not-to-be-contacted). "
                "♻️ **Active** removes the block. 🎉 **Hired** is set automatically when a candidate is moved to Offer. "
                "Status is per person, not per run."
            )
            a1, a2, a3 = st.columns(3)
            if a1.button("🚫 Mark do-not-contact", key="tp_dnc"):
                db.set_candidate_status(cid, "do_not_contact")
                st.session_state["tp_flash"] = f"{rec.get('name','Candidate')} marked as DO NOT CONTACT."
                st.rerun()
            if a2.button("♻️ Mark active", key="tp_act"):
                db.set_candidate_status(cid, "active")
                st.session_state["tp_flash"] = f"{rec.get('name','Candidate')} marked as ACTIVE."
                st.rerun()
            if a3.button("🗑️ Erase candidate data", key="tp_erase",
                         help="Permanently deletes this person and all linked records (privacy / right to erasure)."):
                db.delete_candidate(cid)
                st.session_state["tp_flash"] = "Candidate data erased."
                st.rerun()

    # ── 3. Fairness trend ────────────────────────────────────────────────────
    with t_fair:
        trend = db.fairness_trend()
        st.caption(
            "Each hiring run is audited: do candidates from premier institutions pass more often than others? "
            "This chart tracks that gap run after run. A gap of 20 points or more is flagged. "
            "Falling gap = fairer over time. You need 2+ runs to see a trend line."
        )
        if len(trend) < 1:
            st.info("Fairness snapshots are stored per run. Run at least one screening to start tracking.")
        else:
            import pandas as pd
            import plotly.graph_objects as go
            dft = pd.DataFrame(trend)
            dft["run"] = dft["run_id"].apply(lambda i: f"Run #{i}")
            fig = go.Figure()
            fig.add_trace(go.Bar(x=dft["run"], y=dft["gap_points"], name="Gap (pts)",
                                 opacity=.35, text=dft["gap_points"].round(1), textposition="outside"))
            fig.add_trace(go.Scatter(x=dft["run"], y=dft["premier_pass_rate"],
                                     mode="lines+markers", name="Premier pass %"))
            fig.add_trace(go.Scatter(x=dft["run"], y=dft["nonpremier_pass_rate"],
                                     mode="lines+markers", name="Non-premier pass %"))
            fig.add_hline(y=20, line_dash="dot", annotation_text="20-pt flag threshold")
            fig.update_xaxes(type="category")
            fig.update_layout(template="plotly_dark", height=380, yaxis_title="%",
                              paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)")
            st.plotly_chart(fig, use_container_width=True)

            # Which flags fired, per run (the chart only shows the institution gap)
            st.markdown("**Flags raised per run**")
            for row in reversed(trend):
                try:
                    flags = json.loads(row.get("audit_json") or "{}").get("disparities", [])
                except Exception:
                    flags = []
                label = f"Run #{row['run_id']} · {str(row['created_at'])[:16]} · {len(flags)} flag(s)"
                with st.expander(label, expanded=bool(flags) and row is trend[-1]):
                    if not flags:
                        st.success("No disparity above the 20-point threshold.")
                    for f in flags:
                        st.warning(f"**{f.get('metric','')}** - gap {f.get('gap','?')} pts "
                                   f"(higher: {f.get('direction','?')}, severity: {f.get('severity','?')})")
            st.dataframe(dft.drop(columns=["audit_json", "run"]), use_container_width=True, hide_index=True)

    # ── 4. Funnel ────────────────────────────────────────────────────────────
    with t_funnel:
        funnel = db.funnel_summary()
        if not funnel:
            st.info("No pipeline data yet.")
        else:
            import plotly.graph_objects as go
            from pipeline_tracker import PIPELINE_STAGES
            vals = [funnel.get(s, 0) for s in PIPELINE_STAGES]
            fig = go.Figure(go.Bar(x=PIPELINE_STAGES, y=vals, text=vals, textposition="outside"))
            fig.update_layout(template="plotly_dark", height=340,
                              title="All-time candidates by pipeline stage",
                              paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)")
            st.plotly_chart(fig, use_container_width=True)

    # ── 5. Privacy & retention ───────────────────────────────────────────────
    with t_priv:
        st.markdown("**What is stored:** resume text, email, phone, scores and justifications, pipeline stages, "
                    "notes and the email log, in a local file (`eduhire.db`) on this machine. Nothing is sent to a cloud database.")

        cur = db.get_retention_days()
        labels = list(db.RETENTION_OPTIONS.keys())
        cur_label = next((k for k, v in db.RETENTION_OPTIONS.items() if v == cur),
                         next(k for k, v in db.RETENTION_OPTIONS.items() if v == db.DEFAULT_RETENTION_DAYS))
        r1, r2 = st.columns([2, 1])
        choice = r1.selectbox("🗓️ Auto-delete candidate data after", labels,
                              index=labels.index(cur_label), key="ret_sel")
        r2.markdown("<br>", unsafe_allow_html=True)
        if r2.button("💾 Save retention period", key="ret_save", use_container_width=True):
            db.set_retention_days(db.RETENTION_OPTIONS[choice])
            st.success(f"Saved. Candidate data will be auto-deleted after: {choice}.")
        st.caption("Cleanup runs automatically every time the app is opened. "
                   "Anonymous run summaries and fairness statistics are kept, so the Fairness Trend still works; "
                   "names, resume text and contact details are not.")

        lp = db.last_purge_info()
        if lp:
            st.info(f"Last auto-cleanup: {str(lp['at'])[:16]} · period {lp['days']} days · "
                    f"{lp['candidates']} candidate(s) and {lp['run_records']} run record(s) removed.")

        c1, c2 = st.columns([1, 3])
        if c1.button("🧹 Run cleanup now", key="purge_now", use_container_width=True):
            res = db.purge_old_data(db.RETENTION_OPTIONS[choice])
            if db.RETENTION_OPTIONS[choice] == 0:
                st.warning("Auto-delete is set to 'Never', so nothing was removed.")
            else:
                st.success(f"Removed {res['candidates']} candidate(s) and {res['run_records']} run record(s) "
                           f"older than {choice}.")

        st.markdown("---")
        st.markdown("**⚠️ Danger zone**")
        sure = st.checkbox("I understand this permanently deletes ALL candidate data", key="wipe_ok")
        if st.button("🗑️ Delete ALL candidate data", key="wipe_all", disabled=not sure):
            res = db.delete_all_personal_data()
            st.session_state.pop("current_run_id", None)
            st.success(f"Deleted {res['candidates']} candidate(s) and {res['run_records']} run record(s).")
