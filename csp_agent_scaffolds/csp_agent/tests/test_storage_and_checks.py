"""Storage and self-checks (18-11-2026): per-document upload limits, turned-away
files deleted after 90 days, large scans made smaller only when they read the
same (originals kept 30 days), the frozen golden set, and the weekly full
re-read that records documents which would now read differently."""
import io
import os
import time
import uuid
from datetime import date, datetime, timedelta

import pytest

from app import shrink, vault
from app.models import (AgentSignal, CSP, Document, DocumentStatus, GoldenCase, ManualReviewQueue, ReviewStatus)
from tests.test_portal_and_api import _csp, _form, client  # noqa: F401  (fixture)


def _scan_pdf(pages: int = 2, side: int = 3000) -> bytes:
    """A scanned-looking PDF: each page a large lossless image, no text layer."""
    import fitz
    import numpy as np
    import cv2
    doc = fitz.open()
    for i in range(pages):
        img = np.full((side, int(side * 0.7), 3), 250, np.uint8)
        for y in range(200, side - 200, 90):
            cv2.putText(img, f"CSP AGREEMENT CLAUSE {i}.{y} EKO INDIA FINANCIAL", (120, y), cv2.FONT_HERSHEY_SIMPLEX,
                        1.6, (20, 20, 20), 3)
        cv2.circle(img, (int(side * 0.5), side - 400), 160, (160, 60, 40), 8)          # a coloured stamp
        png = cv2.imencode(".png", img)[1].tobytes()
        page = doc.new_page(width=595, height=842)
        page.insert_image(page.rect, stream=png)
    return doc.tobytes()


def _text_pdf() -> bytes:
    import fitz
    doc = fitz.open()
    page = doc.new_page()
    page.insert_textbox(fitz.Rect(50, 50, 545, 800),
                        "This Agreement is made at Patna between Eko India Financial Services. " * 20, fontsize=9)
    return doc.tobytes()


def _stored(db, data: bytes, doc_type="AGREEMENT", **kw):
    tag = uuid.uuid4().hex[:6]
    code = f"6S{int(tag, 16) % 1000000:06d}"
    c = CSP(name=f"Storage {tag}", current_code=code, is_active_in_calling_sheet=True)
    db.add(c)
    db.flush()
    folder = vault.ROOT / f"{code}_STORAGE"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{code}_AGREEMENT_ACTIVE_2026-01-01_to_2029-01-01.pdf"
    path.write_bytes(data)
    import hashlib
    d = Document(csp_id=c.id, document_type=doc_type, sha256=hashlib.sha256(data).hexdigest(),
                 status=kw.get("status", DocumentStatus.VALID), storage_path=vault.rel(path), is_current=True,
                 issue_date=date(2026, 1, 1), expiry_date=date(2029, 1, 1), file_size_bytes=len(data),
                 date_source=kw.get("date_source"))
    db.add(d)
    db.flush()
    return c, d, path


# ----------------------------------------------------------- upload limits
def test_each_document_has_its_own_upload_limit(client, monkeypatch):
    from app.api import portal
    c, Session = client
    _, _, token = _csp(Session)
    ctx = c.get(f"/api/portal/context?token={token}")
    assert ctx.status_code == 200, ctx.text
    sizes = {d["type"]: d["max_mb"] for d in ctx.json()["docs"]}
    assert sizes == {"AGREEMENT": 8.0, "POLICE_VERIFICATION": 3.0, "IIBF_CERTIFICATE": 2.0}
    big = b"%PDF-1.4\n" + os.urandom(int(3.2 * 1024 * 1024))
    r = c.post("/api/portal/upload", data=_form(token, pvr_issue_date=(date.today() - timedelta(days=9)).isoformat()),
               files={"pvr_file": ("pvr.pdf", big, "application/pdf")})
    res = r.json()["results"][0]
    assert not res["ok"] and "larger than 3 MB" in res["message"]["en"]


# ----------------------------------------------- turned-away files, originals
def test_turned_away_files_are_deleted_after_90_days_and_originals_after_30():
    old, new = vault.ROOT / vault.PORTAL_REJECTED / "1A000001", vault.ROOT / vault.ORIGINALS / "2026-01-01"
    old.mkdir(parents=True)
    new.mkdir(parents=True)
    stale, fresh, orig = old / "a.pdf", old / "b.pdf", new / "c.pdf"
    for f in (stale, fresh, orig):
        f.write_bytes(b"x" * 1000)
    ago = time.time() - 100 * 86400
    os.utime(stale, (ago, ago))
    os.utime(orig, (ago, ago))
    assert vault.purge_rejected()["deleted"] == 1
    assert not stale.exists() and fresh.exists()
    assert vault.purge_originals()["deleted"] == 1 and not orig.exists()


# -------------------------------------------------------------------- shrink
def test_a_scanned_pdf_is_made_smaller_and_a_digital_one_is_left_alone():
    scan = _scan_pdf()
    assert shrink.is_scanned_pdf(scan) is True and shrink.is_scanned_pdf(_text_pdf()) is False
    small = shrink.shrink_pdf(scan)
    assert small is not None and len(small) < 0.7 * len(scan)
    import fitz
    a, b = fitz.open(stream=scan, filetype="pdf"), fitz.open(stream=small, filetype="pdf")
    assert a.page_count == b.page_count and a[0].rect == b[0].rect          # same pages, same size


def test_shrunk_only_when_it_reads_the_same_and_the_original_is_kept(db_session):
    db = db_session
    scan = _scan_pdf()
    c, d, path = _stored(db, scan)
    same = {"readability": "READABLE", "document_type": "AGREEMENT", "start_date": "2026-01-01",
            "expiry_date": "2029-01-01", "validity_rule_used": "R", "compliance_status": "ACTIVE"}
    row = shrink.try_one(db, d, path, apply=True, reader=lambda p, t, timeout: dict(same))
    assert row["action"] == "shrunk" and row["same_reading"]
    db.refresh(d)
    info = d.extracted_fields["shrunk"]
    assert path.stat().st_size == info["to_bytes"] < len(scan) and d.file_size_bytes == info["to_bytes"]
    assert (vault.ROOT / info["original"]).read_bytes() == scan                 # the original is kept
    import hashlib
    assert d.sha256 == hashlib.sha256(scan).hexdigest()                        # still the file as received
    assert shrink.candidates(db, only=[d.id]) == []                             # never shrunk twice

    c2, d2, path2 = _stored(db, _scan_pdf())
    reads = iter([dict(same), {**same, "start_date": "2026-01-07"}])
    row2 = shrink.try_one(db, d2, path2, apply=True, reader=lambda p, t, timeout: next(reads))
    assert row2["action"].startswith("kept the original") and "start_date" in row2["differs"]
    assert not (d2.extracted_fields or {}).get("shrunk") and path2.stat().st_size > 0


def test_dry_run_changes_nothing(db_session):
    db = db_session
    scan = _scan_pdf()
    c, d, path = _stored(db, scan)
    same = {"readability": "READABLE", "document_type": "AGREEMENT"}
    row = shrink.try_one(db, d, path, apply=False, reader=lambda p, t, timeout: dict(same))
    assert row["action"] == "would shrink (dry run)" and path.read_bytes() == scan


# ------------------------------------------------------------- golden set
def test_the_golden_set_keeps_the_answers_as_they_were_frozen(db_session):
    from scripts.eval_reading import _golden
    from scripts.golden_set import freeze, latest_version
    db = db_session
    c, d, _ = _stored(db, b"%PDF-1.4 golden")
    db.add(ManualReviewQueue(document_id=d.id, status=ReviewStatus.APPROVED, resolved_at=datetime.utcnow(),
                             reason="test"))
    db.commit()
    before = latest_version(db)
    v, n = freeze(db, "test")
    assert v == before + 1 and n[("AGREEMENT", "accept")] >= 1
    d.issue_date = date(2026, 2, 2)                                   # the row changes later
    db.commit()
    mine = [g for g, _c, _t in _golden(db) if g.id == d.id]
    assert mine and mine[0].issue_date == date(2026, 1, 1)             # the frozen answer stays


# ---------------------------------------------------------- full re-read
def test_full_reread_records_a_difference_once_and_the_coach_shows_it(db_session):
    from app import signals
    from scripts.full_reread import differences, record
    db = db_session
    c, d, _ = _stored(db, b"%PDF-1.4 reread", status=DocumentStatus.MANUAL_VERIFIED, date_source="MANUAL_REVIEW")
    same = {"readability": "READABLE", "document_type": "AGREEMENT", "start_date": "2026-01-01",
            "expiry_date": "2029-01-01"}
    assert differences(d, same) == []
    assert differences(d, {"timeout": True}) == []                     # could not read: not a difference
    other = {**same, "start_date": "2026-03-01", "expiry_date": "2029-03-01", "date_source": "AGREEMENT_X"}
    diffs = differences(d, other)
    assert diffs == ["issued 2026-01-01 -> 2026-03-01", "expires 2029-01-01 -> 2029-03-01"]
    now = datetime.utcnow()
    assert record(db, d, c, diffs, other, now) is True
    db.commit()
    assert record(db, d, c, diffs, other, now) is False                # once per document and reading
    db.commit()
    found = [f for f in signals.findings(db) if "a person decided would now read differently" in f["title"]]
    assert found and found[0]["level"] == "critical"
    db.query(AgentSignal).filter(AgentSignal.document_id == d.id).delete()
    db.commit()
