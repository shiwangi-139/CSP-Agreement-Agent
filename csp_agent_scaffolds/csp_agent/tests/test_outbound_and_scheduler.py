"""Template registry and the worker schedule."""
from app.comms.templates import TEMPLATES, render
from app.policy import LADDERS, MAX_RM_MESSAGES, MAX_DC_MESSAGES
from app.scheduler import get_scheduler_status
from app.worker import build_scheduler


def test_every_policy_template_exists():
    used = {s.get("template") for ladder in LADDERS.values() for s in ladder if s.get("template")}
    used |= {"UPLOAD_MISSING", "UPLOAD_EXPIRED", "ONBOARD_ALL", "UNREADABLE_REUPLOAD"}
    assert used <= set(TEMPLATES)


def test_policy_tables_respect_caps():
    for key, ladder in LADDERS.items():
        roles = [s["to"] for s in ladder]
        if key == "UPLOAD":                  # RM / DC get a daily list instead (app/escalation.py)
            assert set(roles) == {"CSP"}
            continue
        assert roles.count("RM") <= MAX_RM_MESSAGES and roles.count("DC") <= MAX_DC_MESSAGES
        assert roles.index("RM") >= 2
        assert roles.index("DC") > max(i for i, r in enumerate(roles) if r == "RM")


def test_renewal_template_renders_with_dates():
    out = render("RENEWAL_FINAL", "WHATSAPP", {"csp_name": "Ramesh Kumar", "csp_code": "1A850247",
                                               "doc_label_en": "CSP Agreement", "doc_label_hi": "सीएसपी एग्रीमेंट",
                                               "expiry": "02-10-2026", "days_left": 7})
    assert "Ramesh Kumar" in out["body"] and "02-10-2026" in out["body"] and "7" in out["subject"]


def test_worker_schedule():
    ids = {j.id for j in build_scheduler().get_jobs()}
    from app.config import FEATURE_MESSAGING_ANALYTICS
    core = {"gmail", "sheet", "engine", "outbox", "vault", "reports", "model_assist", "model_guard", "snapshot", "snapshot_at_start",
            "staff_lists", "nudges", "catch_up_at_start", "reading_eval", "signals"}
    assert ids == (core | {"delivery"} if FEATURE_MESSAGING_ANALYTICS else core)    # delivery: parked feature
    assert "worker" in get_scheduler_status()["runs_in"]


def test_worker_catches_up_daily_runs_missed_while_it_was_stopped(db_session):
    from datetime import datetime, timedelta, timezone
    from zoneinfo import ZoneInfo
    from app.models import MonitorRun
    from app.worker import missed_today
    db = db_session
    db.query(MonitorRun).filter(MonitorRun.kind.in_(["job:engine", "job:staff_lists", "job:nudges"])).delete()
    ist = ZoneInfo("Asia/Kolkata")
    now = datetime(2026, 10, 23, 13, 9, tzinfo=ist)
    assert missed_today(db, now) == ["engine", "staff_lists"]          # 17:00 not due yet
    assert missed_today(db, now.replace(hour=8)) == []
    ran = datetime(2026, 10, 23, 9, 0, tzinfo=ist).astimezone(timezone.utc).replace(tzinfo=None) + timedelta(minutes=1)
    db.add(MonitorRun(kind="job:engine", started_at=ran, status="OK"))
    db.flush()
    assert missed_today(db, now) == ["staff_lists"]
