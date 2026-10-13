"""Portal uploads and API access, end to end through FastAPI, on the test DB.
Documents are generated on the fly; no real CSP files are used."""
import io
import uuid
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app import vault
from app import auth
from app.db import get_db
from app.main import app
from app.models import CSP, Document, DocumentStatus, InternalUser, PortalToken
from app.portal_tokens import issue_upload_link, resolve_token


@pytest.fixture()
def client(test_engine, tmp_path, monkeypatch):
    TestSession = sessionmaker(bind=test_engine)

    def _db():
        s = TestSession()
        try:
            yield s
        finally:
            s.close()
    app.dependency_overrides[get_db] = _db
    yield TestClient(app), TestSession
    app.dependency_overrides.clear()


def _csp(Session):
    s = Session()
    tag = uuid.uuid4().hex[:6]
    code = f"8Y{int(tag, 16) % 1000000:06d}"
    rm = InternalUser(name=f"RM {tag}", role="RM", phone="9000000011")
    s.add(rm)
    s.flush()
    c = CSP(name=f"Portal CSP {tag}", current_code=code, lookup_code=code, phone="9876500000",
            whatsapp_number="9876500000", rm_id=rm.id, is_active_in_calling_sheet=True)
    s.add(c)
    s.flush()
    link = issue_upload_link(s, c, ["POLICE_VERIFICATION", "IIBF_CERTIFICATE", "AGREEMENT"])
    s.commit()
    token = link.split("token=")[1]
    cid = c.id
    s.close()
    return cid, code, token


def _pvr_pdf(issue: date, holder: str = "TEST PERSON") -> bytes:
    import fitz
    doc = fitz.open()
    page = doc.new_page()
    text = ("Government of Uttar Pradesh  CHARACTER CERTIFICATE\n"
            f"Application No. - 202503277853 Date - {issue.strftime('%d-%m-%Y')}\n"
            f"This is to certify that Mr. {holder} ... no adverse entry was found against the said\n"
            "candidate in the police records. This certificate is valid only for one year.\n"
            "This is a computer generated document so no signature is required.\n"
            f"Digitally signed by TEST OFFICER Date: {issue.strftime('%Y.%m.%d')} 18:29:17 +05'30'\n")
    page.insert_text((50, 80), text, fontsize=10)
    return doc.tobytes()


def _blurry_png() -> bytes:
    import numpy as np
    import cv2
    img = np.full((1400, 1000), 255, np.uint8)
    cv2.putText(img, "POLICE VERIFICATION 12/05/2025", (40, 400), cv2.FONT_HERSHEY_SIMPLEX, 1.4, 0, 3)
    img = cv2.GaussianBlur(img, (61, 61), 30)
    return cv2.imencode(".png", img)[1].tobytes()


def _form(token, **kw):
    base = {"token": token, "name": "Portal CSP", "mobile": "9876500000"}
    base.update(kw)
    return base


def test_bad_token_page_offers_a_new_link_not_the_form(client):
    c, _ = client
    r = c.get("/upload?token=nope")
    assert "Get your upload link" in r.text and "__CONTEXT_JSON__" not in r.text and "csp_id" not in r.text


def test_page_renders_with_escaped_context(client):
    c, Session = client
    _, code, token = _csp(Session)
    r = c.get(f"/upload?token={token}")
    assert r.status_code == 200 and code in r.text and "__CONTEXT_JSON__" not in r.text


def test_readable_pvr_is_accepted_and_filed_in_csp_folder(client, tmp_path):
    c, Session = client
    cid, code, token = _csp(Session)
    issue = date.today() - timedelta(days=20)
    r = c.post("/api/portal/upload", data=_form(token, pvr_issue_date=issue.isoformat()),
               files={"pvr_file": ("pvr.pdf", _pvr_pdf(issue), "application/pdf")})
    assert r.status_code == 200, r.text
    res = r.json()["results"][0]
    assert res["ok"] and res["issue_date"] == issue.isoformat()
    s = Session()
    d = s.query(Document).filter_by(csp_id=cid).one()
    assert d.readability == "READABLE" and d.is_current and d.date_source == "PVR_DIGITAL_SIGNATURE_DATE"
    path = vault.abs_path(d.storage_path)
    assert path.parent.parent == tmp_path / "documents" and path.parent.name.startswith(code + "_")
    assert path.exists() and path.name.startswith(f"{code}_PVR_ACTIVE_{issue.isoformat()}_to_")
    assert not d.storage_path.startswith("/")   # stored relative to the vault
    s.close()


def test_expired_typed_date_is_rejected_before_reading(client):
    c, Session = client
    _, _, token = _csp(Session)
    old = date.today() - timedelta(days=500)
    r = c.post("/api/portal/upload", data=_form(token, pvr_issue_date=old.isoformat()),
               files={"pvr_file": ("pvr.pdf", _pvr_pdf(old), "application/pdf")})
    res = r.json()["results"][0]
    assert not res["ok"] and "expired" in res["message"]["en"].lower()


def test_future_date_is_rejected(client):
    c, Session = client
    _, _, token = _csp(Session)
    future = date.today() + timedelta(days=3)
    r = c.post("/api/portal/upload", data=_form(token, pvr_issue_date=future.isoformat()),
               files={"pvr_file": ("pvr.pdf", _pvr_pdf(date.today()), "application/pdf")})
    assert not r.json()["results"][0]["ok"]


def test_blurry_photo_is_rejected_and_counts_as_missing(client, monkeypatch):
    import app.ai.extraction.deterministic_extractor as ex
    monkeypatch.setattr(ex, "_ask_model", lambda scan, t: None)   # no vision model in tests
    c, Session = client
    cid, _, token = _csp(Session)
    r = c.post("/api/portal/upload", data=_form(token, pvr_issue_date=date.today().isoformat()),
               files={"pvr_file": ("pvr.png", _blurry_png(), "image/png")})
    res = r.json()["results"][0]
    # Told at once what to fix; nothing stored, so it still counts as missing.
    assert not res["ok"] and ("blurry" in res["message"]["en"] or "not visible" in res["message"]["en"])
    s = Session()
    assert s.query(Document).filter_by(csp_id=cid).count() == 0
    s.close()


def test_fake_file_type_rejected(client):
    c, Session = client
    _, _, token = _csp(Session)
    r = c.post("/api/portal/upload", data=_form(token, pvr_issue_date=date.today().isoformat()),
               files={"pvr_file": ("pvr.pdf", b"<html><script>alert(1)</script></html>", "application/pdf")})
    assert not r.json()["results"][0]["ok"]


def test_new_message_reuses_the_live_link(client):
    """A reminder drafted later must not kill the link the CSP already has."""
    _, Session = client
    cid, _, token = _csp(Session)
    s = Session()
    csp = s.get(CSP, cid)
    row = resolve_token(s, token)
    row.requested_types = ["POLICE_VERIFICATION"]
    s.commit()
    again = issue_upload_link(s, csp, ["AGREEMENT"])
    s.commit()
    assert again.endswith(token) and resolve_token(s, token) is not None
    assert set(resolve_token(s, token).requested_types) == {"POLICE_VERIFICATION", "AGREEMENT"}
    s.close()


def _approved_whatsapp(Session, cid, link="https://x/upload?token=abc"):
    from app.models import OutboundMessage, OutboundStatus
    s = Session()
    m = OutboundMessage(csp_id=cid, template_name="UPLOAD_MISSING", channel="WHATSAPP", destination="9876500000",
                        payload_json={"subject": "", "body": "hello", "link": link}, status=OutboundStatus.APPROVED,
                        idempotency_key=uuid.uuid4().hex, recipient_role="CSP", attempts=0)
    s.add(m)
    s.commit()
    mid = m.id
    s.close()
    return mid


def test_agent_pull_includes_link_and_is_not_listed_twice(client, monkeypatch):
    c, Session = client
    monkeypatch.setattr(auth, "AGENT_API_KEY", "agent-key")
    cid, _, _ = _csp(Session)
    mid = _approved_whatsapp(Session, cid)
    h = {"X-Agent-Key": "agent-key"}
    first = [m for m in c.get("/api/agent/outbox", headers=h).json()["messages"] if m["id"] == mid]
    assert first and first[0]["link"] == "https://x/upload?token=abc"
    from app.comms.outbound import process_outbox
    s = Session()
    process_outbox(s)          # the 2-minute worker job must leave a pulled message alone
    s.close()
    again = [m for m in c.get("/api/agent/outbox", headers=h).json()["messages"] if m["id"] == mid]
    assert again == []
    r = c.post(f"/api/agent/outbox/{mid}/ack", headers={"Authorization": "Bearer agent-key"}, json={"status": "SENT"})
    assert r.status_code == 200 and r.json()["status"] == "SENT"


def test_agent_can_fetch_a_csps_link(client, monkeypatch):
    c, Session = client
    monkeypatch.setattr(auth, "AGENT_API_KEY", "agent-key")
    cid, code, token = _csp(Session)
    r = c.get(f"/api/agent/csp/{code}/link", headers={"X-Agent-Key": "agent-key"})
    assert r.status_code == 200 and r.json()["link"].endswith(token)


def test_contact_edit_is_queued_not_applied(client):
    c, Session = client
    cid, _, token = _csp(Session)
    issue = date.today() - timedelta(days=5)
    c.post("/api/portal/upload", data=_form(token, mobile="9123456789", pvr_issue_date=issue.isoformat()),
           files={"pvr_file": ("pvr.pdf", _pvr_pdf(issue), "application/pdf")})
    s = Session()
    assert s.get(CSP, cid).phone == "9876500000"
    from app.models import ContactChangeRequest
    assert s.query(ContactChangeRequest).filter_by(csp_id=cid, field="mobile").one().new_value == "9123456789"
    s.close()


def test_admin_api_needs_key_when_set(client, monkeypatch):
    c, _ = client
    monkeypatch.setattr(auth, "ADMIN_API_KEY", "secret-test-key")
    assert c.get("/api/hub/summary").status_code == 401
    assert c.get("/api/hub/summary", headers={"X-API-Key": "wrong"}).status_code == 401
    assert c.get("/api/hub/summary", headers={"X-API-Key": "secret-test-key"}).status_code == 200


def test_agent_api_needs_its_own_key(client, monkeypatch):
    c, _ = client
    monkeypatch.setattr(auth, "AGENT_API_KEY", "agent-key")
    assert c.get("/api/agent/outbox").status_code == 401
    assert c.get("/api/agent/outbox", headers={"X-Agent-Key": "agent-key"}).status_code == 200


def test_slab_lists_can_show_only_csps_with_an_expired_document(client, monkeypatch):
    from app.compliance import refresh_category
    c, Session = client
    monkeypatch.setattr(auth, "ADMIN_API_KEY", "")
    s = Session()
    tag = uuid.uuid4().hex[:6]
    code = f"8Z{int(tag, 16) % 1000000:06d}"
    csp = CSP(name=f"Slab CSP {tag}", current_code=code, lookup_code=code, is_active_in_calling_sheet=True)
    s.add(csp)
    s.flush()
    s.add(Document(csp_id=csp.id, document_type="AGREEMENT", sha256=uuid.uuid4().hex, readability="READABLE",
                   status=DocumentStatus.EXPIRED, is_current=True,
                   issue_date=date.today() - timedelta(days=800), expiry_date=date.today() - timedelta(days=10)))
    s.flush()
    refresh_category(s, csp)
    s.commit()
    assert csp.category == 3                       # one document on file
    s.close()
    summary = c.get("/api/hub/summary").json()
    assert summary["categories_with_expired"]["3"] >= 1
    rows = c.get(f"/api/hub/csps?category=3&expired=1&q={code}").json()["rows"]
    assert [r["code"] for r in rows] == [code] and rows[0]["docs"]["AGREEMENT"]["status"] == "EXPIRED"
    assert summary["sub_slabs"]["3.1"]["count"] >= 1 and summary["sub_slabs"]["3.1"]["label"] == "Only Agreement on file"
    rows = c.get(f"/api/hub/csps?category=3&sub=3.1&q={code}").json()["rows"]
    assert [r["sub_slab"] for r in rows] == ["3.1"]
    assert c.get(f"/api/hub/csps?category=3&sub=3.2&q={code}").json()["rows"] == []


def _photo(w, h, value=200, text=True, blur=0):
    import numpy as np
    import cv2
    img = np.full((h, w), value, np.uint8)
    if text:
        for i in range(25):
            cv2.putText(img, "CHARACTER CERTIFICATE Date 12/05/2025 no adverse entry", (30, 60 + i * 50),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.9, 20 if value > 100 else 60, 2)
    if blur:
        img = cv2.GaussianBlur(img, (blur, blur), 0)
    return cv2.imencode(".jpg", img)[1].tobytes()


def test_photo_check_names_the_problem():
    from app.ocr_service import photo_problem
    assert photo_problem(_photo(400, 560)) == "too_small"           # forwarded thumbnail / screenshot
    assert photo_problem(_photo(1000, 1400, value=25)) == "too_dark"
    assert photo_problem(_photo(1000, 1400, value=250, text=False)) == "washed_out"
    assert photo_problem(_photo(1000, 1400, blur=41)) == "blurry"
    assert photo_problem(_photo(1000, 1400)) is None                 # a usable photo goes on to OCR
    assert photo_problem(b"%PDF-1.4 ...") is None                    # PDFs are never pre-rejected


def test_hub_messages_split_by_slab_and_type(client, monkeypatch):
    from app.models import OutboundMessage, OutboundStatus
    c, Session = client
    monkeypatch.setattr(auth, "ADMIN_API_KEY", "")
    s = Session()
    tag = uuid.uuid4().hex[:6]
    a = CSP(name="Hub A", current_code=f"8H{int(tag, 16) % 1000000:06d}", category=4, is_active_in_calling_sheet=True)
    b = CSP(name="Hub B", current_code=f"8J{int(tag, 16) % 1000000:06d}", category=1, is_active_in_calling_sheet=True)
    s.add_all([a, b])
    s.flush()
    for csp, tpl in ((a, "ONBOARD_ALL"), (b, "RENEWAL_NOTICE"), (b, "ESCALATION_RM")):
        s.add(OutboundMessage(csp_id=csp.id, template_name=tpl, channel="EMAIL", destination="x@y.z",
                              status=OutboundStatus.QUEUED_FOR_REVIEW, payload_json={"body": "hi"}))
    s.commit()
    a_code, b_code = a.current_code, b.current_code
    s.close()
    base = "/api/hub/messages?channel=EMAIL&status=QUEUED_FOR_REVIEW&size=200"
    onboard = c.get(base + "&kind=onboard").json()["rows"]
    assert a_code in [m["csp_code"] for m in onboard] and b_code not in [m["csp_code"] for m in onboard]
    assert c.get(base + "&slab=&kind=").status_code == 200      # "All slabs" / "All types"
    slab1 = c.get(base + "&slab=1").json()
    assert {m["template"] for m in slab1["rows"] if m["csp_code"] == b_code} == {"RENEWAL_NOTICE", "ESCALATION_RM"}
    assert slab1["by_kind"]["onboard"] >= 1 and slab1["by_slab"]["4"] >= 1


def test_upload_page_posts_relative_so_it_works_behind_a_public_sub_path(client):
    c, Session = client
    _, _, token = _csp(Session)
    page = c.get(f"/upload?token={token}").text
    assert 'fetch("api/portal/upload"' in page and 'fetch("/api/portal/upload"' not in page


# ------------------------------------------- "Slab 5": form uploads page
def test_form_uploads_record_what_was_accepted_and_what_was_turned_away(client, monkeypatch):
    c, Session = client
    cid, code, token = _csp(Session)
    monkeypatch.setattr(auth, "ADMIN_API_KEY", "k")
    key = {"X-API-Key": "k"}
    pvr_date = date.today() - timedelta(days=30)
    r = c.post("/api/portal/upload", data=_form(token, pvr_issue_date=pvr_date.isoformat(),
                                                iibf_issue_date=pvr_date.isoformat()),
               files={"pvr_file": ("pvr.pdf", _pvr_pdf(pvr_date), "application/pdf"),
                      "iibf_file": ("iibf.pdf", _pvr_pdf(pvr_date), "application/pdf")})   # a PVR in the IIBF box
    assert r.status_code == 200
    shown = r.json()["results"]
    assert all(set(x) <= {"section", "ok", "message", "issue_date", "expiry_date"} for x in shown)  # nothing internal

    d = c.get("/api/hub/form-uploads", headers=key).json()
    [row] = [x for x in d["rows"] if x["csp"]["id"] == cid]
    by = {x["section"]: x for x in row["results"]}
    assert row["outcome"] == "partly" and by["pvr"]["ok"] and by["pvr"]["document_id"]
    assert not by["iibf"]["ok"] and by["iibf"]["reason"] == "wrong document" and by["iibf"]["kept_file"]
    assert by["iibf"]["read_as"] == "POLICE_VERIFICATION" and row["slab_after"] == 3
    f = c.get(f"/api/hub/form-uploads/{row['id']}/file/{by['iibf']['i']}", headers=key)
    assert f.status_code == 200 and f.content[:4] == b"%PDF"
    assert c.get(f"/api/hub/form-uploads/{row['id']}/file/{by['pvr']['i']}", headers=key).status_code == 404
    assert c.get("/api/hub/summary", headers=key).json()["form_uploads"]["csps"] >= 1
    only_partly = c.get("/api/hub/form-uploads?outcome=partly", headers=key).json()["rows"]
    assert all(x["outcome"] == "partly" for x in only_partly)
    s = Session()
    assert s.get(CSP, cid).category == 3            # still in its own slab as well
    s.close()


# ------------------------------------------- photos of pages -> one PDF
def _page_photo(text="PAGE", w=1200, h=1600) -> bytes:
    import cv2
    import numpy as np
    img = np.full((h, w, 3), 235, np.uint8)
    for i in range(12):
        cv2.putText(img, f"{text} line {i} CHARACTER CERTIFICATE", (40, 120 + 110 * i), cv2.FONT_HERSHEY_SIMPLEX, 1.3, (20, 20, 20), 3)
    return cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 97])[1].tobytes()


def test_photos_of_pages_become_one_small_pdf(client, monkeypatch):
    import fitz
    from app.api import portal
    c, Session = client
    cid, _, token = _csp(Session)
    seen = {}
    issue = date.today() - timedelta(days=20)

    def fake_read(data, filename, **kw):
        seen["data"] = data
        return {"readability": "READABLE", "document_type": "POLICE_VERIFICATION", "compliance_status": "VALID",
                "start_date": issue.isoformat(), "expiry_date": (issue + timedelta(days=365)).isoformat(),
                "date_source": "PVR_DIGITAL_SIGNATURE_DATE", "confidence": 0.9}
    monkeypatch.setattr(portal, "extract_document_fields_deterministic", fake_read)
    big = [_page_photo("one"), _page_photo("two"), _page_photo("three")]
    r = c.post("/api/portal/upload", data=_form(token, pvr_issue_date=issue.isoformat()),
               files=[("pvr_file", (f"p{i}.jpg", b, "image/jpeg")) for i, b in enumerate(big)])
    assert r.json()["results"][0]["ok"], r.text
    pdf = fitz.open(stream=seen["data"], filetype="pdf")
    assert pdf.page_count == 3 and seen["data"][:5] == b"%PDF-"
    s = Session()
    d = s.query(Document).filter_by(csp_id=cid).one()
    assert d.mime_type == "application/pdf"
    s.close()


def test_one_pdf_or_photos_not_both_and_bad_page_is_named(client):
    c, Session = client
    _, _, token = _csp(Session)
    issue = date.today() - timedelta(days=20)
    mixed = c.post("/api/portal/upload", data=_form(token, pvr_issue_date=issue.isoformat()),
                   files=[("pvr_file", ("a.pdf", _pvr_pdf(issue), "application/pdf")),
                          ("pvr_file", ("b.jpg", _page_photo(), "image/jpeg"))]).json()["results"][0]
    assert not mixed["ok"] and "not both" in mixed["message"]["en"]
    bad = c.post("/api/portal/upload", data=_form(token, pvr_issue_date=issue.isoformat()),
                 files=[("pvr_file", ("a.jpg", _page_photo(), "image/jpeg")),
                        ("pvr_file", ("b.png", _blurry_png(), "image/png"))]).json()["results"][0]
    assert not bad["ok"] and bad["message"]["en"].startswith("Page 2:")


def test_shrink_photo_keeps_it_readable_and_small():
    from app.pages import MAX_SIDE_PX, shrink_photo
    from PIL import Image
    raw = _page_photo(w=3000, h=4000)
    small = shrink_photo(raw)
    assert max(Image.open(io.BytesIO(small)).size) == MAX_SIDE_PX and len(small) < len(raw)


# ------------------- readable but not recognised: a person decides, not "wrong"
def _plain_pdf(lines: list[str]) -> bytes:
    import fitz
    doc = fitz.open()
    page = doc.new_page()
    for i, line in enumerate(lines):
        page.insert_text((50, 80 + 16 * i), line, fontsize=10)
    return doc.tobytes()


UNKNOWN_WORDING = ["OFFICE OF THE DISTRICT WELFARE BOARD, DISTRICT EXAMPLE",
                   "Ref No. 1234/SB  Dated: 12-08-2026",
                   "Subject: Report regarding the person named below.",
                   "The above named person resides at the given address and was enquired by the beat staff.",
                   "Signed and sealed by the officer in charge of the special branch office of the district."]


def test_readable_upload_with_unknown_wording_goes_to_review_not_rejected(client, monkeypatch):
    import app.ai.extraction.deterministic_extractor as ex
    from app.models import ManualReviewQueue
    monkeypatch.setattr(ex, "_ask_model", lambda scan, t: None)       # no model: rules alone can't name it
    c, Session = client
    cid, _, token = _csp(Session)
    typed = date.today() - timedelta(days=30)
    r = c.post("/api/portal/upload", data=_form(token, pvr_issue_date=typed.isoformat()),
               files={"pvr_file": ("scan_0042.pdf", _plain_pdf(UNKNOWN_WORDING), "application/pdf")})
    res = r.json()["results"][0]
    assert res["ok"] and "team will check" in res["message"]["en"]
    s = Session()
    d = s.query(Document).filter_by(csp_id=cid).one()
    assert d.status == DocumentStatus.NEEDS_REVIEW and not d.is_current      # doesn't count yet
    q = s.query(ManualReviewQueue).filter_by(document_id=d.id).one()
    assert "wasn't recognised" in q.reason and typed.strftime("%d-%m-%Y") in q.reason
    assert s.get(CSP, cid).category == 4
    s.close()


def test_another_known_document_in_the_wrong_box_is_still_wrong(client):
    c, Session = client
    _, _, token = _csp(Session)
    issue = date.today() - timedelta(days=30)
    res = c.post("/api/portal/upload", data=_form(token, iibf_issue_date=issue.isoformat()),
                 files={"iibf_file": ("x.pdf", _pvr_pdf(issue), "application/pdf")}).json()["results"][0]
    assert not res["ok"] and "not the right document" in res["message"]["en"]


def test_hyphenated_hindi_title_is_recognised():
    from app.ocr_service import classify_allowlist_gate
    assert classify_allowlist_gate("कार्यालय पुलिस अधीक्षक चरित्र प्रमाण-पत्र")[1] == "POLICE_VERIFICATION"
    assert classify_allowlist_gate("आचरण प्रमाण पत्र")[1] == "POLICE_VERIFICATION"


def test_a_review_without_a_date_cannot_be_accepted(client, monkeypatch):
    import app.ai.extraction.deterministic_extractor as ex
    from app.models import ManualReviewQueue
    monkeypatch.setattr(ex, "_ask_model", lambda scan, t: None)
    monkeypatch.setattr(auth, "ADMIN_API_KEY", "k")
    c, Session = client
    cid, _, token = _csp(Session)
    no_date = [l for l in UNKNOWN_WORDING if "Dated" not in l]
    c.post("/api/portal/upload", data=_form(token, pvr_issue_date=(date.today() - timedelta(days=3)).isoformat()),
           files={"pvr_file": ("scan_0042.pdf", _plain_pdf(no_date), "application/pdf")})
    s = Session()
    d = s.query(Document).filter_by(csp_id=cid).one()
    qid = s.query(ManualReviewQueue).filter_by(document_id=d.id).one().id
    s.close()
    hdr = {"X-API-Key": "k", "X-Requested-With": auth.CSRF_HEADER_VALUE}
    assert c.post(f"/api/hub/review/{qid}/resolve", json={"accept": True}, headers=hdr).status_code == 422
    ok = c.post(f"/api/hub/review/{qid}/resolve", json={"accept": True, "issue_date": "2026-08-12"}, headers=hdr)
    assert ok.status_code == 200
    s = Session()
    d = s.get(Document, d.id)
    assert d.status == DocumentStatus.MANUAL_VERIFIED and d.expiry_date is not None and d.is_current
    s.close()


# --------------- scanned PDFs whose hidden text layer is scanner garbage
def _scan_with_garbage_layer(issue: date) -> bytes:
    """A scanned page (an image of a clean certificate) plus the kind of
    invisible text layer some scanners add: mostly garbage."""
    import fitz
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("L", (1700, 2200), 255)
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("DejaVuSans.ttf", 34)
    except OSError:
        font = ImageFont.load_default()
    lines = ["Government of Uttar Pradesh  CHARACTER CERTIFICATE",
             f"Application No. 202503277853   Date: {issue.strftime('%d-%m-%Y')}",
             "This is to certify that no adverse entry was found against the candidate",
             "in the police records. This certificate is valid only for one year.",
             f"Digitally signed by TEST OFFICER Date: {issue.strftime('%Y.%m.%d')} 18:29:17"]
    for i, line in enumerate(lines):
        draw.text((80, 200 + 90 * i), line, fill=0, font=font)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    page.insert_image(page.rect, stream=buf.getvalue())
    garbage = ("ft_rrq q{fir( gm\"q a\"ft$e I Eflc gft{ el$e&a 6r qilqfaq Oftiee $uperintendent of Psliee "
               "qke c.{rsr-qd Certif icate <isqrl fqqia 0?/0512026 &d€:2828.O5-87 t2:46:84 ") * 6
    page.insert_text((20, 40), garbage, fontsize=4, render_mode=3)          # invisible, like a scanner's layer
    return doc.tobytes()


def test_text_layer_quality_tells_garbage_from_real_text():
    from app.ocr_service import TEXT_LAYER_MIN_QUALITY, text_layer_quality
    assert text_layer_quality("ft_rrq q{fir( gm\"q a\"ft$e I Eflc gft{ el$e&a 6r qilqfaq") < TEXT_LAYER_MIN_QUALITY
    assert text_layer_quality("This is to certify that no adverse entry was found against the candidate "
                              "in the police records dated 12-05-2025.") >= TEXT_LAYER_MIN_QUALITY


def test_a_scan_with_a_garbage_text_layer_is_read_from_the_image(monkeypatch):
    import app.ai.extraction.deterministic_extractor as ex
    import app.ocr_service as o
    monkeypatch.setattr(ex, "_ask_model", lambda scan, t: None)
    monkeypatch.setattr(o, "_cache_on", False)
    issue = date.today() - timedelta(days=40)
    r = ex.extract_document_fields_deterministic(_scan_with_garbage_layer(issue), "pvr.pdf")
    assert r["readability"] == "READABLE" and r["document_type"] == "POLICE_VERIFICATION", r
    assert r["start_date"] == issue.isoformat() and "OCR" in r["ocr_method"]
