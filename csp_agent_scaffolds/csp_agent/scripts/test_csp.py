"""
End-to-end test with a TEST CSP that belongs to you: its phone and email are
yours, so the WhatsApp and the email reach you, you open the link, upload
sample documents and see what the agent accepts or rejects.

The test CSP (code 9T999999) is kept OFF the calling-sheet list, so it is in
no count, slab, report or daily reminder, and the hourly sheet reload never
touches it. Everything else is real: the link, the form, the checks, the
folder in storage/documents/, the "Form uploads" page.

    python -m scripts.test_csp setup --phone 91XXXXXXXXXX --email you@eko.co.in
    python -m scripts.test_csp samples                  # sample documents -> data/test_docs/
    python -m scripts.test_csp send --whatsapp --email  # the onboarding message, to you
    python -m scripts.test_csp status                   # slab, documents, what the form decided
    python -m scripts.test_csp reset --yes              # remove the test uploads, start again

WhatsApp counts towards WHATSAPP_DAILY_LIMIT like any approval.
"""
import argparse
import secrets
import shutil
import sys
from datetime import date, timedelta
from pathlib import Path

from app import vault
from app.comms import outbound
from app.compliance import DOC_LABELS, refresh_category
from app.db import SessionLocal
from app.models import (CSP, AgreementEvent, ContactChangeRequest, CspQuestion, Document, ExtractionCorrection,
                        ManualReviewQueue, OutboundMessage, OutboundStatus, OutreachCycle, PortalToken)
from app.portal_tokens import issue_upload_link

CODE = "9T999999"
NAME = "TEST CSP (not real)"
ALL = ["AGREEMENT", "POLICE_VERIFICATION", "IIBF_CERTIFICATE"]


def _get(db, must=True):
    c = db.query(CSP).filter(CSP.current_code == CODE).first()
    if c is None and must:
        sys.exit("No test CSP yet. Run: python -m scripts.test_csp setup --phone 91XXXXXXXXXX --email you@...")
    return c


def setup(phone: str, email: str) -> None:
    p = outbound._phone10(phone)
    if not p:
        sys.exit(f"Not a valid mobile number: {phone!r}")
    db = SessionLocal()
    try:
        c = _get(db, must=False)
        if c is None:
            c = CSP(current_code=CODE, lookup_code=CODE)
            db.add(c)
        # Off the calling-sheet list on purpose: no counts, slabs, reports or reminders.
        c.name, c.phone, c.whatsapp_number, c.email = NAME, p, p, (email or "").strip().lower() or None
        c.is_active_in_calling_sheet, c.state = False, "TEST"
        db.flush()
        refresh_category(db, c)
        db.commit()
        print(f"Test CSP {CODE} ready: WhatsApp ...{p[-4:]}, email {c.email or '(none)'}, slab {c.category}.")
    finally:
        db.close()


def send(whatsapp: bool, email: bool) -> None:
    channels = tuple(ch for ch, on in (("WHATSAPP", whatsapp), ("EMAIL", email)) if on)
    if not channels:
        sys.exit("Choose --whatsapp and/or --email.")
    db = SessionLocal()
    try:
        c = _get(db)
        state = refresh_category(db, c)
        link = issue_upload_link(db, c, state.needs_upload or ALL)
        docs = [{"label_en": DOC_LABELS[t][0], "label_hi": DOC_LABELS[t][1], "status": state.docs[t].status} for t in ALL]
        ctx = {"csp_name": c.name, "csp_code": c.current_code, "docs": docs, "upload_link": link}
        stamp = f"{date.today().isoformat()}-{secrets.token_hex(3)}"
        msgs = outbound.draft(db, csp=c, role="CSP", template_key="ONBOARD_ALL", ctx=ctx,
                              key_base=f"TEST:{stamp}", channels=channels, stage="TEST")
        for m in msgs:
            if m.status == OutboundStatus.BLOCKED:
                print(f"{m.channel}: blocked: {m.error_log}")
                continue
            try:
                outbound.approve(db, m, "test_csp script")
            except ValueError as e:
                print(f"{m.channel}: not sent: {e}")
                continue
            outbound.send(db, m)
            db.commit()
            print(f"{m.channel}: {m.status.value}" + (f" ({m.error_log})" if m.error_log else ""))
        db.commit()
        print(f"\nThe link (also inside the message):\n{link}")
    finally:
        db.close()


def status() -> None:
    db = SessionLocal()
    try:
        c = _get(db)
        st = refresh_category(db, c)
        db.commit()
        print(f"{CODE} {c.name}: slab {st.category} ({c.sub_slab}) - {st.reason}")
        print("\nDocuments on record:")
        for d in db.query(Document).filter_by(csp_id=c.id).order_by(Document.uploaded_at):
            print(f"  {d.document_type:20} {d.status.value:13} {d.readability or '':10} issued {d.issue_date} "
                  f"expires {d.expiry_date} current={d.is_current} via {d.upload_channel}")
        print("\nUpload-page submissions (newest first):")
        for e in (db.query(AgreementEvent).filter_by(csp_id=c.id, event_type="PORTAL_UPLOAD")
                  .order_by(AgreementEvent.sent_at.desc()).limit(10)):
            p = e.payload or {}
            parts = [f"{r.get('section')}: {'OK' if r.get('ok') else 'NO'} {r.get('reason', '')}"
                     + (f" (read as {r['read_as']})" if r.get("read_as") else "") for r in p.get("results", [])]
            print(f"  {e.sent_at:%d-%m %H:%M}  slab {p.get('slab_before')} -> {p.get('slab_after')}  |  " + "  |  ".join(parts))
        print("\nMessages:")
        for m in db.query(OutboundMessage).filter_by(csp_id=c.id).order_by(OutboundMessage.id.desc()).limit(10):
            print(f"  {m.channel:9} {m.template_name:15} {m.status.value:18} sent {m.sent_at}  {m.error_log or ''}")
    finally:
        db.close()


def reset() -> None:
    """Remove the test CSP's documents, files, submissions and questions."""
    db = SessionLocal()
    try:
        c = _get(db)
        if c.current_code != CODE or c.is_active_in_calling_sheet:
            sys.exit("Refusing: this is not the test CSP.")
        docs = db.query(Document).filter_by(csp_id=c.id).all()
        ids = [d.id for d in docs]
        db.query(ManualReviewQueue).filter(ManualReviewQueue.document_id.in_(ids or [-1])).delete(synchronize_session=False)
        db.query(ExtractionCorrection).filter(ExtractionCorrection.document_id.in_(ids or [-1])).delete(synchronize_session=False)
        db.query(OutreachCycle).filter(OutreachCycle.document_id.in_(ids or [-1])).update({"document_id": None},
                                                                                         synchronize_session=False)
        files = 0
        for d in docs:
            p = vault.abs_path(d.storage_path)
            if p is not None and p.exists() and vault.is_inside_vault(p.resolve()):
                p.unlink()
                files += 1
            db.delete(d)
        db.query(AgreementEvent).filter(AgreementEvent.csp_id == c.id,
                                        AgreementEvent.event_type.in_(["PORTAL_UPLOAD", "LINK_REQUEST"])).delete(synchronize_session=False)
        db.query(CspQuestion).filter_by(csp_id=c.id).delete(synchronize_session=False)
        db.query(ContactChangeRequest).filter_by(csp_id=c.id).delete(synchronize_session=False)
        db.query(PortalToken).filter(PortalToken.csp_id == c.id, PortalToken.revoked_at.is_(None)).update(
            {"revoked_at": outbound._now(), "revoke_reason": "TEST_RESET"}, synchronize_session=False)
        rejected = vault.ROOT / vault.PORTAL_REJECTED / CODE
        if rejected.is_dir():
            shutil.rmtree(rejected)
        db.flush()
        refresh_category(db, c)
        db.commit()
        print(f"Reset: {len(docs)} documents and {files} files removed; links closed. Slab now {c.category}.")
    finally:
        db.close()


def samples(out: Path) -> None:
    """Sample documents the agent reads like real ones (a text layer, no OCR)."""
    import fitz
    out.mkdir(parents=True, exist_ok=True)
    t = date.today()
    agr, pvr, iibf, old = t - timedelta(days=10), t - timedelta(days=30), date(2022, 7, 8), t - timedelta(days=400)
    d = lambda x: x.strftime("%d-%m-%Y")

    def pdf(name, text):
        doc = fitz.open()
        page = doc.new_page()
        for i, line in enumerate(text.strip().split("\n")):
            page.insert_text((40, 60 + 16 * i), line, fontsize=10)
        doc.save(out / name)

    pdf("1_agreement_valid.pdf", f"""
INDIA NON JUDICIAL  Government of Uttar Pradesh  e-Stamp
Certificate No. : IN-UP00000000000000X
Certificate Issued Date : {agr.strftime('%d-%b-%Y')} 11:29 AM
Stamp Duty Amount(Rs.) : 100 (One Hundred only)
CUSTOMER SERVICE POINT AGREEMENT
(This Agreement shall remain valid for a period of three (3) years from the Effective Date.)
On this day of, {agr.strftime('%d/%m/%Y')} ("Effective Date"), CSP Name {NAME} CSP Code {CODE}
Eko hereby appoints the CSP on the terms of their relationship set out below.
IN WITNESS WHEREOF the parties have signed this agreement.""")
    pvr_text = """
Government of Uttar Pradesh  CHARACTER CERTIFICATE
Application No. - 202500000001 Date - {d}
This is to certify that Mr. TEST PERSON ... no adverse entry was found against the said
candidate in the police records. This certificate is valid only for one year.
This is a computer generated document so no signature is required.
Digitally signed by TEST OFFICER Date: {s} 18:29:17 +05'30'"""
    pdf("2_pvr_valid.pdf", pvr_text.format(d=d(pvr), s=pvr.strftime("%Y.%m.%d")))
    pdf("3_iibf_valid.pdf", f"""
INDIAN INSTITUTE OF BANKING & FINANCE Membership No./ 999001 do hereby certify that TEST PERSON
has passed the CERTIFICATE EXAMINATION FOR BUSINESS CORRESPONDENTS / FACILITATORS of the Institute.
MUMBAI, DATED 08th JUL 2022
Digitally signed by DS INDIAN INSTITUTE OF BANKING AND FINANCE 3 Date: 2022.07.29 10:47:17 IST""")
    pdf("4_pvr_expired.pdf", pvr_text.format(d=d(old), s=old.strftime("%Y.%m.%d")))
    pdf("5_pvr_challan_receipt.pdf", f"""
UP POLICE  Character Certificate Challan Receipt
Payment Receipt  Bank Transaction ID 000000  Head of Account 0055
Application Status : Submitted   Date of Submission : {d(pvr)}
Amount Rs. 50""")
    try:
        import cv2
        import numpy as np
        img = np.full((1400, 1000), 255, np.uint8)
        cv2.putText(img, "CHARACTER CERTIFICATE " + d(pvr), (40, 400), cv2.FONT_HERSHEY_SIMPLEX, 1.4, 0, 3)
        cv2.imwrite(str(out / "6_blurry_photo.jpg"), cv2.GaussianBlur(img, (61, 61), 30))
    except ImportError:
        pass
    print(f"Samples in {out}/ - what to upload and what should happen:\n")
    rows = [
        ("1_agreement_valid.pdf", "Agreement", d(agr), "accepted, valid 3 years"),
        ("2_pvr_valid.pdf", "PVR", d(pvr), "accepted, valid 1 year"),
        ("3_iibf_valid.pdf", "IIBF", "08-07-2022", "accepted, lifetime"),
        ("3_iibf_valid.pdf", "Agreement", "08-07-2022", "rejected: wrong document (it's an IIBF)"),
        ("4_pvr_expired.pdf", "PVR", d(old), "rejected: expired"),
        ("5_pvr_challan_receipt.pdf", "PVR", d(pvr), "rejected: a receipt, not the certificate"),
        ("6_blurry_photo.jpg", "PVR", d(pvr), "stopped on the phone: text not visible in the photo"),
        ("2_pvr_valid.pdf (again)", "PVR", d(pvr), "already on file"),
        ("2_pvr_valid.pdf", "PVR", "a different date", "accepted with the date printed on it"),
    ]
    w = max(len(r[0]) for r in rows)
    print(f"  {'file':{w}}  {'box':9}  {'date to type':16}  expected")
    for f, box, dt, exp in rows:
        print(f"  {f:{w}}  {box:9}  {dt:16}  {exp}")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("setup")
    s.add_argument("--phone", required=True)
    s.add_argument("--email", default="")
    s = sub.add_parser("send")
    s.add_argument("--whatsapp", action="store_true")
    s.add_argument("--email", action="store_true")
    sub.add_parser("status")
    s = sub.add_parser("reset")
    s.add_argument("--yes", action="store_true")
    s = sub.add_parser("samples")
    s.add_argument("--dir", default="data/test_docs")
    a = ap.parse_args()
    if a.cmd == "setup":
        setup(a.phone, a.email)
    elif a.cmd == "send":
        send(a.whatsapp, a.email)
    elif a.cmd == "status":
        status()
    elif a.cmd == "reset":
        if not a.yes:
            sys.exit("This removes the test CSP's uploads. Run again with --yes.")
        reset()
    else:
        samples(Path(a.dir))


if __name__ == "__main__":
    main()
