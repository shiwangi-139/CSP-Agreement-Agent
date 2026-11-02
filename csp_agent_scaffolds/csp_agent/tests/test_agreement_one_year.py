"""scripts/agreement_one_year.py: agreements still on the old 2-year default
get 1 year from issue (no validity written); dry run changes nothing."""
import sys
import uuid
from datetime import date, timedelta

from app.models import CSP, Document, DocumentStatus


def _agreement(db, issue):
    tag = uuid.uuid4().hex[:6]
    c = CSP(name=f"AGR CSP {tag}", current_code=f"8G{int(tag, 16) % 1000000:06d}", is_active_in_calling_sheet=True)
    db.add(c)
    db.flush()
    d = Document(csp_id=c.id, document_type="AGREEMENT", sha256=uuid.uuid4().hex, readability="READABLE",
                 status=DocumentStatus.VALID, is_current=True, issue_date=issue,
                 expiry_date=issue.replace(year=issue.year + 2), validity_rule_used="AGREEMENT_DEFAULT_2_YEAR")
    db.add(d)
    db.commit()
    return c, d


def _run(monkeypatch, test_engine, *args):
    from sqlalchemy.orm import sessionmaker
    from scripts import agreement_one_year
    monkeypatch.setattr(agreement_one_year, "SessionLocal", sessionmaker(bind=test_engine))
    monkeypatch.setattr(sys, "argv", ["agreement_one_year", "--no-reread", *args])
    agreement_one_year.main()


def test_old_two_year_default_becomes_one_year(db_session, test_engine, monkeypatch, capsys):
    old, d_old = _agreement(db_session, date.today() - timedelta(days=500))     # 2 years: still valid; 1 year: over
    new, d_new = _agreement(db_session, date.today() - timedelta(days=100))
    _run(monkeypatch, test_engine, "--csp", old.current_code)
    assert "EXPIRED now" in capsys.readouterr().out
    db_session.expire_all()
    assert db_session.get(Document, d_old.id).validity_rule_used == "AGREEMENT_DEFAULT_2_YEAR"   # dry run
    for c in (old, new):
        _run(monkeypatch, test_engine, "--csp", c.current_code, "--apply")
    db_session.expire_all()
    a, b = db_session.get(Document, d_old.id), db_session.get(Document, d_new.id)
    assert a.validity_rule_used == b.validity_rule_used == "AGREEMENT_DEFAULT_1_YEAR"
    assert a.status == DocumentStatus.EXPIRED and b.status == DocumentStatus.VALID
    assert a.expiry_date < date.today() < b.expiry_date and b.validity_months == 12
