"""The CSP common link (app/csp_common_link.py) and the final notice
(app/final_notice.py): KO code + last 4 digits of the mobile open the CSP's
own page, wrong tries lock, nothing about anyone is shown before that; the
final notice is drafted for Pending by RM's CSPs, with the common link."""
import uuid
from datetime import datetime

from app import auth, csp_common_link, final_notice
from app.models import AgreementEvent, CSP, InternalUser, OutboundMessage, OutboundStatus
from tests.test_portal_and_api import client  # noqa: F401  (fixture)

HDR = {"X-API-Key": "k", "X-Requested-With": auth.CSRF_HEADER_VALUE}


def _world(Session, phone="9876543210", n=2):
    s = Session()
    tag = uuid.uuid4().hex[:6]
    rm = InternalUser(name=f"RM {tag}", role="RM")
    s.add(rm)
    s.flush()
    csps = []
    for i in range(n):
        code = f"8Q{(int(tag, 16) + i) % 1000000:06d}"
        c = CSP(name=f"Common CSP {i} {tag}", current_code=code, lookup_code=code, phone=phone,
                whatsapp_number=phone, rm_id=rm.id, category=4, is_active_in_calling_sheet=True)
        s.add(c)
        csps.append(c)
    s.flush()
    url = csp_common_link.create(s, "test")
    s.commit()
    out = {"rm": rm.id, "rm_name": rm.name, "codes": [c.current_code for c in csps], "ids": [c.id for c in csps],
           "key": url.split("c=", 1)[1]}
    s.close()
    return out


def _open(c, key, code, last4):
    return c.post("/upload", data={"action": "csp_open", "key": key, "code": code, "mobile": last4})


def test_the_page_shows_nobody_and_the_right_digits_open_the_csps_own_page(client):
    c, Session = client
    w = _world(Session)
    page = c.get(f"/upload?c={w['key']}")
    assert page.status_code == 200 and "KO" in page.text and "Common CSP" not in page.text and "9876" not in page.text
    r = _open(c, w["key"], w["codes"][0].lower(), "3210")
    assert r.status_code == 200 and "/upload?token=" in r.json()["url"]
    assert c.get("/upload" + r.json()["url"].split("/upload", 1)[1]).status_code == 200      # their own page
    s = Session()
    assert s.query(AgreementEvent).filter_by(csp_id=w["ids"][0], event_type="COMMON_LINK_OPEN").count() == 1
    s.close()


def test_wrong_tries_lock_the_ko_code_for_30_minutes(client):
    c, Session = client
    w = _world(Session)
    r = _open(c, w["key"], w["codes"][0], "1111")
    assert r.status_code == 400 and r.json()["result"] == "no_match" and "4" in r.json()["message"]["en"]
    for _ in range(4):
        r = _open(c, w["key"], w["codes"][0], "1111")
    assert r.json()["result"] == "locked" and r.status_code == 429
    assert _open(c, w["key"], w["codes"][0], "3210").json()["result"] == "locked"       # even the right digits
    assert _open(c, w["key"], w["codes"][1], "3210").json()["ok"] is True               # other CSPs are not locked
    assert _open(c, w["key"], "8Q999999", "3210").json()["result"] == "no_match"        # unknown code: same answer


def test_bad_input_no_mobile_and_a_closed_link(client):
    c, Session = client
    w = _world(Session, phone=None, n=1)
    assert _open(c, w["key"], w["codes"][0], "12").json()["result"] == "bad_input"
    assert _open(c, w["key"], w["codes"][0], "1234").json()["result"] == "no_mobile"
    s = Session()
    csp_common_link.revoke_all(s)
    s.commit()
    s.close()
    assert _open(c, w["key"], w["codes"][0], "1234").json()["result"] == "closed"
    assert "बंद" in c.get(f"/upload?c={w['key']}").text


def test_final_notice_drafts_for_pending_by_rm_with_the_common_link(client, monkeypatch):
    monkeypatch.setattr(auth, "ADMIN_API_KEY", "k")
    c, Session = client
    w = _world(Session)
    r = c.post("/api/hub/final-notice", json={"rm": str(w["rm"])}, headers=HDR)
    assert r.status_code == 200 and r.json()["drafted"] == 2
    s = Session()
    m = s.query(OutboundMessage).filter_by(csp_id=w["ids"][0], template_name="FINAL_NOTICE").one()
    body = m.payload_json["body"]
    assert m.status == OutboundStatus.QUEUED_FOR_REVIEW and m.channel == "WHATSAPP"
    assert "अंतिम सूचना" in body and f"/upload?c={w['key']}" in body and w["codes"][0] in body
    assert "पुलिस" in body and "IIBF" in body and "6 PM" in body and w["rm_name"] in body
    s.close()
    assert c.post("/api/hub/final-notice", json={"rm": str(w["rm"])}, headers=HDR).json()["already"] == 2
    s = Session()
    csp_common_link.revoke_all(s)
    s.commit()
    s.close()
    assert c.post("/api/hub/final-notice", json={"rm": str(w["rm"])}, headers=HDR).status_code == 409


def test_deadline_is_today_until_3_pm_then_tomorrow():
    assert final_notice.when(datetime(2026, 11, 4, 11, 0)) == {"when_hi": "आज 04-11-2026", "when_en": "today, 04-11-2026"}
    assert final_notice.when(datetime(2026, 11, 4, 16, 0)) == {"when_hi": "कल 05-11-2026", "when_en": "tomorrow, 05-11-2026"}


def test_whatsapp_layout_gets_the_live_link_and_documents_at_send_time(client):
    from app.comms import wabs
    from app.compliance import evaluate
    c, Session = client
    w = _world(Session, n=1)
    s = Session()
    csp = s.get(CSP, w["ids"][0])
    v = wabs.layout_values("FINAL_NOTICE", csp, evaluate(s, csp), "")
    assert v["link"].endswith(f"c={w['key']}") and "IIBF" in v["doc_en"] and v["rm_name"] == w["rm_name"]
    assert set(wabs.CLEAN_MAP["final_notice"].values()) <= {"name", "code", *v}
    s.close()
