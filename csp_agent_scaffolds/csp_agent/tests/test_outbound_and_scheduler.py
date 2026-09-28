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
    for ladder in LADDERS.values():
        roles = [s["to"] for s in ladder]
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
    assert ids == {"gmail", "sheet", "engine", "outbox", "vault", "reports"}
    assert "worker" in get_scheduler_status()["runs_in"]
