"""Every path a CSP, a DC or a visitor can take on the public pages, posted
with exactly the fields the real pages send (app/web/portal.html,
dc_master.html, get_link.html), so a field the server misreads (like the
CSP form's read-only "dc" = DC name, taken for the master link on 21-10)
is caught here."""
import uuid
from datetime import date, timedelta

from sqlalchemy.orm import sessionmaker

from app.api import portal
from app.comms import outbound
from app.comms.whatsapp import SendOutcome
from app.models import (ContactChangeRequest, CspQuestion, CSP, Document, InternalUser, OutboundMessage,
                        OutboundStatus, PortalToken)
from tests.test_portal_and_api import _csp, _pvr_pdf, client  # noqa: F401  (fixture)

ISSUE = date.today() - timedelta(days=20)


def _with_dc(Session, cid, name="Ajay"):
    s = Session()
    dc = InternalUser(name=f"{name} {uuid.uuid4().hex[:4]}", role="DC", phone="9000000055")
    s.add(dc)
    s.flush()
    c = s.get(CSP, cid)
    c.dc_id = dc.id
    s.commit()
    rm_name = s.get(InternalUser, c.rm_id).name if c.rm_id else ""
    out = (c.name, c.phone or "", c.email or "", rm_name, dc.name)
    s.close()
    return out


def _csp_form(token, name, mobile, email, rm, dc, **files_dates):
    """What portal.html posts: FormData($("f")) + token."""
    return {"name": name, "mobile": mobile, "email": email, "rm": rm, "dc": dc, "token": token, **files_dates}


def _pvr(issue=ISSUE, holder="TEST PERSON"):
    return {"pvr_file": ("pvr.pdf", _pvr_pdf(issue, holder), "application/pdf")}


# ------------------------------------------------------------ CSP's own link
def test_csp_form_exactly_as_the_page_sends_it(client):
    c, Session = client
    cid, _, token = _csp(Session)
    name, mobile, email, rm, dc = _with_dc(Session, cid)
    assert "__CONTEXT_JSON__" not in c.get(f"/upload?token={token}").text
    r = c.post("/api/portal/upload", data=_csp_form(token, name, mobile, "new@example.com", rm, dc,
                                                     pvr_issue_date=ISSUE.isoformat()), files=_pvr())
    assert r.status_code == 200, r.text
    assert r.json()["results"][0]["ok"]
    s = Session()
    assert s.query(Document).filter_by(csp_id=cid).count() == 1
    changes = s.query(ContactChangeRequest).filter_by(csp_id=cid).all()
    assert [(x.field, x.new_value) for x in changes] == [("email", "new@example.com")]   # locked fields not "changed"
    s.close()


def test_csp_with_no_mobile_on_record_can_upload(client):
    c, Session = client
    cid, _, token = _csp(Session)
    s = Session()
    s.get(CSP, cid).phone = None
    s.commit()
    s.close()
    name, mobile, email, rm, dc = _with_dc(Session, cid)
    assert mobile == ""
    r = c.post("/api/portal/upload", data=_csp_form(token, name, "", email, rm, dc, pvr_issue_date=ISSUE.isoformat()),
               files=_pvr())
    assert r.status_code == 200 and r.json()["results"][0]["ok"], r.text


def test_page_script_no_longer_blocks_a_missing_mobile():
    page = (portal.PAGE).read_text(encoding="utf-8")
    assert "valid 10-digit mobile" not in page


def test_two_documents_one_good_one_bad_date(client):
    c, Session = client
    cid, _, token = _csp(Session)
    name, mobile, email, rm, dc = _with_dc(Session, cid)
    future = (date.today() + timedelta(days=5)).isoformat()
    r = c.post("/api/portal/upload",
               data=_csp_form(token, name, mobile, email, rm, dc, pvr_issue_date=ISSUE.isoformat(), iibf_issue_date=future),
               files=[("pvr_file", ("pvr.pdf", _pvr_pdf(ISSUE), "application/pdf")),
                      ("iibf_file", ("iibf.pdf", _pvr_pdf(ISSUE, "X"), "application/pdf"))])
    res = {x["section"]: x for x in r.json()["results"]}
    assert res["pvr"]["ok"] and not res["iibf"]["ok"]


def test_no_file_chosen_is_a_clear_message_not_a_crash(client):
    c, Session = client
    cid, _, token = _csp(Session)
    name, mobile, email, rm, dc = _with_dc(Session, cid)
    r = c.post("/api/portal/upload", data=_csp_form(token, name, mobile, email, rm, dc))
    assert r.status_code == 422 and "choose" in r.json()["detail"]["en"].lower()


def test_dead_link_shows_the_get_link_page_and_a_submit_says_why(client):
    c, Session = client
    cid, _, token = _csp(Session)
    s = Session()
    s.query(PortalToken).filter_by(csp_id=cid).update({"revoked_at": PortalToken.created_at})
    s.commit()
    s.close()
    assert "Get your upload link" in c.get(f"/upload?token={token}").text
    r = c.post("/api/portal/upload", data=_csp_form(token, "x", "", "", "", "Ajay", pvr_issue_date=ISSUE.isoformat()),
               files=_pvr())
    assert r.status_code == 403 and r.json()["detail"]["en"]


def test_question_box_as_the_page_sends_it(client):
    c, Session = client
    cid, _, token = _csp(Session)
    r = c.post("/upload", data={"action": "question", "token": token, "category": "UPLOAD_PROBLEM",
                                "text": "Link not opening", "mobile": "9876500000"})
    assert r.status_code == 200 and r.json()["ok"]
    s = Session()
    assert s.query(CspQuestion).filter_by(csp_id=cid).count() == 1
    s.close()


# -------------------------------------------------------- DC master link
def test_master_link_exactly_as_the_dc_page_sends_it(client):
    from app import master_link
    c, Session = client
    cid, code, _ = _csp(Session)
    s = Session()
    dc_token = master_link.create(s, "t").split("dc=")[1]
    s.commit()
    s.close()
    assert c.post("/upload", data={"dc": dc_token, "action": "dc_context", "code": f" {code.lower()} "}).status_code == 200
    r = c.post("/api/portal/upload", data={"pvr_issue_date": ISSUE.isoformat(), "dc": dc_token, "code": code},
               files=_pvr())
    assert r.status_code == 200 and r.json()["results"][0]["ok"], r.text
    s = Session()
    assert s.query(Document).filter_by(csp_id=cid).one().upload_channel == "DC_MASTER_LINK"
    s.close()
    assert c.post("/api/portal/upload", data={"pvr_issue_date": ISSUE.isoformat(), "dc": "wrong", "code": code},
                  files=_pvr()).status_code == 403


# ------------------------------------------------------- "get your link"
def test_get_link_form_and_the_fallback_when_the_link_message_is_refused(client, monkeypatch):
    c, Session = client
    cid, code, _ = _csp(Session)
    monkeypatch.setattr(portal, "SessionLocal", sessionmaker(bind=Session.kw["bind"]))   # the test database
    sent = []

    def fake_send(message_id, phone10, template_key, *a, **k):
        sent.append(template_key)
        if template_key == "LINK_REQUEST":
            return SendOutcome("FAILED", error="Bulk Sender: There is already Hindi content for this template.")
        return SendOutcome("SENT", provider_message_id="j1")
    monkeypatch.setattr(outbound, "WHATSAPP_MODE", "wabs")
    monkeypatch.setattr(outbound, "send_wabs", fake_send)
    r = c.post("/upload", data={"code": code, "mobile": "9876500000"})             # get_link.html form
    assert r.status_code == 200 and "link" in r.text.lower()                        # runs _send_link
    s = Session()
    msgs = {m.template_name: m.status for m in s.query(OutboundMessage).filter(OutboundMessage.csp_id == cid,
                                                                               OutboundMessage.stage == "LINK_REQUEST")}
    s.close()
    assert sent[0] == "LINK_REQUEST" and msgs["LINK_REQUEST"] == OutboundStatus.FAILED
    assert sent[1] == "ONBOARD_ALL" and msgs["ONBOARD_ALL"] == OutboundStatus.SENT   # same link, approved layout


# ------------------------------------------------------ agreement pages
def _agreement_pdf(n_pages, issue=ISSUE):
    import pymupdf
    d = pymupdf.open()
    for i in range(n_pages):
        p = d.new_page()
        p.insert_text((40, 60), "CUSTOMER SERVICE POINT AGREEMENT" if i == 0 else f"Clause page {i + 1}", fontsize=10)
        if i == 0:
            p.insert_text((40, 80), f'On this day of, {issue:%d/%m/%Y} ("Effective Date"), CSP Name TEST', fontsize=9)
            p.insert_text((40, 100), "valid for a period of three (3) years", fontsize=9)
    return d.tobytes()


def test_agreement_with_few_pages_is_refused_or_reviewed(client):
    from app import agreement_pages
    c, Session = client
    cid, _, token = _csp(Session)
    name, mobile, email, rm, dc = _with_dc(Session, cid)

    def up(n):
        return c.post("/api/portal/upload", data=_csp_form(token, name, mobile, email, rm, dc,
                                                           agreement_issue_date=ISSUE.isoformat()),
                      files={"agreement_file": (f"a{n}.pdf", _agreement_pdf(n), "application/pdf")}).json()["results"][0]
    one = up(1)
    assert not one["ok"] and one["message"] == agreement_pages.message(1)
    s = Session()
    assert s.query(Document).filter_by(csp_id=cid).count() == 0          # nothing filed
    s.close()
    seven = up(7)
    assert seven["ok"] and "check" in seven["message"]["en"].lower()     # "our team will check it"
    s = Session()
    d = s.query(Document).filter_by(csp_id=cid).one()
    assert d.status.value == "NEEDS_REVIEW" and "Only 7 page" in d.extracted_fields["needs_confirmation"]
    s.close()
    thirteen = up(13)
    assert thirteen["ok"] and "check" not in thirteen["message"]["en"].lower()


def test_verdict_by_pages():
    from app.agreement_pages import verdict
    assert [verdict(n) for n in (1, 4, 5, 9, 10, 13, 0, None)] == ["refuse", "refuse", "review", "review", None, None, None, None]


def test_the_same_file_as_a_rejected_one_is_turned_away_with_the_reason(client):
    from app.models import DocumentStatus
    c, Session = client
    cid, _, token = _csp(Session)
    name, mobile, email, rm, dc = _with_dc(Session, cid)
    data = _pvr_pdf(ISSUE)
    send = lambda: c.post("/api/portal/upload", data=_csp_form(token, name, mobile, email, rm, dc,
                                                                pvr_issue_date=ISSUE.isoformat()),
                          files={"pvr_file": ("pvr.pdf", data, "application/pdf")}).json()["results"][0]
    assert send()["ok"]
    s = Session()
    d = s.query(Document).filter_by(csp_id=cid).one()
    d.status = DocumentStatus.REJECTED
    s.commit()
    s.close()
    again = send()
    assert not again["ok"] and again["message"]["en"] == portal.MSG["dup_rejected"][1]


def test_a_date_read_as_today_while_another_was_typed_goes_to_a_person(client, monkeypatch):
    c, Session = client
    cid, _, token = _csp(Session)
    name, mobile, email, rm, dc = _with_dc(Session, cid)
    today = date.today()
    monkeypatch.setattr(portal, "extract_document_fields_deterministic", lambda *a, **k: {
        "readability": "READABLE", "document_type": "POLICE_VERIFICATION", "compliance_status": "VALID",
        "start_date": today.isoformat(), "expiry_date": (today + timedelta(days=180)).isoformat(),
        "validity_rule_used": "PVR_STATED_6_MONTHS", "date_source": "PVR_DIGITAL_SIGNATURE_DATE"})
    res = c.post("/api/portal/upload", data=_csp_form(token, name, mobile, email, rm, dc,
                                                       pvr_issue_date=(today - timedelta(days=79)).isoformat()),
                 files=_pvr()).json()["results"][0]
    assert res["ok"] and res["message"]["en"] == portal.MSG["received_check"][1]
    s = Session()
    d = s.query(Document).filter_by(csp_id=cid).one()
    assert d.status.value == "NEEDS_REVIEW" and "photo's date" in d.extracted_fields["needs_confirmation"]
    s.close()


def test_an_admin_takes_a_refused_file_and_accepts_it_in_the_review_queue(client, monkeypatch):
    from app import auth
    from app.compliance import evaluate
    from app.models import AgreementEvent, ManualReviewQueue, ReviewStatus
    monkeypatch.setattr(auth, "ADMIN_API_KEY", "k")
    hdr = {"X-API-Key": "k", "X-Requested-With": auth.CSRF_HEADER_VALUE}
    c, Session = client
    cid, _, token = _csp(Session)
    name, mobile, email, rm, dc = _with_dc(Session, cid)
    res = c.post("/api/portal/upload", data=_csp_form(token, name, mobile, email, rm, dc,
                                                       agreement_issue_date=ISSUE.isoformat()),
                 files={"agreement_file": ("a.pdf", _agreement_pdf(1), "application/pdf")}).json()["results"][0]
    assert not res["ok"]                                              # refused (1 page)
    s = Session()
    ev = s.query(AgreementEvent).filter_by(csp_id=cid, event_type="PORTAL_UPLOAD").one()
    ev_id = ev.id
    s.close()
    row = next(r for r in c.get("/api/hub/form-uploads", headers=hdr).json()["rows"] if r["id"] == ev_id)
    assert row["results"][0]["kept_file"] and not row["results"][0]["taken"]
    took = c.post(f"/api/hub/form-uploads/{ev_id}/take/0", headers=hdr).json()
    assert took["document_id"] and not took["already"]
    assert c.post(f"/api/hub/form-uploads/{ev_id}/take/0", headers=hdr).json()["already"]      # once only
    s = Session()
    q = s.query(ManualReviewQueue).filter_by(document_id=took["document_id"], status=ReviewStatus.PENDING).one()
    assert q.reason.startswith("Taken by")
    qid = q.id
    s.close()
    ok = c.post(f"/api/hub/review/{qid}/resolve", headers=hdr,
                json={"accept": True, "issue_date": ISSUE.isoformat(), "document_type": "AGREEMENT", "validity_months": 36})
    assert ok.status_code == 200, ok.text
    s = Session()
    st = evaluate(s, s.get(CSP, cid)).docs["AGREEMENT"]
    assert st.status == "VALID" and st.expiry_date.year == ISSUE.year + 3
    s.close()
