"""scripts/pcc_one_year.py: Police Clearance Certificates once accepted with
"no expiry" become valid 1 year from their issue date; dry run changes nothing."""
import sys
import uuid
from datetime import date, timedelta

from app.models import CSP, Document, DocumentStatus


def _pcc(db, issue):
    tag = uuid.uuid4().hex[:6]
    c = CSP(name=f"PCC CSP {tag}", current_code=f"8P{int(tag, 16) % 1000000:06d}", is_active_in_calling_sheet=True)
    db.add(c)
    db.flush()
    d = Document(csp_id=c.id, document_type="POLICE_VERIFICATION", sha256=uuid.uuid4().hex, readability="READABLE",
                 status=DocumentStatus.MANUAL_VERIFIED, is_current=True, issue_date=issue, expiry_date=None,
                 validity_rule_used="PVR_PCC_LIFETIME")
    db.add(d)
    db.commit()
    return c, d


def _run(monkeypatch, test_engine, *args):
    from sqlalchemy.orm import sessionmaker
    from scripts import pcc_one_year
    monkeypatch.setattr(pcc_one_year, "SessionLocal", sessionmaker(bind=test_engine))
    monkeypatch.setattr(sys, "argv", ["pcc_one_year", *args])
    pcc_one_year.main()


def test_dry_run_changes_nothing_and_apply_gives_one_year(db_session, test_engine, monkeypatch, capsys):
    old, d_old = _pcc(db_session, date.today() - timedelta(days=500))
    new, d_new = _pcc(db_session, date.today() - timedelta(days=100))
    _run(monkeypatch, test_engine, "--csp", old.current_code)
    assert "EXPIRED now" in capsys.readouterr().out
    db_session.expire_all()
    assert db_session.get(Document, d_old.id).expiry_date is None          # dry run: unchanged
    for c in (old, new):
        _run(monkeypatch, test_engine, "--csp", c.current_code, "--apply")
    db_session.expire_all()
    a, b = db_session.get(Document, d_old.id), db_session.get(Document, d_new.id)
    assert a.validity_rule_used == b.validity_rule_used == "PVR_PCC_DEFAULT_1_YEAR"
    assert a.expiry_date < date.today() < b.expiry_date and b.validity_months == 12
    assert db_session.get(CSP, old.id).category is not None
