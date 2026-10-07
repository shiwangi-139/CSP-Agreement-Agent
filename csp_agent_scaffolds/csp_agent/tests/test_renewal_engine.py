"""Follow-up engine, categories, recipient guard and templates, against the
isolated test database (tests/conftest.py never touches the main DB)."""
from datetime import date, datetime, timedelta
import uuid

import pytest

from app import renewal_engine
from app.compliance import evaluate
from app.comms import outbound
from app.comms.templates import render
from app.models import (CSP, Document, DocumentStatus, InternalUser, OutboundMessage,
                        OutboundStatus, OutreachCycle)

TODAY = date(2026, 9, 25)


@pytest.fixture(autouse=True)
def _no_real_links(monkeypatch):
    monkeypatch.setattr(renewal_engine, "issue_upload_link", lambda db, csp, types: "https://x/upload?token=t")


def _csp(db, *, email=True, rm=True, dc=True, rm_contact=True):
    tag = uuid.uuid4().hex[:6]
    code = f"9Z{int(tag, 16) % 1000000:06d}"
    rm_u = InternalUser(name=f"RM {tag}", role="RM", phone="9000000001" if rm_contact else None,
                        email=f"rm{tag}@eko.co.in" if rm_contact else None) if rm else None
    dc_u = InternalUser(name=f"DC {tag}", role="DC", phone="9000000002", email=f"dc{tag}@eko.co.in") if dc else None
    for u in (rm_u, dc_u):
        if u:
            db.add(u)
    db.flush()
    c = CSP(name=f"Test CSP {tag}", current_code=code, lookup_code=code, phone="9876543210",
            whatsapp_number="9876543210", email=f"csp{tag}@gmail.com" if email else None,
            rm_id=rm_u.id if rm_u else None, dc_id=dc_u.id if dc_u else None, is_active_in_calling_sheet=True)
    db.add(c)
    db.flush()
    return c


def _doc(db, csp, doc_type, issue, expiry, status=DocumentStatus.VALID, readability="READABLE", months=None):
    d = Document(csp_id=csp.id, document_type=doc_type, sha256=uuid.uuid4().hex, status=status,
                 issue_date=issue, expiry_date=expiry, is_current=readability == "READABLE",
                 readability=readability, validity_months=months)
    db.add(d)
    db.flush()
    return d


def _all_valid(db, csp, agreement_expiry, pvr_expiry=None):
    _doc(db, csp, "AGREEMENT", agreement_expiry - timedelta(days=3 * 365), agreement_expiry)
    _doc(db, csp, "POLICE_VERIFICATION", TODAY - timedelta(days=30), pvr_expiry or TODAY + timedelta(days=335), months=12)
    _doc(db, csp, "IIBF_CERTIFICATE", date(2022, 7, 8), None)


def _msgs(db, csp):
    return db.query(OutboundMessage).filter_by(csp_id=csp.id).order_by(OutboundMessage.id).all()


def _run_days(db, csp, start, days, send=False):
    """The daily engine run; with send=True the team sends every waiting
    draft the same day (as if approved on the dashboard)."""
    for i in range(days):
        day = start + timedelta(days=i)
        renewal_engine.run_for_csp(db, csp, day)
        db.flush()
        if send:
            for m in _msgs(db, csp):
                if m.status in renewal_engine.PENDING:
                    m.status, m.sent_at = OutboundStatus.SENT, datetime.combine(day, datetime.min.time()).replace(hour=10)
            db.flush()


# ------------------------------------------------------------ categories
def test_categories(db_session):
    db = db_session
    a = _csp(db); _all_valid(db, a, TODAY + timedelta(days=400))
    b = _csp(db); _doc(db, b, "AGREEMENT", date(2026, 1, 1), date(2029, 1, 1))
    c = _csp(db); _doc(db, c, "AGREEMENT", date(2023, 1, 1), date(2026, 1, 1), status=DocumentStatus.EXPIRED)
    d = _csp(db)
    e = _csp(db); _doc(db, e, "AGREEMENT", None, None, status=DocumentStatus.UNREADABLE, readability="UNREADABLE")
    f = _csp(db); _all_valid(db, f, TODAY - timedelta(days=5))            # all three on file, all expired
    g = _csp(db); _doc(db, g, "AGREEMENT", date(2026, 1, 1), date(2029, 1, 1))
    _doc(db, g, "IIBF_CERTIFICATE", date(2020, 1, 1), None)               # two on file, one missing
    assert evaluate(db, a, TODAY).category == 1
    assert evaluate(db, f, TODAY).category == 1 and len(evaluate(db, f, TODAY).expired) >= 1
    assert evaluate(db, g, TODAY).category == 2
    assert evaluate(db, b, TODAY).category == 3                           # one on file, two missing
    sc = evaluate(db, c, TODAY)
    assert sc.category == 3 and sc.expired == ["AGREEMENT"]               # expired counts as on file
    assert evaluate(db, d, TODAY).category == 4
    st = evaluate(db, e, TODAY)
    assert st.category == 4 and st.docs["AGREEMENT"].status == "UNREADABLE"


def test_expired_document_still_shown_with_dates(db_session):
    db = db_session
    c = _csp(db)
    _doc(db, c, "AGREEMENT", date(2023, 1, 1), date(2026, 1, 1), status=DocumentStatus.EXPIRED)
    s = evaluate(db, c, TODAY).docs["AGREEMENT"]
    assert s.status == "EXPIRED" and s.expiry_date == date(2026, 1, 1) and s.days_left < 0


# --------------------------------------------------------- renewal ladder
def test_agreement_ladder_order_and_caps(db_session):
    db = db_session
    csp = _csp(db)
    expiry = TODAY + timedelta(days=61)
    _all_valid(db, csp, expiry)
    _run_days(db, csp, TODAY, 61, send=True)

    agr = [m for m in _msgs(db, csp) if m.document_type == "AGREEMENT" and m.channel == "WHATSAPP"]
    roles = [m.recipient_role for m in agr]
    stages = [m.stage for m in agr]
    assert stages == ["T-60", "T-53", "T-46", "T-39", "T-32-RM1", "T-25", "T-18-RM2", "T-11-DC", "T-7", "T-3", "T-1"]
    assert roles.index("RM") >= 2              # at least 2 CSP messages first
    assert roles.count("RM") == 2 and roles.count("DC") == 1
    assert roles.index("DC") > len(roles) - 1 - roles[::-1].index("RM")  # DC after the 2nd RM message


def test_unsent_renewal_reminders_do_not_pile_up(db_session):
    db = db_session
    csp = _csp(db)
    _all_valid(db, csp, TODAY + timedelta(days=61))
    _run_days(db, csp, TODAY, 40)                                    # nobody sends anything
    agr = [m for m in _msgs(db, csp) if m.document_type == "AGREEMENT"]
    waiting = {m.stage for m in agr if m.status in renewal_engine.PENDING}
    assert len(waiting) == 1                                         # only the newest reminder waits
    assert "RM" not in {m.recipient_role for m in agr}               # no RM before the CSP got anything


def test_each_step_drafted_once(db_session):
    db = db_session
    csp = _csp(db)
    _all_valid(db, csp, TODAY + timedelta(days=60))
    for _ in range(3):
        renewal_engine.run_for_csp(db, csp, TODAY)
    assert len([m for m in _msgs(db, csp) if m.stage == "T-60"]) == 2   # one WhatsApp + one email


def test_late_entry_sends_one_message_not_a_burst(db_session):
    db = db_session
    csp = _csp(db)
    _all_valid(db, csp, TODAY + timedelta(days=20))
    renewal_engine.run_for_csp(db, csp, TODAY)
    msgs = [m for m in _msgs(db, csp) if m.document_type == "AGREEMENT"]
    assert {m.stage for m in msgs} == {"T-25"} and {m.recipient_role for m in msgs} == {"CSP"}


def test_newer_document_stops_the_ladder(db_session):
    db = db_session
    csp = _csp(db)
    expiry = TODAY + timedelta(days=60)
    _all_valid(db, csp, expiry)
    _run_days(db, csp, TODAY, 10)
    before = len(_msgs(db, csp))
    _doc(db, csp, "AGREEMENT", TODAY + timedelta(days=10), TODAY + timedelta(days=10 + 3 * 365))
    _run_days(db, csp, TODAY + timedelta(days=10), 40)
    agr_after = [m for m in _msgs(db, csp)[before:] if m.document_type == "AGREEMENT"]
    assert agr_after == []
    cycle = db.query(OutreachCycle).filter_by(csp_id=csp.id, document_type="AGREEMENT").first()
    assert cycle.closed_at is not None and cycle.close_reason == "NEWER_DOCUMENT_RECEIVED"


def test_six_month_pvr_uses_short_ladder(db_session):
    db = db_session
    csp = _csp(db)
    _doc(db, csp, "AGREEMENT", date(2026, 1, 1), date(2029, 1, 1))
    _doc(db, csp, "IIBF_CERTIFICATE", date(2022, 1, 1), None)
    _doc(db, csp, "POLICE_VERIFICATION", TODAY - timedelta(days=160), TODAY + timedelta(days=21), months=6)
    renewal_engine.run_for_csp(db, csp, TODAY)
    pvr = [m for m in _msgs(db, csp) if m.document_type == "POLICE_VERIFICATION"]
    assert {m.stage for m in pvr} == {"T-21"}


# ----------------------------------------------------------- upload cycle
def test_upload_cycle_cadence_and_caps(db_session):
    db = db_session
    csp = _csp(db)
    _doc(db, csp, "AGREEMENT", date(2026, 1, 1), date(2029, 1, 1))   # PVR + IIBF missing -> Cat 2
    _run_days(db, csp, TODAY, 150, send=True)
    wa = [m for m in _msgs(db, csp) if m.cycle_id and m.channel == "WHATSAPP"
          and db.get(OutreachCycle, m.cycle_id).kind == "UPLOAD"]
    steps = [(m.stage, m.recipient_role, (m.sent_at.date() - TODAY).days) for m in wa]
    assert steps[:7] == [("U-1", "CSP", 0), ("U-2", "CSP", 4), ("U-3", "CSP", 11), ("U-RM1", "RM", 18),
                         ("U-4", "CSP", 25), ("U-RM2", "RM", 32), ("U-DC", "DC", 39)]
    assert steps[7][0] == "U-W1" and steps[7][2] == 46                # then weekly
    roles = [r for _, r, _ in steps]
    assert roles.count("RM") == 2 and roles.count("DC") == 1
    assert roles.count("CSP") == 10                                   # capped
    assert all(m.template_name in ("UPLOAD_MISSING", "ESCALATION_RM", "ESCALATION_DC") for m in wa)


def test_next_reminder_waits_until_the_previous_one_is_sent(db_session):
    db = db_session
    csp = _csp(db)
    _run_days(db, csp, TODAY, 30)                                    # slab 4, nothing sent for a month
    msgs = _msgs(db, csp)
    assert {m.stage for m in msgs} == {"U-1"} and len(msgs) == 2     # one WhatsApp + one email, no pile-up
    cycle = db.query(OutreachCycle).filter_by(csp_id=csp.id, kind="UPLOAD").first()
    assert cycle.csp_messages == 0
    wa = next(m for m in msgs if m.channel == "WHATSAPP")
    wa.status, wa.sent_at = OutboundStatus.SENT, datetime(2026, 10, 25, 10)  # sent on day 30
    _run_days(db, csp, date(2026, 10, 25), 4)                        # days 30-33: too early
    assert {m.stage for m in _msgs(db, csp)} == {"U-1"}
    renewal_engine.run_for_csp(db, csp, date(2026, 10, 29))          # 4 days after the send
    assert {m.stage for m in _msgs(db, csp)} == {"U-1", "U-2"}
    email_u1 = next(m for m in msgs if m.channel == "EMAIL")
    assert email_u1.status == OutboundStatus.QUEUED_FOR_REVIEW        # the sent stage's email is left alone
    assert cycle.csp_messages == 1


def test_rejected_reminder_counts_as_handled(db_session):
    db = db_session
    csp = _csp(db)
    renewal_engine.run_for_csp(db, csp, TODAY)
    for m in _msgs(db, csp):
        outbound.reject(db, m, "tester", "not now")
        m.reviewed_at = datetime(2026, 9, 25, 12)
    renewal_engine.run_for_csp(db, csp, TODAY + timedelta(days=3))
    assert {m.stage for m in _msgs(db, csp)} == {"U-1"}
    renewal_engine.run_for_csp(db, csp, TODAY + timedelta(days=4))
    assert {m.stage for m in _msgs(db, csp)} == {"U-1", "U-2"}


def test_older_unsent_reminders_are_retired_when_a_later_one_was_sent(db_session):
    db = db_session
    csp = _csp(db)
    renewal_engine.run_for_csp(db, csp, TODAY)
    cycle = db.query(OutreachCycle).filter_by(csp_id=csp.id, kind="UPLOAD").first()
    [late] = outbound.draft(db, csp=csp, role="CSP", template_key="ONBOARD_ALL",   # old calendar's day-6 draft
                            ctx={"csp_name": csp.name, "csp_code": csp.current_code, "docs": []},
                            key_base=f"C{cycle.id}:U-D6", cycle_id=cycle.id, stage="U-D6", channels=("WHATSAPP",))
    late.status, late.sent_at = OutboundStatus.SENT, datetime(2026, 9, 26, 10)          # the team sent that one
    renewal_engine.run_for_csp(db, csp, TODAY + timedelta(days=2))
    old = [m for m in _msgs(db, csp) if m.stage == "U-1"]
    assert old and all(m.status == OutboundStatus.REJECTED for m in old)               # no second onboarding
    renewal_engine.run_for_csp(db, csp, date(2026, 9, 30))                            # 4 days after the send
    assert {m.stage for m in _msgs(db, csp) if m.status == OutboundStatus.QUEUED_FOR_REVIEW} == {"U-2"}


def test_old_piled_up_drafts_are_reduced_to_one(db_session):
    db = db_session
    csp = _csp(db)
    renewal_engine.run_for_csp(db, csp, TODAY)
    cycle = db.query(OutreachCycle).filter_by(csp_id=csp.id, kind="UPLOAD").first()
    for stage in ("U-D3", "U-D6"):                                    # what the old calendar drafted
        outbound.draft(db, csp=csp, role="CSP", template_key="ONBOARD_ALL",
                       ctx={"csp_name": csp.name, "csp_code": csp.current_code, "docs": []},
                       key_base=f"C{cycle.id}:{stage}", cycle_id=cycle.id, stage=stage)
    renewal_engine.run_for_csp(db, csp, TODAY + timedelta(days=1))
    waiting = {m.stage for m in _msgs(db, csp) if m.status == OutboundStatus.QUEUED_FOR_REVIEW}
    assert waiting == {"U-1"}
    retired = [m for m in _msgs(db, csp) if m.stage in ("U-D3", "U-D6")]
    assert all(m.status == OutboundStatus.REJECTED and m.error_log.startswith("Superseded") for m in retired)
    for m in _msgs(db, csp):                                          # the team sends U-1 on day 2
        if m.stage == "U-1":
            m.status, m.sent_at = OutboundStatus.SENT, datetime(2026, 9, 27, 10)
    renewal_engine.run_for_csp(db, csp, date(2026, 10, 1))
    assert [m.stage for m in _msgs(db, csp) if m.status == OutboundStatus.QUEUED_FOR_REVIEW] == ["U-2", "U-2"]


def test_upload_cycle_closes_when_documents_arrive(db_session):
    db = db_session
    csp = _csp(db)
    _doc(db, csp, "AGREEMENT", date(2026, 1, 1), date(2029, 1, 1))
    _run_days(db, csp, TODAY, 4)
    _doc(db, csp, "POLICE_VERIFICATION", TODAY, TODAY + timedelta(days=365), months=12)
    _doc(db, csp, "IIBF_CERTIFICATE", date(2022, 1, 1), None)
    renewal_engine.run_for_csp(db, csp, TODAY + timedelta(days=5))
    cycle = db.query(OutreachCycle).filter_by(csp_id=csp.id, kind="UPLOAD").first()
    assert cycle.closed_at is not None and cycle.close_reason == "ALL_DOCUMENTS_RECEIVED"


def test_upload_closes_cycle_at_once_and_cancels_unsent_reminders(db_session):
    db = db_session
    csp = _csp(db)
    _doc(db, csp, "AGREEMENT", date(2026, 1, 1), date(2029, 1, 1))
    renewal_engine.run_for_csp(db, csp, TODAY)                       # U-1 drafted, waiting for review
    queued = [m for m in _msgs(db, csp) if m.status == OutboundStatus.QUEUED_FOR_REVIEW]
    assert queued
    _doc(db, csp, "POLICE_VERIFICATION", TODAY, TODAY + timedelta(days=365), months=12)
    _doc(db, csp, "IIBF_CERTIFICATE", date(2022, 1, 1), None)
    renewal_engine.on_documents_received(db, csp, TODAY)             # what the portal/Gmail call
    cycle = db.query(OutreachCycle).filter_by(csp_id=csp.id, kind="UPLOAD").first()
    assert cycle.close_reason == "ALL_DOCUMENTS_RECEIVED"
    assert all(m.status == OutboundStatus.REJECTED and "Cancelled before sending" in m.error_log for m in queued)


def test_category_4_gets_onboarding_message(db_session):
    db = db_session
    csp = _csp(db)
    renewal_engine.run_for_csp(db, csp, TODAY)
    assert {m.template_name for m in _msgs(db, csp)} == {"ONBOARD_ALL"}


# --------------------------------------------------------- recipient guard
def test_recipient_guard(db_session):
    db = db_session
    csp = _csp(db)
    assert outbound.recipient_allowed(db, csp, "CSP", "WHATSAPP", "+91 98765 43210")
    assert not outbound.recipient_allowed(db, csp, "CSP", "WHATSAPP", "9999999999")
    assert not outbound.recipient_allowed(db, csp, "RM", "EMAIL", "someone@evil.com")


def test_send_blocks_destination_not_on_sheet(db_session):
    db = db_session
    csp = _csp(db)
    [msg] = outbound.draft(db, csp=csp, role="CSP", template_key="ONBOARD_ALL",
                           ctx={"csp_name": csp.name, "csp_code": csp.current_code, "docs": []},
                           key_base=f"t{csp.id}", channels=("WHATSAPP",))
    msg.destination = "9111111111"     # tampered
    msg.status = OutboundStatus.APPROVED
    outbound.send(db, msg)
    assert msg.status == OutboundStatus.BLOCKED


def test_whatsapp_stub_marks_ready_not_sent(db_session):
    db = db_session
    csp = _csp(db)
    [msg] = outbound.draft(db, csp=csp, role="CSP", template_key="ONBOARD_ALL",
                           ctx={"csp_name": csp.name, "csp_code": csp.current_code, "docs": []},
                           key_base=f"s{csp.id}", channels=("WHATSAPP",))
    outbound.approve(db, msg, "tester")
    outbound.send(db, msg)
    assert msg.status == OutboundStatus.READY_NOT_SENT


def test_missing_rm_contact_is_blocked_not_sent_elsewhere(db_session):
    db = db_session
    csp = _csp(db, rm_contact=False)
    msgs = outbound.draft(db, csp=csp, role="RM", template_key="ESCALATION_RM",
                          ctx={"csp_name": csp.name, "csp_code": csp.current_code, "docs": []},
                          key_base=f"r{csp.id}")
    assert all(m.status == OutboundStatus.BLOCKED for m in msgs)


def test_edit_cannot_change_recipient(db_session):
    db = db_session
    csp = _csp(db)
    [msg] = outbound.draft(db, csp=csp, role="CSP", template_key="ONBOARD_ALL",
                           ctx={"csp_name": csp.name, "csp_code": csp.current_code, "docs": []},
                           key_base=f"e{csp.id}", channels=("EMAIL",))
    dest = msg.destination
    outbound.edit_text(msg, "new subject", "new body")
    assert msg.destination == dest and msg.payload_json["body"] == "new body"


# ---------------------------------------------------------------- templates
def test_templates_are_hindi_first_then_english():
    ctx = {"csp_name": "Kaushar Jahan", "csp_code": "1A850004", "upload_link": "https://x/u",
           "docs": [{"label_en": "IIBF Certificate", "label_hi": "आईआईबीएफ सर्टिफिकेट", "status": "MISSING"}],
           "rm_name": "Vandana", "rm_phone": "9000000001"}
    for channel in ("WHATSAPP", "EMAIL"):
        body = render("UPLOAD_MISSING", channel, ctx)["body"]
        hi, en = body.index("नमस्ते"), body.index("Hello")
        assert hi < en and "https://x/u" in body
    assert "Google Drive" in render("UPLOAD_MISSING", "EMAIL", ctx)["body"]


def test_sub_slabs_say_exactly_what_is_on_file(db_session):
    from app.compliance import sub_slab
    db = db_session
    full = _csp(db); _all_valid(db, full, TODAY + timedelta(days=400))
    soon = _csp(db); _all_valid(db, soon, TODAY + timedelta(days=400), pvr_expiry=TODAY + timedelta(days=20))
    overdue = _csp(db); _all_valid(db, overdue, TODAY - timedelta(days=5))
    no_iibf = _csp(db); _doc(db, no_iibf, "AGREEMENT", date(2026, 1, 1), date(2029, 1, 1))
    _doc(db, no_iibf, "POLICE_VERIFICATION", TODAY - timedelta(days=30), TODAY + timedelta(days=335), months=12)
    only_pvr = _csp(db); _doc(db, only_pvr, "POLICE_VERIFICATION", TODAY - timedelta(days=30), TODAY + timedelta(days=335), months=12)
    blurry = _csp(db); _doc(db, blurry, "AGREEMENT", None, None, status=DocumentStatus.UNREADABLE, readability="UNREADABLE")
    nothing = _csp(db)
    got = [sub_slab(evaluate(db, c, TODAY)) for c in (full, soon, overdue, no_iibf, only_pvr, blurry, nothing)]
    assert got == ["1.1", "1.2", "1.3", "2.3", "3.2", "4.2", "4.1"]


def test_retired_drafts_are_written_again_fresh(db_session):
    db = db_session
    csp = _csp(db)
    _all_valid(db, csp, TODAY + timedelta(days=60))                  # agreement renewal T-60 due
    renewal_engine.run_for_csp(db, csp, TODAY)
    first = [m for m in _msgs(db, csp) if m.document_type == "AGREEMENT"]
    for m in first:                                                   # what scripts/redraft_fresh does
        m.status, m.error_log = OutboundStatus.REJECTED, "Superseded: redrafted fresh"
    renewal_engine.run_for_csp(db, csp, TODAY)
    fresh = [m for m in _msgs(db, csp) if m.document_type == "AGREEMENT" and m.status == OutboundStatus.QUEUED_FOR_REVIEW]
    assert {m.stage for m in fresh} == {"T-60"} and len(fresh) == 2
    assert {m.idempotency_key for m in fresh}.isdisjoint({m.idempotency_key for m in first})


def test_csp_without_email_gets_a_fresh_whatsapp_draft_after_redraft(db_session):
    db = db_session
    csp = _csp(db, email=False)                                        # email row is BLOCKED
    renewal_engine.run_for_csp(db, csp, TODAY)
    for m in _msgs(db, csp):
        if m.status == OutboundStatus.QUEUED_FOR_REVIEW:              # what scripts/redraft_fresh does
            m.status, m.error_log = OutboundStatus.REJECTED, "Superseded: redrafted fresh"
    renewal_engine.run_for_csp(db, csp, TODAY + timedelta(days=1))
    fresh = [m for m in _msgs(db, csp) if m.status == OutboundStatus.QUEUED_FOR_REVIEW]
    assert [m.channel for m in fresh] == ["WHATSAPP"]
    for _ in range(3):                                                  # no new blocked rows every day
        renewal_engine.run_for_csp(db, csp, TODAY + timedelta(days=2))
    assert len(_msgs(db, csp)) == 3


def test_csp_with_no_contact_is_not_drafted_again_every_day(db_session):
    db = db_session
    csp = _csp(db, email=False)
    csp.phone = csp.whatsapp_number = None
    _run_days(db, csp, TODAY, 20)
    assert len(_msgs(db, csp)) == 2 and all(m.status == OutboundStatus.BLOCKED for m in _msgs(db, csp))
