"""Agent 3 — SBI Upload (app/sbi_upload.py, app/api/sbi_portal.py): which of a
CSP's current documents are ready to upload to the SBI portal, the exact
values to enter, what is skipped and why, and what has already been uploaded.
Read-only; no browser, no SBI login anywhere in these paths."""
import hashlib
import uuid
from datetime import date, datetime, timedelta

import pytest

from app import sbi_upload as S
from app import auth, vault
from app.models import CSP, Document, DocumentStatus, InternalUser, SbiUpload
from tests.test_portal_and_api import client  # noqa: F401  (fixture)
from tests.test_login import CSRF, PUBLIC, _login, world  # noqa: F401  (fixture)

HDR = {"X-API-Key": "k", "X-Requested-With": auth.CSRF_HEADER_VALUE}
FUT = date.today() + timedelta(days=400)


def _file(code: str, name: str) -> str:
    folder = vault.ROOT / f"{code}_SBI"
    folder.mkdir(parents=True, exist_ok=True)
    p = folder / name
    p.write_bytes(b"%PDF-1.4\n" + name.encode() + b"\n")
    return vault.rel(p)


def _csp(db, *, state="Bihar", rm=None, agreement=True, pvr=True, iibf="123456789",
         big=False, missing_pvr_state=False):
    tag = uuid.uuid4().hex[:6]
    code = f"9S{int(tag, 16) % 1000000:06d}"
    c = CSP(name=f"SBI CSP {tag}", current_code=code, lookup_code=code, is_active_in_calling_sheet=True,
            state=(None if missing_pvr_state else state), rm_id=rm.id if rm else None)
    db.add(c)
    db.flush()

    def doc(t, big=False, expiry=FUT, reg=None):
        path = _file(code, f"{code}_{t}.pdf")
        if big:
            vault.abs_path(path).write_bytes(b"0" * (3 * 1024 * 1024 + 10))
        d = Document(csp_id=c.id, document_type=t, sha256=hashlib.sha256((path + t).encode()).hexdigest(),
                     status=DocumentStatus.VALID, is_current=True, readability="READABLE",
                     issue_date=date(2026, 1, 1), expiry_date=expiry, storage_path=path,
                     file_size_bytes=(3 * 1024 * 1024 if big else 500), iibf_reg_number=reg)
        db.add(d)
        db.flush()
        return d

    if agreement:
        doc("AGREEMENT", big=big)
    if pvr:
        doc("POLICE_VERIFICATION")
    if iibf is not None:
        doc("IIBF_CERTIFICATE", reg=iibf, expiry=None)
    db.flush()
    return c


# --------------------------------------------------------------------- plan
def test_plan_lists_ready_documents_with_the_right_values(db_session):
    s = db_session
    c = _csp(s)
    s.commit()
    plan = S.plan(s, codes=[c.current_code])
    assert len(plan) == 1
    item = plan[0]
    kinds = {d.kind for d in item.docs}
    assert kinds == {"agreement", "pvr", "iibf"} and not item.skipped
    agr = next(d for d in item.docs if d.kind == "agreement")
    assert agr.issue_date == date(2026, 1, 1) and agr.expiry_date == FUT
    iibf = next(d for d in item.docs if d.kind == "iibf")
    assert iibf.iibf_reg_number == "123456789"


def test_plan_skips_with_clear_reasons(db_session):
    s = db_session
    # no state -> PVR skipped; bad IIBF number -> IIBF skipped; agreement too big -> skipped
    c = _csp(s, missing_pvr_state=True, iibf="12345", big=True)
    s.commit()
    item = S.plan(s, codes=[c.current_code])[0]
    reasons = " | ".join(item.skipped)
    assert "pvr: no state" in reasons
    assert "iibf: registration number '12345' is not 9 digits" in reasons
    assert "agreement:" in reasons and "over 2 MB" in reasons
    assert item.docs == []


def test_already_uploaded_is_not_offered_again_but_a_newer_copy_is(db_session):
    s = db_session
    c = _csp(s, pvr=False, iibf=None)       # agreement only
    s.commit()
    d = S.plan(s, codes=[c.current_code])[0].docs[0]
    by = "tester"
    S.record_upload(s, S.plan(s, codes=[c.current_code])[0], d, by)
    # now it is uploaded: not offered
    assert S.plan(s, codes=[c.current_code])[0].docs == []
    assert S.already_uploaded(s, c.id, "AGREEMENT", d.sha256) is not None
    # a newer agreement copy (different sha) IS offered again
    doc = s.query(Document).filter_by(csp_id=c.id, document_type="AGREEMENT").one()
    doc.sha256 = "newsha_agreement"
    s.commit()
    again = S.plan(s, codes=[c.current_code])[0]
    assert [x.kind for x in again.docs] == ["agreement"]


def test_dashboard_rows_status_per_document(db_session):
    s = db_session
    c = _csp(s)
    s.commit()
    d = S.dashboard_rows(s)
    mine = [r for r in d["rows"] if r["csp"]["code"] == c.current_code]
    assert {r["kind"] for r in mine} == {"agreement", "pvr", "iibf"}
    assert all(r["status"] == "ready" for r in mine)
    agr = next(r for r in mine if r["kind"] == "agreement")
    assert agr["values"]["signatory"] == S.DEFAULT_SIGNATORY and agr["values"]["start_date"] == "2026-01-01"
    # upload the PVR -> that row becomes "uploaded"
    item = S.plan(s, codes=[c.current_code])[0]
    pvr = next(x for x in item.docs if x.kind == "pvr")
    S.record_upload(s, item, pvr, "tester")
    d2 = S.dashboard_rows(s)
    row = next(r for r in d2["rows"] if r["csp"]["code"] == c.current_code and r["kind"] == "pvr")
    assert row["status"] == "uploaded" and row["uploaded"]["by"] == "tester"


# ---------------------------------------------------------------- the page
def test_sbi_upload_page_and_excel_for_an_admin(client, monkeypatch):
    monkeypatch.setattr(auth, "ADMIN_API_KEY", "k")
    c, Session = client
    s = Session()
    x = _csp(s)
    s.commit()
    code = x.current_code
    s.close()
    d = c.get("/api/hub/sbi-upload", headers=HDR).json()
    assert d["signatory"] == S.DEFAULT_SIGNATORY and d["total"] >= 3
    ready = next(b for b in d["statuses"] if b["key"] == "ready")
    assert ready["n"] >= 3
    only = c.get(f"/api/hub/sbi-upload?q={code}&kind=iibf", headers=HDR).json()
    mine = [r for r in only["rows"] if r["csp"]["code"] == code]   # shared test DB: isolate this CSP
    assert len(mine) == 1 and mine[0]["values"]["registration_number"] == "123456789"
    xl = c.get(f"/api/hub/sbi-upload.xlsx?q={code}", headers=HDR)
    assert xl.status_code == 200 and xl.content[:2] == b"PK"


def test_sbi_upload_is_admin_only(world):
    """The SBI upload worklist is an ops task: RMs are refused (admins only)."""
    c, w = world
    assert _login(c, w["rm"]).status_code == 200
    assert c.get("/api/hub/sbi-upload", headers={**PUBLIC, **CSRF}).status_code == 403
    assert c.get("/api/hub/sbi-upload.xlsx", headers={**PUBLIC, **CSRF}).status_code == 403
