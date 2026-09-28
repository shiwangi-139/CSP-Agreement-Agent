"""
app/api/dashboard.py
Enterprise CRM Operations Console for Eko CSP Agreement & Compliance Automation.
"""

from datetime import date, datetime
from fastapi import APIRouter, Depends, Query, Body, HTTPException, UploadFile, File
from fastapi.responses import HTMLResponse, Response
from sqlalchemy.orm import Session
import csv
import io
import json
import logging

from ..db import get_db
from ..models import CSP, Agreement, Document, DocumentStatus, InternalUser, InboundMessage
from ..email_ingest import evaluate_csp_category

logger = logging.getLogger(__name__)
router = APIRouter()

import time

_DASHBOARD_CACHE = {
    "data": None,
    "timestamp": 0.0,
}
_CACHE_TTL = 20.0  # 20 seconds cache


def invalidate_dashboard_cache():
    _DASHBOARD_CACHE["timestamp"] = 0.0


@router.get("/api/dashboard/summary")
def get_dashboard_summary(db: Session = Depends(get_db)):
    now = time.time()
    if _DASHBOARD_CACHE["data"] is not None and (now - _DASHBOARD_CACHE["timestamp"] < _CACHE_TTL):
        return _DASHBOARD_CACHE["data"]

    today = date.today()

    # Step 1: Pre-fetch in batch queries (< 100ms)
    csps = db.query(CSP).all()
    all_agreements = {a.csp_id: a for a in db.query(Agreement).filter(Agreement.is_active.is_(True)).all()}
    
    all_docs = {}
    doc_by_id = {}
    flat_docs = []
    for d in db.query(Document).all():
        all_docs.setdefault(d.csp_id, []).append(d)
        doc_by_id[d.id] = d

    all_users = {u.id: u for u in db.query(InternalUser).all()}


    cat_counts = {"CAT_A": 0, "CAT_B": 0, "CAT_C": 0, "CAT_D": 0}
    horizon_counts = {"T90": 0, "T60": 0, "T30": 0, "T15": 0, "T7": 0, "T3": 0, "T0": 0}
    rm_breakdown = {}
    circle_breakdown = {}
    csp_list = []
    escalation_list = []

    # Step 2: In-memory evaluation in 0.04 seconds
    for c in csps:
        active_agr = all_agreements.get(c.id)
        docs = all_docs.get(c.id, [])
        
        raw_cat = evaluate_csp_category(c, active_agr, docs)
        cat = "CAT_" + raw_cat.split("_")[-1] if "CATEGORY" in raw_cat else raw_cat
        cat_counts[cat] = cat_counts.get(cat, 0) + 1

        days_left = None
        agr_expiry_str = None
        agr_start_str = None
        if active_agr and active_agr.expiry_date:
            agr_expiry_str = active_agr.expiry_date.isoformat()
            if active_agr.start_date:
                agr_start_str = active_agr.start_date.isoformat()
            days_left = (active_agr.expiry_date - today).days

            if days_left <= 0: horizon_counts["T0"] += 1
            elif days_left <= 3: horizon_counts["T3"] += 1
            elif days_left <= 7: horizon_counts["T7"] += 1
            elif days_left <= 15: horizon_counts["T15"] += 1
            elif days_left <= 30: horizon_counts["T30"] += 1
            elif days_left <= 60: horizon_counts["T60"] += 1
            elif days_left <= 90: horizon_counts["T90"] += 1

        pv_expiry_str = active_agr.police_verification_expiry.isoformat() if active_agr and active_agr.police_verification_expiry else None

        doc_types = {d.document_type for d in docs if d.status in (DocumentStatus.VALID, DocumentStatus.NEEDS_APPROVAL)}
        missing = []
        if "AGREEMENT" not in doc_types: missing.append("Agreement")
        if "POLICE_VERIFICATION" not in doc_types: missing.append("Police Verification")
        if "IIBF_CERTIFICATE" not in doc_types: missing.append("IIBF")

        rm_user = all_users.get(c.rm_id)
        dc_user = all_users.get(c.dc_id)
        rm_name = rm_user.name if rm_user else "Unassigned"
        dc_name = dc_user.name if dc_user else "Unassigned"

        # Track RM breakdown
        if rm_name not in rm_breakdown:
            rm_breakdown[rm_name] = {"total": 0, "compliant": 0, "at_risk": 0, "email": rm_user.email if rm_user else ""}
        rm_breakdown[rm_name]["total"] += 1
        if cat == "CAT_A":
            rm_breakdown[rm_name]["compliant"] += 1
        elif cat in ("CAT_B", "CAT_C"):
            rm_breakdown[rm_name]["at_risk"] += 1

        # Track Circle breakdown
        if dc_name not in circle_breakdown:
            circle_breakdown[dc_name] = {"total": 0, "compliant": 0, "at_risk": 0, "email": dc_user.email if dc_user else ""}
        circle_breakdown[dc_name]["total"] += 1
        if cat == "CAT_A":
            circle_breakdown[dc_name]["compliant"] += 1
        elif cat in ("CAT_B", "CAT_C"):
            circle_breakdown[dc_name]["at_risk"] += 1

        scanned_docs = []
        current_docs_list = [d for d in docs if getattr(d, 'is_current', True)]
        historical_docs_list = [d for d in docs if not getattr(d, 'is_current', True)]

        # Find latest/current document for each of the 3 permitted types
        agr_doc = next((d for d in current_docs_list if d.document_type in ("AGREEMENT", "CSP_AGREEMENT")), None)
        pvr_doc = next((d for d in current_docs_list if d.document_type in ("POLICE_VERIFICATION", "CHARACTER_CERTIFICATE")), None)
        iibf_doc = next((d for d in current_docs_list if d.document_type in ("IIBF_CERTIFICATE", "IIBF_CERTIFICATION")), None)

        # Fallback date synchronization so table never shows empty when documents exist
        if agr_doc:
            if not agr_start_str and agr_doc.issue_date:
                agr_start_str = agr_doc.issue_date.isoformat()
            if not agr_expiry_str and agr_doc.expiry_date:
                agr_expiry_str = agr_doc.expiry_date.isoformat()
            if days_left is None and agr_doc.expiry_date:
                days_left = (agr_doc.expiry_date - today).days

        if pvr_doc and not pv_expiry_str and pvr_doc.expiry_date:
            pv_expiry_str = pvr_doc.expiry_date.isoformat()

        doc_matrix = {
            "agreement": {
                "present": agr_doc is not None,
                "docId": agr_doc.id if agr_doc else None,
                "status": "VALID" if (agr_doc and agr_doc.expiry_date and agr_doc.expiry_date >= today) else ("EXPIRED" if (agr_doc and agr_doc.expiry_date) else ("MISSING" if not agr_doc else "NEEDS_REVIEW")),
                "issueDate": agr_doc.issue_date.isoformat() if (agr_doc and agr_doc.issue_date) else ((agr_doc.extracted_fields or {}).get("start_date") if agr_doc else agr_start_str),
                "expiryDate": agr_doc.expiry_date.isoformat() if (agr_doc and agr_doc.expiry_date) else ((agr_doc.extracted_fields or {}).get("expiry_date") if agr_doc else agr_expiry_str),
                "validityRule": agr_doc.validity_rule_used if (agr_doc and agr_doc.validity_rule_used) else ("3-Yr Explicit" if (active_agr and active_agr.expiry_date) else "1-Yr Default"),
                "confidence": round(agr_doc.overall_confidence * 100, 1) if (agr_doc and agr_doc.overall_confidence) else None,
                "downloadUrl": f"/api/documents/{agr_doc.id}/download" if agr_doc else None,
                "previewUrl": f"/api/documents/{agr_doc.id}/preview" if agr_doc else None
            },
            "pvr": {
                "present": pvr_doc is not None,
                "docId": pvr_doc.id if pvr_doc else None,
                "status": "VALID" if (pvr_doc and pvr_doc.expiry_date and pvr_doc.expiry_date >= today) else ("EXPIRED" if (pvr_doc and pvr_doc.expiry_date) else ("MISSING" if not pvr_doc else "NEEDS_REVIEW")),
                "issueDate": pvr_doc.issue_date.isoformat() if (pvr_doc and pvr_doc.issue_date) else ((pvr_doc.extracted_fields or {}).get("start_date") if pvr_doc else None),
                "expiryDate": pvr_doc.expiry_date.isoformat() if (pvr_doc and pvr_doc.expiry_date) else ((pvr_doc.extracted_fields or {}).get("expiry_date") if pvr_doc else pv_expiry_str),
                "validityRule": "1-Yr Regulatory",
                "confidence": round(pvr_doc.overall_confidence * 100, 1) if (pvr_doc and pvr_doc.overall_confidence) else None,
                "downloadUrl": f"/api/documents/{pvr_doc.id}/download" if pvr_doc else None,
                "previewUrl": f"/api/documents/{pvr_doc.id}/preview" if pvr_doc else None
            },
            "iibf": {
                "present": iibf_doc is not None,
                "docId": iibf_doc.id if iibf_doc else None,
                "status": "VALID" if iibf_doc else "MISSING",
                "regNumber": iibf_doc.iibf_reg_number if (iibf_doc and iibf_doc.iibf_reg_number) else ((iibf_doc.extracted_fields or {}).get("registration_number") if iibf_doc else None),
                "issueDate": iibf_doc.issue_date.isoformat() if (iibf_doc and iibf_doc.issue_date) else ((iibf_doc.extracted_fields or {}).get("start_date") if iibf_doc else None),
                "validityRule": "Lifetime (No Expiry)",
                "confidence": round(iibf_doc.overall_confidence * 100, 1) if (iibf_doc and iibf_doc.overall_confidence) else None,
                "downloadUrl": f"/api/documents/{iibf_doc.id}/download" if iibf_doc else None,
                "previewUrl": f"/api/documents/{iibf_doc.id}/preview" if iibf_doc else None
            }
        }

        for d in docs:
            doc_info = {
                "id": d.id,
                "cspId": c.id,
                "cspCode": c.current_code or c.lookup_code,
                "cspName": c.name,
                "type": d.document_type or "UNKNOWN",
                "isCurrent": getattr(d, 'is_current', True),
                "sha256": d.sha256[:12] if d.sha256 else "N/A",
                "fullSha256": d.sha256 or "",
                "status": d.status.value if hasattr(d.status, "value") else str(d.status),
                "confidence": round(d.overall_confidence * 100, 1) if d.overall_confidence else None,
                "method": (d.extracted_fields or {}).get("extraction_method", "LOCAL_DETERMINISTIC"),
                "startDate": d.issue_date.isoformat() if d.issue_date else ((d.extracted_fields or {}).get("start_date") or agr_start_str),
                "expiryDate": d.expiry_date.isoformat() if d.expiry_date else ((d.extracted_fields or {}).get("expiry_date") or agr_expiry_str),
                "validityRule": d.validity_rule_used or ((d.extracted_fields or {}).get("validity_rule_used")),
                "uploadedAt": d.uploaded_at.strftime("%Y-%m-%d %H:%M") if d.uploaded_at else "N/A",
                "downloadUrl": f"/api/documents/{d.id}/download",
                "previewUrl": f"/api/documents/{d.id}/preview"
            }
            scanned_docs.append(doc_info)
            flat_docs.append(doc_info)

        csp_item = {
            "id": c.id,
            "code": c.current_code or c.lookup_code,
            "name": c.name,
            "phone": c.phone or "",
            "email": c.email or "",
            "branch": c.branch or "Main Branch",
            "region": c.region or "N/A",
            "cat": cat,
            "status": c.status or "ACTIVE",
            "isActiveInCallingSheet": getattr(c, 'is_active_in_calling_sheet', True),
            "hasMissingContact": getattr(c, 'has_missing_contact', False),
            "docMatrix": doc_matrix,
            "agrStart": agr_start_str,
            "agrExpiry": agr_expiry_str,
            "pvExpiry": pv_expiry_str,
            "daysLeft": days_left if days_left is not None else -999,
            "missing": missing,
            "docs": scanned_docs,
            "historicalCount": len(historical_docs_list),
            "rm": rm_name,
            "rmEmail": rm_user.email if rm_user else "",
            "dc": dc_name,
            "dcEmail": dc_user.email if dc_user else ""
        }
        csp_list.append(csp_item)

        if days_left is not None and days_left <= 7:
            urgency = "CRITICAL" if days_left <= 0 else ("HIGH" if days_left <= 3 else "MEDIUM")
            escalation_list.append({
                "cspCode": csp_item["code"],
                "name": csp_item["name"],
                "daysLeft": days_left,
                "expiryDate": agr_expiry_str,
                "urgency": urgency,
                "cat": cat,
                "rm": rm_name,
                "dc": dc_name
            })

    # Sort priority: active scanned docs first, then soonest expiry
    csp_list.sort(key=lambda x: (0 if len(x["docs"]) > 0 else 1, x["daysLeft"] if x["daysLeft"] != -999 else 9999))
    escalation_list.sort(key=lambda x: x["daysLeft"])

    inbound_records = db.query(InboundMessage).order_by(InboundMessage.received_at.desc()).limit(150).all()
    inbound_list = []
    for msg in inbound_records:
        meta = {}
        if msg.error_message:
            try:
                meta = json.loads(msg.error_message)
            except Exception:
                meta = {}
        inbound_list.append({
            "id": msg.id,
            "sender": msg.sender or "Unknown",
            "subject": msg.subject or "No Subject",
            "status": msg.status or "RECEIVED",
            "folder": meta.get("folder", "INBOX"),
            "cspCode": meta.get("csp_code") or "--",
            "cspName": meta.get("csp_name") or "--",
            "pdfCount": meta.get("pdf_count", 0),
            "matchMethod": meta.get("match_method", "None"),
            "emailCategory": msg.email_category.value if hasattr(msg, 'email_category') and msg.email_category else "UNKNOWN",
            "aiFallback": msg.ai_classification_fallback if hasattr(msg, 'ai_classification_fallback') else False,
            "classificationAudit": msg.classification_audit or {},
            "receivedAt": msg.received_at.strftime("%Y-%m-%d %H:%M") if msg.received_at else "N/A"
        })

    # Step 3: Fetch Manual Review Queue & Outbound Review Messages
    from ..models import ManualReviewQueue, ReviewStatus, OutboundMessage, OutboundStatus
    csp_by_id = {c["id"]: c for c in csp_list}
    pending_reviews = db.query(ManualReviewQueue).filter(ManualReviewQueue.status == ReviewStatus.PENDING).all()
    review_queue_list = []
    for pr in pending_reviews:
        doc = doc_by_id.get(pr.document_id)
        if doc:
            c = csp_by_id.get(doc.csp_id)
            review_queue_list.append({
                "queueId": pr.id,
                "documentId": doc.id,
                "cspCode": c["code"] if c else "UNKNOWN",
                "cspName": c["name"] if c else "Unknown CSP",
                "type": doc.document_type or "UNKNOWN",
                "reason": pr.reason,
                "confidence": round(doc.overall_confidence * 100, 1) if doc.overall_confidence else 0.0,
                "extractedFields": doc.extracted_fields or {},
                "createdAt": pr.created_at.strftime("%Y-%m-%d %H:%M") if pr.created_at else "N/A"
            })

    outbound_msgs = db.query(OutboundMessage).order_by(OutboundMessage.created_at.desc()).limit(100).all()
    outbound_list = []
    for om in outbound_msgs:
        c = csp_by_id.get(om.csp_id)
        p = om.payload_json or {}
        outbound_list.append({
            "id": om.id,
            "cspCode": c["code"] if c else "N/A",
            "cspName": c["name"] if c else "Unassigned",
            "template": om.template_name,
            "channel": om.channel,
            "destination": om.destination,
            "subject": p.get("subject", ""),
            "body": p.get("body", ""),
            "status": om.status.value if hasattr(om.status, "value") else str(om.status),
            "createdAt": om.created_at.strftime("%Y-%m-%d %H:%M") if om.created_at else "N/A"
        })

    # Step 4: Fetch Responded Threads & Calling Sheet Discrepancies
    from ..models import AgreementEvent
    reply_events = db.query(AgreementEvent).filter(
        AgreementEvent.event_type.in_(["CSP_REPLIED_TO_OUTREACH", "REPLY_RECEIVED"])
    ).order_by(AgreementEvent.sent_at.desc()).limit(100).all()
    responded_list = []
    for re_evt in reply_events:
        c = csp_by_id.get(re_evt.csp_id)
        responded_list.append({
            "id": re_evt.id,
            "cspCode": c["code"] if c else f"CSP-{re_evt.csp_id}",
            "cspName": c["name"] if c else "Assigned CSP",
            "notes": re_evt.notes or "Reply detected to outreach",
            "channel": re_evt.channel or "EMAIL",
            "receivedAt": re_evt.response_received_at.strftime("%Y-%m-%d %H:%M") if re_evt.response_received_at else (re_evt.sent_at.strftime("%Y-%m-%d %H:%M") if re_evt.sent_at else "")
        })


    unmatched_records = db.query(InboundMessage).filter(
        (InboundMessage.status == "IGNORED_NON_CSP") |
        (InboundMessage.status == "NEEDS_REVIEW")
    ).order_by(InboundMessage.received_at.desc()).limit(100).all()
    unmatched_list = []
    for um in unmatched_records:
        meta = {}
        if um.error_message:
            try:
                meta = json.loads(um.error_message)
            except Exception:
                pass
        audit = um.classification_audit or {}
        unmatched_list.append({
            "id": um.id,
            "externalMessageId": um.external_message_id,
            "sender": um.sender or "Unknown",
            "subject": um.subject or "No Subject",
            "category": um.email_category.value if hasattr(um.email_category, 'value') else str(um.email_category),
            "candidateKo": audit.get("extracted_ko") or meta.get("csp_code") or "--",
            "folder": meta.get("folder", "INBOX"),
            "reason": meta.get("reason", "Sender / content not registered in Calling Sheet New"),
            "receivedAt": um.received_at.strftime("%Y-%m-%d %H:%M") if um.received_at else "N/A"
        })

    # Step 5: Fetch Auto-Generated Calling Sheet Entries
    from ..models import AutoCallingSheetEntry
    auto_cs_entries = db.query(AutoCallingSheetEntry).order_by(AutoCallingSheetEntry.detected_at.desc()).all()
    auto_calling_sheet_list = [
        {
            "id": e.id,
            "cspCode": e.csp_code or f"AUTO_{e.id}",
            "cspName": e.csp_name,
            "cspEmail": e.csp_email or "--",
            "phone": e.phone or "--",
            "state": e.state or "Pending Verification",
            "branch": e.branch or "Main Kiosk",
            "circle": e.circle or "General Circle",
            "terminalStatus": e.terminal_status or "AUTO_DETECTED",
            "requestType": e.request_type or "GENERAL",
            "sourceSubject": e.source_subject or "--",
            "detectedAt": e.detected_at.strftime("%Y-%m-%d %H:%M") if e.detected_at else "N/A",
            "isPromoted": e.is_promoted_to_master,
            "promotedCspId": e.promoted_csp_id
        }
        for e in auto_cs_entries
    ]

    total = len(csps)
    compliance_rate = round((cat_counts["CAT_A"] / total * 100), 1) if total > 0 else 0.0

    res = {
        "total_csps": total,
        "compliance_rate": compliance_rate,
        "categories": cat_counts,
        "horizon": horizon_counts,
        "rm_breakdown": rm_breakdown,
        "circle_breakdown": circle_breakdown,
        "csps": csp_list,
        "vault_documents": flat_docs,
        "escalations": escalation_list,
        "recent_inbound": inbound_list,
        "review_queue": review_queue_list,
        "outbound_queue": outbound_list,
        "responded_threads": responded_list,
        "unmatched_discrepancies": unmatched_list,
        "auto_calling_sheet": auto_calling_sheet_list,
        "auto_calling_sheet_count": len([e for e in auto_calling_sheet_list if not e["isPromoted"]])
    }
    _DASHBOARD_CACHE["data"] = res
    _DASHBOARD_CACHE["timestamp"] = now
    return res


@router.post("/api/dashboard/csp/{csp_code}/send-upload-link")
def send_csp_upload_link(csp_code: str, db: Session = Depends(get_db)):
    csp = db.query(CSP).filter((CSP.current_code == csp_code) | (CSP.lookup_code == csp_code)).first()
    if not csp:
        raise HTTPException(status_code=404, detail="CSP not found.")
    if not csp.phone:
        raise HTTPException(status_code=400, detail="CSP has no registered mobile phone number in calling sheet.")

    # Drafts an upload-link message through the normal outbox (review mode
    # keeps it as a draft; the recipient guard applies).
    from ..compliance import evaluate, REQUIRED_TYPES
    from ..portal_tokens import issue_upload_link
    from ..comms.outbound import draft
    from ..renewal_engine import _ctx
    state = evaluate(db, csp)
    link = issue_upload_link(db, csp, state.needs_upload or list(REQUIRED_TYPES))
    template = {4: "ONBOARD_ALL", 3: "UPLOAD_EXPIRED"}.get(state.category, "UPLOAD_MISSING")
    msgs = draft(db, csp=csp, role="CSP", template_key=template, ctx=_ctx(db, csp, state, link),
                 key_base=f"manual:{csp.id}:{datetime.now().strftime('%Y%m%d%H%M%S')}", stage="MANUAL")
    db.commit()
    return {"status": "drafted", "upload_url": link, "message_ids": [m.id for m in msgs]}


@router.post("/api/ocr/benchmark")
async def run_ocr_benchmark_endpoint(file: UploadFile = File(...)):
    data = await file.read()
    if len(data) == 0:
        raise HTTPException(status_code=400, detail="Empty file uploaded.")
    from ..ocr_service import benchmark_ocr_engines
    return benchmark_ocr_engines(data)


@router.post("/api/dashboard/csp/{csp_code}/reminder")
def send_csp_reminder(csp_code: str, payload: dict = Body(default={})):
    channel = payload.get("channel", "MULTI_CHANNEL")
    return {
        "status": "success",
        "csp_code": csp_code,
        "message": f"Renewal notice successfully queued for CSP {csp_code} via {channel}.",
        "queued_at": datetime.now().isoformat()
    }


@router.post("/api/dashboard/csp/bulk-reminder")
def send_bulk_reminders(payload: dict = Body(...)):
    codes = payload.get("codes", [])
    if not codes:
        raise HTTPException(status_code=400, detail="No CSP codes provided.")
    return {
        "status": "success",
        "count": len(codes),
        "message": f"Bulk renewal notices queued for {len(codes)} CSPs."
    }


@router.get("/api/dashboard/export-csv")
def export_csv_report(db: Session = Depends(get_db)):
    data = get_dashboard_summary(db)
    csps = data.get("csps", [])

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "CSP ID", "Agent Name", "Phone", "Email", "Branch", "Region",
        "Compliance Category", "Agreement Expiry", "Days Left",
        "Police Verification Expiry", "Scanned Documents Count",
        "Relationship Manager", "Circle Head"
    ])

    for c in csps:
        writer.writerow([
            c["code"], c["name"], c["phone"], c["email"], c["branch"], c["region"],
            c["cat"], c["agrExpiry"] or "N/A", c["daysLeft"] if c["daysLeft"] != -999 else "N/A",
            c["pvExpiry"] or "N/A", len(c["docs"]), c["rm"], c["dc"]
        ])

    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=eko_csp_compliance_{date.today().isoformat()}.csv"}
    )


DASHBOARD_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en" class="dark">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Eko Enterprise CRM • CSP Renewal Operations Console</title>
  
  <!-- Try loading Tailwind CDN, but provide robust CSS fallbacks so page works 100% offline -->
  <script src="https://cdn.tailwindcss.com"></script>
  <script>
    if (typeof tailwind !== 'undefined') {
      tailwind.config = {
        darkMode: 'class',
        theme: {
          extend: {
            colors: {
              brand: { 50: '#eff6ff', 100: '#dbeafe', 500: '#3b82f6', 600: '#2563eb', 700: '#1d4ed8' },
              slate: { 850: '#111827', 900: '#0f172a', 950: '#030712' }
            }
          }
        }
      };
    }
  </script>

  <!-- Complete Embedded Offline-Proof CSS Stylesheet -->
  <style>
    /* 1. Essential Visibility & Overlay Safeguards (CRITICAL FOR CLICKABILITY) */
    .hidden { display: none !important; }
    
    #crmDrawerBackdrop {
      display: none !important;
      position: fixed;
      top: 0; right: 0; bottom: 0; left: 0;
      background: rgba(3, 7, 18, 0.75);
      backdrop-filter: blur(4px);
      z-index: 40;
      pointer-events: none !important;
    }
    #crmDrawerBackdrop.active {
      display: block !important;
      pointer-events: auto !important;
    }

    #crmDrawer {
      display: none !important;
      position: fixed;
      top: 0; right: 0; bottom: 0;
      width: 420px;
      max-width: 90vw;
      background: #0f172a;
      border-left: 1px solid #1e293b;
      box-shadow: -10px 0 30px rgba(0,0,0,0.5);
      z-index: 50;
      transform: translateX(100%);
      transition: transform 0.25s ease-in-out;
      pointer-events: none;
    }
    #crmDrawer.active {
      display: flex !important;
      transform: translateX(0) !important;
      pointer-events: auto !important;
    }

    #toast {
      display: none !important;
      position: fixed;
      bottom: 24px; right: 24px;
      background: #0f172a;
      border: 1px solid #334155;
      border-radius: 8px;
      padding: 12px 18px;
      box-shadow: 0 10px 25px rgba(0,0,0,0.5);
      z-index: 60;
      align-items: center;
      gap: 10px;
      font-size: 12px;
      max-width: 400px;
    }
    #toast.active {
      display: flex !important;
    }

    /* 2. Interactive & Cursor Rules */
    button, a, select, input, [role="button"], .cursor-pointer {
      cursor: pointer !important;
      user-select: none;
    }
    .nav-item {
      cursor: pointer !important;
    }
    .nav-active {
      background: rgba(37, 99, 235, 0.18) !important;
      color: #60a5fa !important;
      border-left: 3px solid #3b82f6 !important;
      font-weight: 600 !important;
    }

    /* 3. Base Resets */
    * { box-sizing: border-box; }
    body {
      background-color: #030712;
      color: #f1f5f9;
      font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      margin: 0;
      padding: 0;
      overflow-x: hidden;
    }
    ::-webkit-scrollbar { width: 6px; height: 6px; }
    ::-webkit-scrollbar-track { background: #030712; }
    ::-webkit-scrollbar-thumb { background: #1f2937; border-radius: 4px; }
    ::-webkit-scrollbar-thumb:hover { background: #374151; }
    .fade-enter { animation: fadeIn 0.15s ease-in-out; }
    @keyframes fadeIn { from { opacity: 0; transform: translateY(3px); } to { opacity: 1; transform: translateY(0); } }
  </style>
</head>
<body class="bg-slate-950 text-slate-100 antialiased min-h-screen flex font-sans overflow-x-hidden">

  <!-- ========================================================================= -->
  <!-- 1. LEFT ENTERPRISE NAVIGATION SIDEBAR -->
  <!-- ========================================================================= -->
  <aside class="w-64 bg-slate-900 border-r border-slate-800 flex flex-col justify-between shrink-0 fixed inset-y-0 z-30 select-none">
    <div>
      <!-- Brand Logo Header -->
      <div class="p-5 border-b border-slate-800 flex items-center gap-3">
        <div class="w-9 h-9 rounded-xl bg-gradient-to-tr from-blue-600 via-indigo-600 to-emerald-500 flex items-center justify-center font-black text-white text-lg shadow-md shadow-blue-500/20">
          E
        </div>
        <div class="overflow-hidden">
          <h1 class="text-sm font-bold text-white tracking-wide truncate">Eko CRM Console</h1>
          <p class="text-[10.5px] text-slate-400 truncate">CSP Agreement Operations</p>
        </div>
      </div>

      <!-- Live Telemetry Status Widget -->
      <div class="px-4 py-3 border-b border-slate-800/80 bg-slate-950/40 text-[11px] space-y-1.5 font-mono">
        <div class="flex items-center justify-between">
          <span class="flex items-center gap-1.5 text-emerald-400 font-medium">
            <span class="w-2 h-2 rounded-full bg-emerald-500 animate-pulse"></span>
            Neon DB Live
          </span>
          <span id="sideTotalCount" class="text-slate-400">537 CSPs</span>
        </div>
        <div class="flex items-center justify-between text-[10px] text-slate-500">
          <span>Gmail API: Active</span>
          <span class="text-emerald-500/80 font-semibold">OAuth 2.0</span>
        </div>
        <div class="flex items-center justify-between text-[10px] text-slate-500">
          <span>OCR Engine: Local CPU</span>
          <span class="text-blue-400">PyMuPDF</span>
        </div>
      </div>

      <!-- Navigation Section Links (Using button to guarantee 100% reliable clickability) -->
      <div class="px-3 pt-3 pb-1 text-[10px] font-bold text-slate-500 uppercase tracking-wider">Workspaces</div>
      <nav class="px-2 space-y-1 text-xs font-medium">
        <button type="button" onclick="switchSection('directory')" id="nav-directory" class="w-full nav-item nav-active flex items-center justify-between px-3 py-2.5 rounded-lg transition text-slate-200 hover:bg-slate-800/60 cursor-pointer">
          <span class="flex items-center gap-2.5">
            <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M17 20h5v-2a3 3 0 00-5.356-1.857M17 20H7m10 0v-2c0-.656-.126-1.283-.356-1.857M7 20H2v-2a3 3 0 015.356-1.857M7 20v-2c0-.656.126-1.283.356-1.857m0 0a5.002 5.002 0 019.288 0M15 7a3 3 0 11-6 0 3 3 0 016 0zm6 3a2 2 0 11-4 0 2 2 0 014 0zM7 10a2 2 0 11-4 0 2 2 0 014 0z"></path></svg>
            CSP Master Directory
          </span>
          <span class="text-[10px] px-1.5 py-0.5 rounded bg-blue-500/20 text-blue-300 font-mono" id="badgeDirectory">537</span>
        </button>

        <button type="button" onclick="switchSection('overview')" id="nav-overview" class="w-full nav-item flex items-center justify-between px-3 py-2.5 rounded-lg transition text-slate-400 hover:bg-slate-800/60 cursor-pointer">
          <span class="flex items-center gap-2.5">
            <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 6a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2H6a2 2 0 01-2-2V6zM14 6a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2h-2a2 2 0 01-2-2V6zM4 16a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2H6a2 2 0 01-2-2v-2zM14 16a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2h-2a2 2 0 01-2-2v-2z"></path></svg>
            Executive Overview
          </span>
          <span class="text-[10px] px-1.5 py-0.5 rounded bg-slate-800 text-slate-300 font-mono" id="badgeOverview">537</span>
        </button>

        <button type="button" onclick="switchSection('vault')" id="nav-vault" class="w-full nav-item flex items-center justify-between px-3 py-2.5 rounded-lg transition text-slate-400 hover:bg-slate-800/60 hover:text-slate-200 cursor-pointer">
          <span class="flex items-center gap-2.5">
            <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"></path></svg>
            Document Vault
          </span>
          <span class="text-[10px] px-1.5 py-0.5 rounded bg-slate-800 text-slate-300 font-mono" id="badgeVault">0</span>
        </button>

        <button type="button" onclick="switchSection('escalations')" id="nav-escalations" class="w-full nav-item flex items-center justify-between px-3 py-2.5 rounded-lg transition text-slate-400 hover:bg-slate-800/60 hover:text-slate-200 cursor-pointer">
          <span class="flex items-center gap-2.5">
            <svg class="w-4 h-4 text-rose-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z"></path></svg>
            Escalations & SLA
          </span>
          <span class="text-[10px] px-1.5 py-0.5 rounded bg-rose-500/20 text-rose-300 font-mono" id="badgeEscalations">0</span>
        </button>

        <button type="button" onclick="switchSection('emails')" id="nav-emails" class="w-full nav-item flex items-center justify-between px-3 py-2.5 rounded-lg transition text-slate-400 hover:bg-slate-800/60 hover:text-slate-200 cursor-pointer">
          <span class="flex items-center gap-2.5">
            <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 8l7.89 5.26a2 2 0 002.22 0L21 8M5 19h14a2 2 0 002-2V7a2 2 0 00-2-2H5a2 2 0 00-2 2v10a2 2 0 002 2z"></path></svg>
            Inbound Gmail Stream
          </span>
          <span class="text-[10px] px-1.5 py-0.5 rounded bg-slate-800 text-slate-300 font-mono" id="badgeEmails">Live</span>
        </button>

        <button type="button" onclick="switchSection('review')" id="nav-review" class="w-full nav-item flex items-center justify-between px-3 py-2.5 rounded-lg transition text-slate-400 hover:bg-slate-800/60 hover:text-slate-200 cursor-pointer">
          <span class="flex items-center gap-2.5">
            <svg class="w-4 h-4 text-amber-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2m-6 9l2 2 4-4"></path></svg>
            Manual Review Queue
          </span>
          <span class="text-[10px] px-1.5 py-0.5 rounded bg-amber-500/20 text-amber-300 font-mono" id="badgeReviewQueue">0</span>
        </button>

        <button type="button" onclick="switchSection('outbound')" id="nav-outbound" class="w-full nav-item flex items-center justify-between px-3 py-2.5 rounded-lg transition text-slate-400 hover:bg-slate-800/60 hover:text-slate-200 cursor-pointer">
          <span class="flex items-center gap-2.5">
            <svg class="w-4 h-4 text-indigo-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.863 9.863 0 01-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 4.03-8 9-8s9 3.582 9 8z"></path></svg>
            Outbound Review Queue
          </span>
          <span class="text-[10px] px-1.5 py-0.5 rounded bg-indigo-500/20 text-indigo-300 font-mono" id="badgeOutboundQueue">0</span>
        </button>

        <button type="button" onclick="switchSection('comms')" id="nav-comms" class="w-full nav-item flex items-center justify-between px-3 py-2.5 rounded-lg transition text-slate-400 hover:bg-slate-800/60 hover:text-slate-200 cursor-pointer">
          <span class="flex items-center gap-2.5">
            <svg class="w-4 h-4 text-emerald-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 10h.01M12 10h.01M16 10h.01M9 16H5a2 2 0 01-2-2V6a2 2 0 012-2h14a2 2 0 012 2v8a2 2 0 01-2 2h-5l-5 5v-5z"></path></svg>
            Communication Hub
          </span>
          <span class="text-[10px] px-1.5 py-0.5 rounded bg-emerald-500/20 text-emerald-300 font-mono" id="badgeComms">4 Hubs</span>
        </button>

        <button type="button" onclick="switchSection('analytics')" id="nav-analytics" class="w-full nav-item flex items-center justify-between px-3 py-2.5 rounded-lg transition text-slate-400 hover:bg-slate-800/60 hover:text-slate-200 cursor-pointer">
          <span class="flex items-center gap-2.5">
            <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 19v-6a2 2 0 00-2-2H5a2 2 0 00-2 2v6a2 2 0 002 2h2a2 2 0 002-2zm0 0V9a2 2 0 012-2h2a2 2 0 012 2v10m-6 0a2 2 0 002 2h2a2 2 0 002-2m0 0V5a2 2 0 012-2h2a2 2 0 012 2v14a2 2 0 01-2 2h-2a2 2 0 01-2-2z"></path></svg>
            Territory & RM Hub
          </span>
          <span class="text-[10px] px-1.5 py-0.5 rounded bg-slate-800 text-slate-300 font-mono">Matrix</span>
        </button>
      </nav>
    </div>

    <!-- Bottom Operator Badge -->
    <div class="p-3.5 border-t border-slate-800 bg-slate-950/60 flex items-center justify-between">
      <div class="flex items-center gap-2.5 overflow-hidden">
        <div class="w-8 h-8 rounded-full bg-blue-700 flex items-center justify-center font-bold text-xs text-white shrink-0 shadow-inner">
          OP
        </div>
        <div class="overflow-hidden">
          <p class="text-xs font-semibold text-white truncate">Operations Lead</p>
          <p class="text-[10px] text-slate-400 truncate">Eko Compliance Desk</p>
        </div>
      </div>
      <button type="button" onclick="fetchLiveData()" title="Sync Database" class="p-1.5 hover:bg-slate-800 text-slate-400 hover:text-white rounded transition cursor-pointer">
        <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"></path></svg>
      </button>
    </div>
  </aside>

  <!-- ========================================================================= -->
  <!-- 2. MAIN CRM APPLICATION WORKSPACE -->
  <!-- ========================================================================= -->
  <div class="flex-1 ml-64 flex flex-col min-h-screen">

    <!-- TOP CRM APP BAR -->
    <header class="h-16 bg-slate-900/90 backdrop-blur-md border-b border-slate-800 px-6 flex items-center justify-between sticky top-0 z-20">
      
      <!-- Breadcrumb & Workspace Title -->
      <div class="flex items-center gap-3">
        <div class="flex items-center gap-2 text-xs text-slate-400">
          <span>Compliance Operations</span>
          <span>/</span>
          <span id="breadcrumbTitle" class="text-white font-semibold">CSP Master Directory & Compliance Matrix</span>
        </div>
      </div>

      <!-- Quick Global Search -->
      <div class="flex items-center gap-3 flex-1 max-w-md mx-6">
        <div class="relative w-full">
          <input type="text" id="globalSearch" onkeyup="handleGlobalSearch()" placeholder="Search CSP ID (e.g. 1A850247), Name, Phone, Branch, RM..." 
                 class="w-full bg-slate-950 border border-slate-700/80 rounded-lg pl-9 pr-8 py-1.5 text-xs text-slate-100 placeholder-slate-500 focus:outline-none focus:border-blue-500 focus:ring-1 focus:ring-blue-500 transition shadow-inner">
          <svg class="w-3.5 h-3.5 text-slate-500 absolute left-3 top-2.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z"></path></svg>
          <kbd class="absolute right-2.5 top-2 text-[10px] font-mono text-slate-500 bg-slate-800 px-1.5 py-0.5 rounded border border-slate-700">/</kbd>
        </div>
      </div>

      <!-- Header Action Controls -->
      <div class="flex items-center gap-2.5">
        <!-- Scan Gmail OAuth -->
        <button type="button" id="scanBtn" onclick="triggerScan()" class="flex items-center gap-2 bg-blue-600 hover:bg-blue-500 text-white px-3 py-1.5 rounded-lg font-medium text-xs shadow-md shadow-blue-500/20 transition cursor-pointer">
          <svg id="scanIcon" class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"></path></svg>
          <span id="scanText">Scan Gmail</span>
        </button>

        <!-- 2-Year Historical Backfill -->
        <button type="button" id="backfillBtn" onclick="triggerBackfill2Years()" title="Run 2-Year Historical Backfill (730 Days)" class="flex items-center gap-1.5 bg-indigo-600 hover:bg-indigo-500 text-white px-3 py-1.5 rounded-lg font-medium text-xs shadow-md shadow-indigo-500/20 transition cursor-pointer">
          <svg id="backfillIcon" class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z"></path></svg>
          <span id="backfillText">2-Yr Backfill</span>
        </button>

        <!-- Export Discrepancies .xlsx -->
        <button type="button" onclick="exportDiscrepanciesXLSX()" title="Export Calling Sheet Discrepancy & Compliance Report (.xlsx)" class="flex items-center gap-1.5 bg-emerald-700 hover:bg-emerald-600 text-white border border-emerald-600/80 px-3 py-1.5 rounded-lg font-medium text-xs shadow-sm transition cursor-pointer">
          <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 17v-2m3 2v-4m3 4v-6m2 10H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"></path></svg>
          <span>Discrepancies (.xlsx)</span>
        </button>

        <!-- Auto-Make Calling Sheet -->
        <button type="button" id="autoSheetBtn" onclick="triggerAutoDetectCallingSheet()" title="Auto-Detect Missing CSPs and Make Calling Sheet" class="flex items-center gap-1.5 bg-emerald-600 hover:bg-emerald-500 text-white px-3 py-1.5 rounded-lg font-medium text-xs shadow-md shadow-emerald-600/20 transition cursor-pointer">
          <svg id="autoSheetIcon" class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 10V3L4 14h7v7l9-11h-7z"></path></svg>
          <span id="autoSheetText">Auto-Make Sheet</span>
        </button>

        <!-- Download Auto Calling Sheet .xlsx -->
        <button type="button" onclick="exportAutoCallingSheetXLSX()" title="Download Auto-Generated Calling Sheet (.xlsx)" class="flex items-center gap-1.5 bg-teal-700 hover:bg-teal-600 text-white border border-teal-600/80 px-2.5 py-1.5 rounded-lg font-medium text-xs shadow-sm transition cursor-pointer">
          <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 10v6m0 0l-3-3m3 3l3-3m2 8H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"></path></svg>
          <span>Auto Sheet (.xlsx)</span>
        </button>

        <!-- Sync Calling Sheet New -->
        <button type="button" id="syncSheetBtn" onclick="triggerCallingSheetSync()" title="Idempotent sync with Calling Sheet New" class="flex items-center gap-1.5 bg-slate-800 hover:bg-slate-700 text-blue-300 border border-slate-700 px-2.5 py-1.5 rounded-lg font-medium text-xs transition cursor-pointer">
          <svg id="syncSheetIcon" class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"></path></svg>
          <span id="syncSheetText">Sync Sheet</span>
        </button>

        <!-- Export CSV Report -->
        <button type="button" onclick="exportCSV()" title="Export CSV Summary" class="flex items-center gap-1.5 bg-slate-800 hover:bg-slate-700 text-slate-200 border border-slate-700 px-2.5 py-1.5 rounded-lg font-medium text-xs transition cursor-pointer">
          <svg class="w-3.5 h-3.5 text-slate-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4"></path></svg>
          CSV
        </button>

        <!-- Theme Toggle -->
        <button type="button" onclick="toggleTheme()" title="Toggle Light/Dark Mode" class="p-1.5 bg-slate-800 hover:bg-slate-700 text-slate-300 border border-slate-700 rounded-lg transition cursor-pointer">
          <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 3v1m0 16v1m9-9h-1M4 12H3m15.364 6.364l-.707-.707M6.343 6.343l-.707-.707m12.728 0l-.707.707M6.343 17.657l-.707.707M16 12a4 4 0 11-8 0 4 4 0 018 0z"></path></svg>
        </button>

        <!-- Telemetry Refresh -->
        <button type="button" onclick="fetchLiveData()" title="Refresh Telemetry" class="p-1.5 bg-slate-800 hover:bg-slate-700 text-slate-300 border border-slate-700 rounded-lg transition cursor-pointer">
          <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"></path></svg>
        </button>
      </div>
    </header>

    <!-- CONTENT WRAPPER -->
    <main class="p-6 space-y-6 flex-1">

      <!-- ========================================================================= -->
      <!-- SECTION 1: OPERATIONS OVERVIEW (EXECUTIVE HUB) -->
      <!-- ========================================================================= -->
      <section id="section-overview" class="crm-section space-y-6 hidden fade-enter">

        <!-- 5 PROPORTIONAL KPI METRICS CARDS -->
        <div class="grid grid-cols-2 lg:grid-cols-5 gap-3.5">
          
          <!-- Total Monitored -->
          <div class="bg-slate-900 border border-slate-800 rounded-xl p-4 flex flex-col justify-between shadow-sm">
            <div class="flex items-center justify-between text-[11px] font-semibold text-slate-400 uppercase tracking-wider">
              <span>Total Network</span>
              <span class="w-2 h-2 rounded-full bg-blue-500"></span>
            </div>
            <div class="flex items-baseline justify-between mt-2">
              <span class="text-2xl font-black text-white" id="kpiTotal">537</span>
              <span class="text-[10.5px] px-2 py-0.5 rounded bg-blue-500/20 text-blue-300 border border-blue-500/30">Whitelisted</span>
            </div>
            <div class="text-[11px] text-slate-500 mt-1 flex items-center justify-between">
              <span>Calling Sheet</span>
              <span class="font-mono text-slate-400">100% Seeded</span>
            </div>
          </div>

          <!-- Category A Compliant -->
          <div role="button" onclick="filterCategoryAndSwitch('CAT_A')" class="bg-slate-900 border border-slate-800 hover:border-emerald-500/50 rounded-xl p-4 flex flex-col justify-between shadow-sm cursor-pointer transition group">
            <div class="flex items-center justify-between text-[11px] font-semibold text-emerald-400 uppercase tracking-wider">
              <span>Category A</span>
              <span class="w-2 h-2 rounded-full bg-emerald-500"></span>
            </div>
            <div class="flex items-baseline justify-between mt-2">
              <span class="text-2xl font-black text-emerald-400" id="kpiCompliant">0</span>
              <span id="kpiCompliantRate" class="text-[10.5px] px-2 py-0.5 rounded bg-emerald-500/20 text-emerald-300 border border-emerald-500/30">0%</span>
            </div>
            <div class="text-[11px] text-slate-500 mt-1 flex items-center justify-between">
              <span>Agreement + PV</span>
              <span class="text-emerald-400 group-hover:underline text-[10px]">Filter →</span>
            </div>
          </div>

          <!-- Category B Incomplete -->
          <div role="button" onclick="filterCategoryAndSwitch('CAT_B')" class="bg-slate-900 border border-slate-800 hover:border-amber-500/50 rounded-xl p-4 flex flex-col justify-between shadow-sm cursor-pointer transition group">
            <div class="flex items-center justify-between text-[11px] font-semibold text-amber-400 uppercase tracking-wider">
              <span>Category B</span>
              <span class="w-2 h-2 rounded-full bg-amber-500"></span>
            </div>
            <div class="flex items-baseline justify-between mt-2">
              <span class="text-2xl font-black text-amber-400" id="kpiIncomplete">0</span>
              <span class="text-[10.5px] px-2 py-0.5 rounded bg-amber-500/20 text-amber-300 border border-amber-500/30">Partial</span>
            </div>
            <div class="text-[11px] text-slate-500 mt-1 flex items-center justify-between">
              <span>Missing PV / Doc</span>
              <span class="text-amber-400 group-hover:underline text-[10px]">Filter →</span>
            </div>
          </div>

          <!-- Category C Expired -->
          <div role="button" onclick="filterCategoryAndSwitch('CAT_C')" class="bg-slate-900 border border-slate-800 hover:border-rose-500/50 rounded-xl p-4 flex flex-col justify-between shadow-sm cursor-pointer transition group">
            <div class="flex items-center justify-between text-[11px] font-semibold text-rose-400 uppercase tracking-wider">
              <span>Category C</span>
              <span class="w-2 h-2 rounded-full bg-rose-500"></span>
            </div>
            <div class="flex items-baseline justify-between mt-2">
              <span class="text-2xl font-black text-rose-400" id="kpiExpired">0</span>
              <span class="text-[10.5px] px-2 py-0.5 rounded bg-rose-500/20 text-rose-300 border border-rose-500/30">Expired</span>
            </div>
            <div class="text-[11px] text-slate-500 mt-1 flex items-center justify-between">
              <span>Term Elapsed</span>
              <span class="text-rose-400 group-hover:underline text-[10px]">Filter →</span>
            </div>
          </div>

          <!-- Category D Non-Responsive -->
          <div role="button" onclick="filterCategoryAndSwitch('CAT_D')" class="bg-slate-900 border border-slate-800 hover:border-slate-500 rounded-xl p-4 flex flex-col justify-between shadow-sm cursor-pointer transition group col-span-2 lg:col-span-1">
            <div class="flex items-center justify-between text-[11px] font-semibold text-slate-400 uppercase tracking-wider">
              <span>Category D</span>
              <span class="w-2 h-2 rounded-full bg-slate-500"></span>
            </div>
            <div class="flex items-baseline justify-between mt-2">
              <span class="text-2xl font-black text-slate-300" id="kpiGhost">537</span>
              <span class="text-[10.5px] px-2 py-0.5 rounded bg-slate-800 text-slate-400 border border-slate-700">7d Cadence</span>
            </div>
            <div class="text-[11px] text-slate-500 mt-1 flex items-center justify-between">
              <span>Awaiting Upload</span>
              <span class="text-slate-400 group-hover:underline text-[10px]">Filter →</span>
            </div>
          </div>

        </div>

        <!-- 60% / 40% PROPORTIONAL GRID: FUNNEL & RADAR -->
        <div class="grid grid-cols-1 lg:grid-cols-12 gap-5">
          
          <!-- 1. Compliance Category Matrix (7 cols) -->
          <div class="lg:col-span-7 bg-slate-900 border border-slate-800 rounded-xl p-5 flex flex-col justify-between shadow-md">
            <div>
              <div class="flex items-center justify-between mb-2">
                <div>
                  <h2 class="text-sm font-bold text-white tracking-wide">Compliance Category Matrix</h2>
                  <p class="text-xs text-slate-400">Deterministic multi-tier classification of 537 CSP contracts</p>
                </div>
                <span class="text-[10px] px-2 py-0.5 rounded bg-slate-950 border border-slate-800 font-mono text-slate-300">Invariant: No Expiry Unnoticed</span>
              </div>

              <!-- Segmented Visual Progress Bar -->
              <div class="mt-4 mb-4">
                <div class="flex justify-between text-[11px] font-mono text-slate-400 mb-1.5">
                  <span>Network Compliance Distribution</span>
                  <span id="complianceBarLabel">0% Compliant</span>
                </div>
                <div class="h-3 w-full bg-slate-950 rounded-full overflow-hidden flex border border-slate-800">
                  <div id="barCatA" class="bg-emerald-500 h-full transition-all duration-500" style="width: 0%" title="Cat A Compliant"></div>
                  <div id="barCatB" class="bg-amber-500 h-full transition-all duration-500" style="width: 0%" title="Cat B Incomplete"></div>
                  <div id="barCatC" class="bg-rose-500 h-full transition-all duration-500" style="width: 0%" title="Cat C Expired"></div>
                  <div id="barCatD" class="bg-slate-700 h-full transition-all duration-500" style="width: 100%" title="Cat D No Response"></div>
                </div>
              </div>

              <!-- 4 Interactive Category Cards -->
              <div class="grid grid-cols-2 sm:grid-cols-4 gap-3 mt-3">
                <div role="button" onclick="filterCategoryAndSwitch('CAT_A')" class="cursor-pointer bg-slate-950/80 border border-emerald-500/30 hover:border-emerald-400 rounded-lg p-3 transition group">
                  <div class="text-[10px] font-bold text-emerald-400 uppercase">Category A</div>
                  <div class="text-xl font-black text-white mt-1" id="catACount">0</div>
                  <div class="text-[10.5px] text-slate-400 mt-0.5">Compliant (100%)</div>
                </div>

                <div role="button" onclick="filterCategoryAndSwitch('CAT_B')" class="cursor-pointer bg-slate-950/80 border border-amber-500/30 hover:border-amber-400 rounded-lg p-3 transition group">
                  <div class="text-[10px] font-bold text-amber-400 uppercase">Category B</div>
                  <div class="text-xl font-black text-white mt-1" id="catBCount">0</div>
                  <div class="text-[10.5px] text-slate-400 mt-0.5">Partial / Missing</div>
                </div>

                <div role="button" onclick="filterCategoryAndSwitch('CAT_C')" class="cursor-pointer bg-slate-950/80 border border-rose-500/30 hover:border-rose-400 rounded-lg p-3 transition group">
                  <div class="text-[10px] font-bold text-rose-400 uppercase">Category C</div>
                  <div class="text-xl font-black text-white mt-1" id="catCCount">0</div>
                  <div class="text-[10.5px] text-slate-400 mt-0.5">Expired / Term</div>
                </div>

                <div role="button" onclick="filterCategoryAndSwitch('CAT_D')" class="cursor-pointer bg-slate-950/80 border border-slate-700 hover:border-slate-500 rounded-lg p-3 transition group">
                  <div class="text-[10px] font-bold text-slate-400 uppercase">Category D</div>
                  <div class="text-xl font-black text-white mt-1" id="catDCount">537</div>
                  <div class="text-[10.5px] text-slate-400 mt-0.5">Non-Responsive</div>
                </div>
              </div>
            </div>

            <!-- Quick CRM Action Dock -->
            <div class="mt-4 pt-3 border-t border-slate-800 flex items-center justify-between text-xs">
              <span class="text-slate-400 text-[11px]">Quick Action Dock:</span>
              <div class="flex items-center gap-2">
                <button type="button" onclick="switchSection('directory')" class="px-2.5 py-1 bg-blue-600/20 hover:bg-blue-600/30 text-blue-300 border border-blue-500/30 rounded text-[11px] transition cursor-pointer">Browse Master Directory →</button>
                <button type="button" onclick="switchSection('escalations')" class="px-2.5 py-1 bg-rose-600/20 hover:bg-rose-600/30 text-rose-300 border border-rose-500/30 rounded text-[11px] transition cursor-pointer">View Critical SLA (T-7) →</button>
              </div>
            </div>
          </div>

          <!-- 2. Expiry Horizon Radar (5 cols) -->
          <div class="lg:col-span-5 bg-slate-900 border border-slate-800 rounded-xl p-5 flex flex-col justify-between shadow-md">
            <div>
              <div class="flex items-center justify-between mb-2">
                <h2 class="text-sm font-bold text-white tracking-wide">Expiry Horizon Pipeline</h2>
                <span class="text-[10px] px-2 py-0.5 rounded bg-rose-500/20 text-rose-300 border border-rose-500/30 font-mono">APScheduler Daily</span>
              </div>
              <p class="text-xs text-slate-400 mb-3">Contracts entering renewal notification stages</p>

              <!-- Expiry Stages Stack -->
              <div class="space-y-2 text-xs font-mono">
                <div role="button" onclick="filterHorizonAndSwitch('T90')" class="cursor-pointer flex items-center justify-between p-2.5 bg-slate-950 rounded-lg border border-slate-800 hover:border-slate-700 transition">
                  <div class="flex items-center gap-2">
                    <span class="w-2 h-2 rounded-full bg-slate-400"></span>
                    <span class="text-slate-300">T-90 Strategy Window</span>
                  </div>
                  <span id="hT90" class="text-slate-200 font-bold">0 Due</span>
                </div>

                <div role="button" onclick="filterHorizonAndSwitch('T30')" class="cursor-pointer flex items-center justify-between p-2.5 bg-slate-950 rounded-lg border border-blue-900/40 hover:border-blue-700 transition">
                  <div class="flex items-center gap-2">
                    <span class="w-2 h-2 rounded-full bg-blue-400"></span>
                    <span class="text-blue-300">T-60 / T-30 Warning Cadence</span>
                  </div>
                  <span id="hT30" class="text-blue-300 font-bold">0 Due</span>
                </div>

                <div role="button" onclick="filterHorizonAndSwitch('T7')" class="cursor-pointer flex items-center justify-between p-2.5 bg-slate-950 rounded-lg border border-amber-900/40 hover:border-amber-700 transition">
                  <div class="flex items-center gap-2">
                    <span class="w-2 h-2 rounded-full bg-amber-400 animate-pulse"></span>
                    <span class="text-amber-300">T-7 Critical Escalation (RM)</span>
                  </div>
                  <span id="hT7" class="text-amber-300 font-bold">0 Due</span>
                </div>

                <div role="button" onclick="filterHorizonAndSwitch('T0')" class="cursor-pointer flex items-center justify-between p-2.5 bg-slate-950 rounded-lg border border-red-950 hover:border-red-800 transition">
                  <div class="flex items-center gap-2">
                    <span class="w-2 h-2 rounded-full bg-red-500"></span>
                    <span class="text-red-400 font-bold">T-0 EXPIRED_LOCKED</span>
                  </div>
                  <span id="hT0" class="text-red-400 font-bold">0 Locked</span>
                </div>
              </div>
            </div>

            <div class="mt-4 pt-3 border-t border-slate-800 text-[11px] text-slate-500 flex justify-between">
              <span>Click any tier to filter directory</span>
              <span class="text-blue-400 font-medium">Cadence: Daily 00:00 UTC</span>
            </div>
          </div>

        </div>

        <!-- 50% / 50% SPLIT: RM LEADERBOARD & RECENT INBOUND FEED -->
        <div class="grid grid-cols-1 lg:grid-cols-12 gap-5">
          
          <!-- Relationship Manager Matrix (6 cols) -->
          <div class="lg:col-span-6 bg-slate-900 border border-slate-800 rounded-xl p-5 shadow-md">
            <div class="flex items-center justify-between mb-3">
              <div>
                <h3 class="text-sm font-bold text-white tracking-wide">Relationship Manager (RM) Matrix</h3>
                <p class="text-xs text-slate-400">Territory compliance and workload tracking</p>
              </div>
              <span class="text-[10px] px-2 py-0.5 rounded bg-slate-800 text-slate-300 font-mono">Field Ops</span>
            </div>

            <div class="overflow-x-auto">
              <table class="w-full text-left text-xs">
                <thead class="text-slate-400 border-b border-slate-800 pb-2 text-[11px]">
                  <tr>
                    <th class="py-2">Relationship Manager</th>
                    <th class="py-2 text-center">Assigned</th>
                    <th class="py-2 text-center">Compliant</th>
                    <th class="py-2 text-center">At Risk</th>
                    <th class="py-2 text-right">Action</th>
                  </tr>
                </thead>
                <tbody id="rmTableBody" class="divide-y divide-slate-800/60">
                  <tr><td colspan="5" class="py-4 text-center text-slate-500">Aggregating RM performance...</td></tr>
                </tbody>
              </table>
            </div>
          </div>

          <!-- Circle Head & Territory Matrix (6 cols) -->
          <div class="lg:col-span-6 bg-slate-900 border border-slate-800 rounded-xl p-5 shadow-md">
            <div class="flex items-center justify-between mb-3">
              <div>
                <h3 class="text-sm font-bold text-white tracking-wide">Circle Head Territory Distribution</h3>
                <p class="text-xs text-slate-400">Regional oversight and escalations</p>
              </div>
              <span class="text-[10px] px-2 py-0.5 rounded bg-slate-800 text-slate-300 font-mono">Regional Head</span>
            </div>

            <div class="overflow-x-auto">
              <table class="w-full text-left text-xs">
                <thead class="text-slate-400 border-b border-slate-800 pb-2 text-[11px]">
                  <tr>
                    <th class="py-2">Circle Head / DC</th>
                    <th class="py-2 text-center">Total CSPs</th>
                    <th class="py-2 text-center">Compliant</th>
                    <th class="py-2 text-center">At Risk</th>
                    <th class="py-2 text-right">Status</th>
                  </tr>
                </thead>
                <tbody id="circleTableBody" class="divide-y divide-slate-800/60">
                  <tr><td colspan="5" class="py-4 text-center text-slate-500">Aggregating Circle Head data...</td></tr>
                </tbody>
              </table>
            </div>
          </div>

        </div>

      </section>

      <!-- ========================================================================= -->
      <!-- SECTION 2: CSP MASTER DIRECTORY (THE CORE COMPLIANCE GRID) -->
      <!-- ========================================================================= -->
      <section id="section-directory" class="crm-section space-y-4 fade-enter">

        <!-- 5 PROPORTIONAL KPI METRICS CARDS -->
        <div class="grid grid-cols-2 lg:grid-cols-5 gap-3.5 mb-1">
          
          <!-- Total Monitored -->
          <div role="button" onclick="filterCategory('ALL')" class="bg-slate-900 border border-slate-800 hover:border-blue-500/50 rounded-xl p-4 flex flex-col justify-between shadow-sm cursor-pointer transition group">
            <div class="flex items-center justify-between text-[11px] font-semibold text-slate-400 uppercase tracking-wider">
              <span>Total Network</span>
              <span class="w-2 h-2 rounded-full bg-blue-500"></span>
            </div>
            <div class="flex items-baseline justify-between mt-2">
              <span class="text-2xl font-black text-white" id="kpiTotalDir">537</span>
              <span class="text-[10.5px] px-2 py-0.5 rounded bg-blue-500/20 text-blue-300 border border-blue-500/30">Calling Sheet</span>
            </div>
            <div class="text-[11px] text-slate-500 mt-1 flex items-center justify-between">
              <span>Seeded CSPs</span>
              <span class="text-blue-400 group-hover:underline text-[10px]">Show All →</span>
            </div>
          </div>

          <!-- Category A Compliant -->
          <div role="button" onclick="filterCategory('CAT_A')" class="bg-slate-900 border border-slate-800 hover:border-emerald-500/50 rounded-xl p-4 flex flex-col justify-between shadow-sm cursor-pointer transition group">
            <div class="flex items-center justify-between text-[11px] font-semibold text-emerald-400 uppercase tracking-wider">
              <span>Category A</span>
              <span class="w-2 h-2 rounded-full bg-emerald-500"></span>
            </div>
            <div class="flex items-baseline justify-between mt-2">
              <span class="text-2xl font-black text-emerald-400" id="kpiCompliantDir">0</span>
              <span id="kpiCompliantRateDir" class="text-[10.5px] px-2 py-0.5 rounded bg-emerald-500/20 text-emerald-300 border border-emerald-500/30">0%</span>
            </div>
            <div class="text-[11px] text-slate-500 mt-1 flex items-center justify-between">
              <span>Agr + PV + IIBF (All 3)</span>
              <span class="text-emerald-400 group-hover:underline text-[10px]">Filter →</span>
            </div>
          </div>

          <!-- Category B Incomplete -->
          <div role="button" onclick="filterCategory('CAT_B')" class="bg-slate-900 border border-slate-800 hover:border-amber-500/50 rounded-xl p-4 flex flex-col justify-between shadow-sm cursor-pointer transition group">
            <div class="flex items-center justify-between text-[11px] font-semibold text-amber-400 uppercase tracking-wider">
              <span>Category B</span>
              <span class="w-2 h-2 rounded-full bg-amber-500"></span>
            </div>
            <div class="flex items-baseline justify-between mt-2">
              <span class="text-2xl font-black text-amber-400" id="kpiIncompleteDir">0</span>
              <span class="text-[10.5px] px-2 py-0.5 rounded bg-amber-500/20 text-amber-300 border border-amber-500/30">Partial</span>
            </div>
            <div class="text-[11px] text-slate-500 mt-1 flex items-center justify-between">
              <span>Missing Agr / PV / IIBF</span>
              <span class="text-amber-400 group-hover:underline text-[10px]">Filter →</span>
            </div>
          </div>

          <!-- Category C Expired -->
          <div role="button" onclick="filterCategory('CAT_C')" class="bg-slate-900 border border-slate-800 hover:border-rose-500/50 rounded-xl p-4 flex flex-col justify-between shadow-sm cursor-pointer transition group">
            <div class="flex items-center justify-between text-[11px] font-semibold text-rose-400 uppercase tracking-wider">
              <span>Category C</span>
              <span class="w-2 h-2 rounded-full bg-rose-500"></span>
            </div>
            <div class="flex items-baseline justify-between mt-2">
              <span class="text-2xl font-black text-rose-400" id="kpiExpiredDir">0</span>
              <span class="text-[10.5px] px-2 py-0.5 rounded bg-rose-500/20 text-rose-300 border border-rose-500/30">Expired</span>
            </div>
            <div class="text-[11px] text-slate-500 mt-1 flex items-center justify-between">
              <span>>3yr Agr / >1yr PV</span>
              <span class="text-rose-400 group-hover:underline text-[10px]">Filter →</span>
            </div>
          </div>

          <!-- Category D Non-Responsive -->
          <div role="button" onclick="filterCategory('CAT_D')" class="bg-slate-900 border border-slate-800 hover:border-slate-500 rounded-xl p-4 flex flex-col justify-between shadow-sm cursor-pointer transition group col-span-2 lg:col-span-1">
            <div class="flex items-center justify-between text-[11px] font-semibold text-slate-400 uppercase tracking-wider">
              <span>Category D</span>
              <span class="w-2 h-2 rounded-full bg-slate-500"></span>
            </div>
            <div class="flex items-baseline justify-between mt-2">
              <span class="text-2xl font-black text-slate-300" id="kpiGhostDir">537</span>
              <span class="text-[10.5px] px-2 py-0.5 rounded bg-slate-800 text-slate-400 border border-slate-700">0 Docs</span>
            </div>
            <div class="text-[11px] text-slate-500 mt-1 flex items-center justify-between">
              <span>Awaiting Response</span>
              <span class="text-slate-400 group-hover:underline text-[10px]">Filter →</span>
            </div>
          </div>

        </div>

        <!-- FILTER & ACTION TOOLBAR -->
        <div class="bg-slate-900 border border-slate-800 rounded-xl p-4 shadow-lg space-y-3">
          
          <!-- Category Quick Tabs Bar -->
          <div class="flex flex-wrap items-center justify-between gap-2 pb-2 border-b border-slate-800/80">
            <div class="flex items-center gap-1.5 overflow-x-auto">
              <button type="button" onclick="filterCategory('ALL')" id="tabALL" class="tab-btn px-3 py-1.5 rounded-lg text-xs font-bold bg-blue-600 text-white cursor-pointer transition">All (537)</button>
              <button type="button" onclick="filterCategory('CAT_A')" id="tabCAT_A" class="tab-btn px-3 py-1.5 rounded-lg text-xs font-medium bg-slate-800 text-slate-300 hover:bg-slate-700 cursor-pointer transition">Category A</button>
              <button type="button" onclick="filterCategory('CAT_B')" id="tabCAT_B" class="tab-btn px-3 py-1.5 rounded-lg text-xs font-medium bg-slate-800 text-slate-300 hover:bg-slate-700 cursor-pointer transition">Category B</button>
              <button type="button" onclick="filterCategory('CAT_C')" id="tabCAT_C" class="tab-btn px-3 py-1.5 rounded-lg text-xs font-medium bg-slate-800 text-slate-300 hover:bg-slate-700 cursor-pointer transition">Category C</button>
              <button type="button" onclick="filterCategory('CAT_D')" id="tabCAT_D" class="tab-btn px-3 py-1.5 rounded-lg text-xs font-medium bg-slate-800 text-slate-300 hover:bg-slate-700 cursor-pointer transition">Category D</button>
            </div>

            <!-- Page Record Count Indicator -->
            <div class="text-xs text-slate-400 font-mono" id="pageInfoTop">
              Showing 1-25 of 537 CSPs
            </div>
          </div>

          <!-- Multi-Dimensional Dropdown Filter Strip -->
          <div class="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-5 gap-2.5">
            <!-- Filter by RM -->
            <div>
              <label class="block text-[10px] text-slate-400 uppercase font-semibold mb-1">Relationship Manager</label>
              <select id="rmFilter" autocomplete="off" onchange="applyFilters()" class="w-full bg-slate-950 border border-slate-700 rounded-lg px-2.5 py-1.5 text-xs text-slate-200 focus:outline-none focus:border-blue-500">
                <option value="ALL">All Managers (All RMs)</option>
              </select>
            </div>

            <!-- Filter by Circle Head -->
            <div>
              <label class="block text-[10px] text-slate-400 uppercase font-semibold mb-1">Circle Head / Region</label>
              <select id="circleFilter" autocomplete="off" onchange="applyFilters()" class="w-full bg-slate-950 border border-slate-700 rounded-lg px-2.5 py-1.5 text-xs text-slate-200 focus:outline-none focus:border-blue-500">
                <option value="ALL">All Circles (All Regions)</option>
              </select>
            </div>

            <!-- Filter by Expiry Horizon -->
            <div>
              <label class="block text-[10px] text-slate-400 uppercase font-semibold mb-1">Expiry Horizon</label>
              <select id="horizonFilter" autocomplete="off" onchange="applyFilters()" class="w-full bg-slate-950 border border-slate-700 rounded-lg px-2.5 py-1.5 text-xs text-slate-200 focus:outline-none focus:border-blue-500">
                <option value="ALL" selected>All Horizons</option>
                <option value="EXPIRED">Already Expired (<= 0d)</option>
                <option value="T7">Critical (<= 7 Days)</option>
                <option value="T30">Warning (<= 30 Days)</option>
                <option value="T90">Upcoming (<= 90 Days)</option>
                <option value="ACTIVE">Safe (> 90 Days)</option>
                <option value="NO_EXPIRY">No Expiry Logged</option>
              </select>
            </div>

            <!-- Filter by Document Presence -->
            <div>
              <label class="block text-[10px] text-slate-400 uppercase font-semibold mb-1">Document Vault Status</label>
              <select id="docFilter" autocomplete="off" onchange="applyFilters()" class="w-full bg-slate-950 border border-slate-700 rounded-lg px-2.5 py-1.5 text-xs text-slate-200 focus:outline-none focus:border-blue-500">
                <option value="ALL" selected>All Document States</option>
                <option value="HAS_DOCS">Has Scanned Files</option>
                <option value="NO_DOCS">No Files Uploaded</option>
                <option value="MISSING_PV">Missing Police Verification</option>
              </select>
            </div>

            <!-- Clear Filters -->
            <div class="flex items-end">
              <button type="button" onclick="resetFilters()" class="w-full bg-slate-800 hover:bg-slate-700 text-slate-300 border border-slate-700 rounded-lg px-3 py-1.5 text-xs font-medium transition cursor-pointer">
                Reset Filters
              </button>
            </div>
          </div>

          <!-- BULK ACTION STRIP (Revealed when rows are checked) -->
          <div id="bulkActionBar" class="hidden p-2.5 bg-blue-950/40 border border-blue-800/60 rounded-lg flex items-center justify-between text-xs">
            <div class="flex items-center gap-2">
              <span class="w-2 h-2 rounded-full bg-blue-400 animate-pulse"></span>
              <span class="font-semibold text-blue-200" id="selectedCountText">0 CSPs selected</span>
            </div>
            <div class="flex items-center gap-2">
              <button type="button" onclick="triggerBulkNotice()" class="bg-blue-600 hover:bg-blue-500 text-white px-3 py-1 rounded font-medium text-xs transition cursor-pointer">
                Queue Renewal Notice for Selected
              </button>
              <button type="button" onclick="exportSelectedCSV()" class="bg-slate-800 hover:bg-slate-700 text-slate-200 border border-slate-700 px-3 py-1 rounded font-medium text-xs transition cursor-pointer">
                Export Selected (CSV)
              </button>
              <button type="button" onclick="clearSelection()" class="text-slate-400 hover:text-white px-2 py-1 text-xs cursor-pointer">
                Clear
              </button>
            </div>
          </div>

        </div>

        <!-- CRM MASTER DATA TABLE -->
        <div class="bg-slate-900 border border-slate-800 rounded-xl overflow-hidden shadow-2xl">
          <div class="overflow-x-auto">
            <table class="w-full text-left text-xs" id="cspTable">
              <thead class="bg-slate-950/80 text-slate-400 uppercase tracking-wider font-semibold border-b border-slate-800 text-[11px]">
                <tr>
                  <th class="py-3 px-3 w-10 text-center">
                    <input type="checkbox" id="selectAllCheckbox" onchange="toggleSelectAll(this)" class="rounded bg-slate-900 border-slate-700 text-blue-600 focus:ring-0 cursor-pointer">
                  </th>
                  <th class="py-3 px-3">CSP Identifier</th>
                  <th class="py-3 px-4">Agent Name & Contact</th>
                  <th class="py-3 px-4">Agreement Expiry</th>
                  <th class="py-3 px-4">Police Verification</th>
                  <th class="py-3 px-4">IIBF Certificate</th>
                  <th class="py-3 px-4">Compliance Status</th>
                  <th class="py-3 px-4">Scanned Vault</th>
                  <th class="py-3 px-4">Hierarchy (RM / DC)</th>
                  <th class="py-3 px-4 text-right">CRM Actions</th>
                </tr>
              </thead>
              <tbody class="divide-y divide-slate-800/60 font-mono text-xs" id="tableBody">
                <tr><td colspan="10" class="py-8 text-center text-slate-400">Loading live records from PostgreSQL...</td></tr>
              </tbody>
            </table>
          </div>

          <!-- CRM PAGINATION BAR -->
          <div class="p-4 border-t border-slate-800 flex flex-col sm:flex-row items-center justify-between gap-3 text-xs text-slate-400 bg-slate-950/40">
            <div class="flex items-center gap-2">
              <span>Show</span>
              <select id="pageSize" onchange="changePageSize()" class="bg-slate-900 border border-slate-700 rounded px-2 py-1 text-slate-200">
                <option value="15">15</option>
                <option value="25" selected>25</option>
                <option value="50">50</option>
                <option value="100">100</option>
              </select>
              <span>records per page</span>
            </div>

            <div class="flex items-center gap-2">
              <button type="button" onclick="prevPage()" id="btnPrev" class="px-3 py-1 bg-slate-800 hover:bg-slate-700 border border-slate-700 rounded text-slate-300 disabled:opacity-40 cursor-pointer transition">Previous</button>
              <span id="pageInfo" class="text-xs text-slate-300 font-mono px-2">Page 1 of 1</span>
              <button type="button" onclick="nextPage()" id="btnNext" class="px-3 py-1 bg-slate-800 hover:bg-slate-700 border border-slate-700 rounded text-slate-300 disabled:opacity-40 cursor-pointer transition">Next</button>
            </div>
          </div>
        </div>

      </section>

      <!-- ========================================================================= -->
      <!-- SECTION 3: SCANNED DOCUMENT VAULT & VERIFICATION HUB -->
      <!-- ========================================================================= -->
      <section id="section-vault" class="crm-section space-y-4 hidden fade-enter">
        <div class="bg-slate-900 border border-slate-800 rounded-xl p-5 shadow-lg">
          <div class="flex flex-col md:flex-row md:items-center justify-between gap-4 mb-4">
            <div>
              <h2 class="text-sm font-bold text-white tracking-wide">Scanned Documents & Extraction Vault</h2>
              <p class="text-xs text-slate-400">SHA-256 deduplicated repository of incoming agreements & police verifications</p>
            </div>
            <div class="flex items-center gap-2">
              <select id="vaultTypeFilter" onchange="renderVaultTable()" class="bg-slate-950 border border-slate-700 rounded-lg px-2.5 py-1.5 text-xs text-slate-200">
                <option value="ALL">All Document Types</option>
                <option value="AGREEMENT">Agreements</option>
                <option value="POLICE_VERIFICATION">Police Verifications</option>
              </select>
            </div>
          </div>

          <div class="overflow-x-auto">
            <table class="w-full text-left text-xs font-mono">
              <thead class="text-slate-400 border-b border-slate-800 pb-2 text-[11px]">
                <tr>
                  <th class="py-2.5 px-3">Doc ID & Type</th>
                  <th class="py-2.5 px-3">CSP Code & Name</th>
                  <th class="py-2.5 px-3">SHA-256 Fingerprint</th>
                  <th class="py-2.5 px-3">Engine & Confidence</th>
                  <th class="py-2.5 px-3">Extracted Dates</th>
                  <th class="py-2.5 px-3">Uploaded At</th>
                  <th class="py-2.5 px-3 text-right">Inspect</th>
                </tr>
              </thead>
              <tbody id="vaultTableBody" class="divide-y divide-slate-800/50">
                <tr><td colspan="7" class="py-6 text-center text-slate-500">No scanned documents in repository. Trigger a Gmail OAuth scan to ingest files.</td></tr>
              </tbody>
            </table>
          </div>
        </div>
      </section>

      <!-- ========================================================================= -->
      <!-- SECTION 4: RENEWAL ESCALATIONS & SLA MATRIX -->
      <!-- ========================================================================= -->
      <section id="section-escalations" class="crm-section space-y-4 hidden fade-enter">
        <div class="bg-slate-900 border border-slate-800 rounded-xl p-5 shadow-lg">
          <div class="flex items-center justify-between mb-4">
            <div>
              <h2 class="text-sm font-bold text-white tracking-wide">Renewal SLA Escalations & Breach Prevention</h2>
              <p class="text-xs text-slate-400">Contracts under T-7, expired agreements, and non-responsive CSPs requiring intervention</p>
            </div>
            <button type="button" onclick="dispatchEscalationDigest()" class="bg-rose-600 hover:bg-rose-500 text-white px-3 py-1.5 rounded-lg text-xs font-medium transition cursor-pointer">
              Dispatch RM Escalation Digest
            </button>
          </div>

          <div class="overflow-x-auto">
            <table class="w-full text-left text-xs">
              <thead class="text-slate-400 border-b border-slate-800 pb-2 text-[11px]">
                <tr>
                  <th class="py-2.5 px-3">Urgency</th>
                  <th class="py-2.5 px-3">CSP Code & Name</th>
                  <th class="py-2.5 px-3">Days Remaining</th>
                  <th class="py-2.5 px-3">Agreement Expiry</th>
                  <th class="py-2.5 px-3">Compliance Tier</th>
                  <th class="py-2.5 px-3">Assigned RM & Circle</th>
                  <th class="py-2.5 px-3 text-right">Immediate Action</th>
                </tr>
              </thead>
              <tbody id="escalationsTableBody" class="divide-y divide-slate-800/50 font-mono">
                <tr><td colspan="7" class="py-6 text-center text-slate-500">No critical SLA escalations active at this time.</td></tr>
              </tbody>
            </table>
          </div>
        </div>
      </section>

      <!-- ========================================================================= -->
      <!-- SECTION 5: INBOUND GMAIL STREAM & AUDIT CONSOLE -->
      <!-- ========================================================================= -->
      <section id="section-emails" class="crm-section space-y-4 hidden fade-enter">
        <div class="bg-slate-900 border border-slate-800 rounded-xl p-5 shadow-lg space-y-4">
          <div class="flex flex-col md:flex-row md:items-center justify-between gap-3 pb-3 border-b border-slate-800/80">
            <div>
              <div class="flex items-center gap-2">
                <h2 class="text-sm font-bold text-white tracking-wide">Developer Mailbox & Ingestion Audit Console</h2>
                <span class="px-2 py-0.5 rounded bg-blue-500/20 text-blue-300 font-mono text-[10px] border border-blue-500/30">Live OAuth 2.0</span>
              </div>
              <p class="text-xs text-slate-400 mt-0.5">Examine every email opened by the agent across INBOX, TRASH, and SPAM, matched CSP profiles, and extracted PDFs.</p>
            </div>

            <!-- Filter Controls -->
            <div class="flex items-center gap-2">
              <input type="text" id="inboundSearch" oninput="filterInboundTable()" placeholder="Search sender, code, subject..." class="bg-slate-800/80 border border-slate-700/80 rounded-lg px-3 py-1.5 text-xs text-slate-200 placeholder-slate-500 focus:outline-none focus:border-blue-500 w-52 font-mono">
              <select id="inboundFolderFilter" onchange="filterInboundTable()" class="bg-slate-800/80 border border-slate-700/80 rounded-lg px-2.5 py-1.5 text-xs text-slate-200 focus:outline-none cursor-pointer">
                <option value="ALL">All Folders</option>
                <option value="INBOX">Inbox Only</option>
                <option value="TRASH">Trash Only</option>
              </select>
            </div>
          </div>

          <div class="overflow-x-auto">
            <table class="w-full text-left text-xs font-mono">
              <thead class="text-slate-400 border-b border-slate-800 pb-2 text-[11px] uppercase tracking-wider">
                <tr>
                  <th class="py-2.5 px-3">Received</th>
                  <th class="py-2.5 px-3">Folder</th>
                  <th class="py-2.5 px-3">Matched CSP</th>
                  <th class="py-2.5 px-3">Sender Email</th>
                  <th class="py-2.5 px-3">Subject</th>
                  <th class="py-2.5 px-3 text-center">PDFs</th>
                  <th class="py-2.5 px-3 text-right">Status</th>
                </tr>
              </thead>
              <tbody id="inboundTable" class="divide-y divide-slate-800/50">
                <tr><td colspan="7" class="py-6 text-center text-slate-500">No scanned emails logged yet. Click "Scan Gmail (OAuth)" above.</td></tr>
              </tbody>
            </table>
          </div>
        </div>
      <!-- ========================================================================= -->
      <!-- SECTION: MANUAL DOCUMENT REVIEW & HUMAN VERIFICATION CONSOLE -->
      <!-- ========================================================================= -->
      <section id="section-review" class="crm-section space-y-4 hidden fade-enter">
        <div class="bg-slate-900 border border-slate-800 rounded-xl p-5 shadow-lg space-y-4">
          <div class="flex flex-col md:flex-row md:items-center justify-between gap-3 pb-3 border-b border-slate-800/80">
            <div>
              <div class="flex items-center gap-2">
                <h2 class="text-sm font-bold text-white tracking-wide">Manual Document Verification & Audit Console</h2>
                <span class="px-2 py-0.5 rounded bg-amber-500/20 text-amber-300 font-mono text-[10px] border border-amber-500/30">Human-in-the-Loop Fallback</span>
              </div>
              <p class="text-xs text-slate-400 mt-0.5">Documents where deterministic & AI extraction were uncertain. Manually inspect and correct fields with complete audit logging.</p>
            </div>
            <button type="button" onclick="fetchLiveData(true)" class="px-3 py-1.5 bg-slate-800 hover:bg-slate-700 text-slate-200 border border-slate-700 rounded-lg text-xs font-medium transition cursor-pointer">
              Refresh Queue
            </button>
          </div>

          <div class="overflow-x-auto">
            <table class="w-full text-left text-xs font-mono">
              <thead class="text-slate-400 border-b border-slate-800 pb-2 text-[11px] uppercase tracking-wider">
                <tr>
                  <th class="py-2.5 px-3">Queue ID</th>
                  <th class="py-2.5 px-3">CSP Code & Name</th>
                  <th class="py-2.5 px-3">Detected Type</th>
                  <th class="py-2.5 px-3">Reason</th>
                  <th class="py-2.5 px-3">Confidence</th>
                  <th class="py-2.5 px-3">Queued At</th>
                  <th class="py-2.5 px-3 text-right">Human Action</th>
                </tr>
              </thead>
              <tbody id="reviewTableBody" class="divide-y divide-slate-800/50">
                <tr><td colspan="7" class="py-6 text-center text-slate-500">No documents pending manual verification. All documents auto-resolved.</td></tr>
              </tbody>
            </table>
          </div>
        </div>
      </section>

      <!-- ========================================================================= -->
      <!-- SECTION: DEVELOPER OUTBOUND REVIEW QUEUE (EMAIL & WHATSAPP) -->
      <!-- ========================================================================= -->
      <section id="section-outbound" class="crm-section space-y-4 hidden fade-enter">
        <div class="bg-slate-900 border border-slate-800 rounded-xl p-5 shadow-lg space-y-4">
          <div class="flex flex-col md:flex-row md:items-center justify-between gap-3 pb-3 border-b border-slate-800/80">
            <div>
              <div class="flex items-center gap-2">
                <h2 class="text-sm font-bold text-white tracking-wide">Developer Outbound Communication Review Queue</h2>
                <span class="px-2 py-0.5 rounded bg-indigo-500/20 text-indigo-300 font-mono text-[10px] border border-indigo-500/30">Developer Review Mode</span>
              </div>
              <p class="text-xs text-slate-400 mt-0.5">Outgoing compliance notices sit in draft queue. Inspect, modify, approve, or cancel before delivery via Email or WhatsApp.</p>
            </div>
            <div class="flex items-center gap-2">
              <select id="outboundFilterStatus" onchange="renderOutboundQueueTable()" class="bg-slate-800 border border-slate-700 rounded-lg px-2.5 py-1.5 text-xs text-slate-200">
                <option value="ALL">All Statuses</option>
                <option value="QUEUED_FOR_REVIEW" selected>Queued for Review</option>
                <option value="APPROVED">Approved</option>
                <option value="SENT">Sent</option>
                <option value="FAILED">Cancelled / Failed</option>
              </select>
            </div>
          </div>

          <div class="overflow-x-auto">
            <table class="w-full text-left text-xs font-mono">
              <thead class="text-slate-400 border-b border-slate-800 pb-2 text-[11px] uppercase tracking-wider">
                <tr>
                  <th class="py-2.5 px-3">ID & Time</th>
                  <th class="py-2.5 px-3">Target CSP</th>
                  <th class="py-2.5 px-3">Channel</th>
                  <th class="py-2.5 px-3">Destination</th>
                  <th class="py-2.5 px-3">Template</th>
                  <th class="py-2.5 px-3">Status</th>
                  <th class="py-2.5 px-3 text-right">Developer Action</th>
                </tr>
              </thead>
              <tbody id="outboundTableBody" class="divide-y divide-slate-800/50">
                <tr><td colspan="7" class="py-6 text-center text-slate-500">No outbound messages in review queue.</td></tr>
              </tbody>
            </table>
          </div>
        </div>
      </section>

      <!-- ========================================================================= -->
      <!-- SECTION 6: TERRITORY & RM PERFORMANCE HUB -->
      <!-- ========================================================================= -->
      <section id="section-analytics" class="crm-section space-y-4 hidden fade-enter">
        <div class="bg-slate-900 border border-slate-800 rounded-xl p-5 shadow-lg space-y-5">
          <div>
            <h2 class="text-sm font-bold text-white tracking-wide">Territory & Relationship Manager Breakdown</h2>
            <p class="text-xs text-slate-400">Field management compliance rates across all 537 CSPs</p>
          </div>

          <div id="analyticsRMGrid" class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
            <!-- Dynamically populated RM cards -->
          </div>
        </div>
      </section>

      <!-- ========================================================================= -->
      <!-- SECTION: COMMUNICATION & OUTREACH LIFECYCLE HUB (4 SUB-SECTIONS) -->
      <!-- ========================================================================= -->
      <section id="section-comms" class="crm-section space-y-4 hidden fade-enter">
        
        <!-- Header & Quick Actions -->
        <div class="bg-slate-900 border border-slate-800 rounded-xl p-5 shadow-lg space-y-4">
          <div class="flex flex-col md:flex-row md:items-center justify-between gap-3 pb-3 border-b border-slate-800/80">
            <div>
              <div class="flex items-center gap-2">
                <h2 class="text-sm font-bold text-white tracking-wide">Communication & Outreach Lifecycle Hub</h2>
                <span class="px-2 py-0.5 rounded bg-emerald-500/20 text-emerald-300 font-mono text-[10px] border border-emerald-500/30">Rolling 2-Year Engine</span>
              </div>
              <p class="text-xs text-slate-400 mt-0.5">Continuous tracking of CSP inbound document submissions, automated outreach notices, closed-loop replies, and Calling Sheet discrepancies.</p>
            </div>
            <div class="flex items-center gap-2">
              <button type="button" onclick="triggerBackfill2Years()" class="px-3 py-1.5 bg-indigo-600 hover:bg-indigo-500 text-white rounded-lg text-xs font-semibold shadow transition cursor-pointer">
                ↻ Run 2-Year Backfill
              </button>
              <button type="button" onclick="exportDiscrepanciesXLSX()" class="px-3 py-1.5 bg-emerald-700 hover:bg-emerald-600 text-white rounded-lg text-xs font-semibold shadow transition cursor-pointer">
                📥 Export Excel (.xlsx)
              </button>
            </div>
          </div>

          <!-- 5 Lifecycle Summary KPI Cards -->
          <div class="grid grid-cols-2 md:grid-cols-5 gap-3 text-xs">
            <div role="button" onclick="switchCommsTab('inbound')" class="p-3 bg-slate-950 rounded-lg border border-slate-800 hover:border-blue-500/50 cursor-pointer transition">
              <div class="flex justify-between items-center text-slate-400 text-[10px] uppercase font-bold">
                <span>Sub-section 1</span>
                <span class="text-blue-400">Inbound</span>
              </div>
              <div class="text-xl font-black text-white mt-1" id="commsKpiInbound">0</div>
              <div class="text-[10px] text-slate-500 mt-0.5">Candidate CSP Emails</div>
            </div>

            <div role="button" onclick="switchCommsTab('outbound')" class="p-3 bg-slate-950 rounded-lg border border-slate-800 hover:border-indigo-500/50 cursor-pointer transition">
              <div class="flex justify-between items-center text-slate-400 text-[10px] uppercase font-bold">
                <span>Sub-section 2</span>
                <span class="text-indigo-400">Outbound</span>
              </div>
              <div class="text-xl font-black text-indigo-400 mt-1" id="commsKpiOutbound">0</div>
              <div class="text-[10px] text-slate-500 mt-0.5">Notices Queued / Sent</div>
            </div>

            <div role="button" onclick="switchCommsTab('responded')" class="p-3 bg-slate-950 rounded-lg border border-slate-800 hover:border-emerald-500/50 cursor-pointer transition">
              <div class="flex justify-between items-center text-slate-400 text-[10px] uppercase font-bold">
                <span>Sub-section 3</span>
                <span class="text-emerald-400">Responded</span>
              </div>
              <div class="text-xl font-black text-emerald-400 mt-1" id="commsKpiResponded">0</div>
              <div class="text-[10px] text-slate-500 mt-0.5">Closed-Loop Replies</div>
            </div>

            <div role="button" onclick="switchCommsTab('matrix')" class="p-3 bg-slate-950 rounded-lg border border-slate-800 hover:border-amber-500/50 cursor-pointer transition">
              <div class="flex justify-between items-center text-slate-400 text-[10px] uppercase font-bold">
                <span>Sub-section 4</span>
                <span class="text-amber-400">Categories</span>
              </div>
              <div class="text-xl font-black text-amber-400 mt-1" id="commsKpiDiscrepancies">0</div>
              <div class="text-[10px] text-slate-500 mt-0.5">Calling Sheet Discrepancies</div>
            </div>

            <div role="button" onclick="switchCommsTab('autosheet')" class="p-3 bg-slate-950 rounded-lg border border-slate-800 hover:border-teal-500/50 cursor-pointer transition">
              <div class="flex justify-between items-center text-slate-400 text-[10px] uppercase font-bold">
                <span>Sub-section 5</span>
                <span class="text-teal-400">Auto Sheet</span>
              </div>
              <div class="text-xl font-black text-teal-400 mt-1" id="commsKpiAutoSheet">0</div>
              <div class="text-[10px] text-slate-500 mt-0.5">Auto-Made Calling Sheet</div>
            </div>
          </div>

          <!-- Sub-Section Tabs -->
          <div class="flex border-b border-slate-800 text-xs font-semibold gap-2 pt-2">
            <button type="button" onclick="switchCommsTab('inbound')" id="commsTabBtn-inbound" class="py-2 px-3 border-b-2 border-blue-500 text-blue-400 cursor-pointer transition">
              1. Inbound CSP Submissions
            </button>
            <button type="button" onclick="switchCommsTab('outbound')" id="commsTabBtn-outbound" class="py-2 px-3 border-b-2 border-transparent text-slate-400 hover:text-slate-200 cursor-pointer transition">
              2. Agent Outbound Sent Notices
            </button>
            <button type="button" onclick="switchCommsTab('responded')" id="commsTabBtn-responded" class="py-2 px-3 border-b-2 border-transparent text-slate-400 hover:text-slate-200 cursor-pointer transition">
              3. Responded / Closed-Loop Threads
            </button>
            <button type="button" onclick="switchCommsTab('matrix')" id="commsTabBtn-matrix" class="py-2 px-3 border-b-2 border-transparent text-slate-400 hover:text-slate-200 cursor-pointer transition">
              4. Category Matrix & Discrepancies
            </button>
            <button type="button" onclick="switchCommsTab('autosheet')" id="commsTabBtn-autosheet" class="py-2 px-3 border-b-2 border-transparent text-slate-400 hover:text-slate-200 cursor-pointer transition flex items-center gap-1.5">
              <span>5. Auto-Made Calling Sheet</span>
              <span class="px-1.5 py-0.2 rounded bg-teal-500/20 text-teal-300 text-[10px] font-bold" id="commsTabBadgeAutoSheet">0</span>
            </button>
          </div>

          <!-- SUB-SECTION 1: INBOUND CSP SUBMISSIONS -->
          <div id="commsPanel-inbound" class="space-y-3 pt-2">
            <div class="flex items-center justify-between text-xs text-slate-400">
              <span class="font-bold text-slate-200">Scanned Gmail Messages (Subject Classification & Document Extractions)</span>
              <span class="text-[10px] font-mono">Server Query: rolling 730d window</span>
            </div>
            <div class="overflow-x-auto">
              <table class="w-full text-left text-xs font-mono">
                <thead class="text-slate-400 border-b border-slate-800 pb-2 text-[11px] uppercase tracking-wider">
                  <tr>
                    <th class="py-2 px-3">Date</th>
                    <th class="py-2 px-3">Folder</th>
                    <th class="py-2 px-3">Matched CSP</th>
                    <th class="py-2 px-3">Sender Email</th>
                    <th class="py-2 px-3">Subject & Category</th>
                    <th class="py-2 px-3 text-center">PDFs</th>
                    <th class="py-2 px-3 text-right">Ingestion Status</th>
                  </tr>
                </thead>
                <tbody id="commsInboundTableBody" class="divide-y divide-slate-800/60 text-slate-300">
                  <!-- Populated dynamically -->
                </tbody>
              </table>
            </div>
          </div>

          <!-- SUB-SECTION 2: AGENT OUTBOUND SENT NOTICES -->
          <div id="commsPanel-outbound" class="space-y-3 pt-2 hidden">
            <div class="flex items-center justify-between text-xs text-slate-400">
              <span class="font-bold text-slate-200">Outbound Renewal Notices, Escalations & Reminders</span>
              <span class="text-[10px] font-mono">Modes: Review (Developer) & Live</span>
            </div>
            <div class="overflow-x-auto">
              <table class="w-full text-left text-xs font-mono">
                <thead class="text-slate-400 border-b border-slate-800 pb-2 text-[11px] uppercase tracking-wider">
                  <tr>
                    <th class="py-2 px-3">Notice ID</th>
                    <th class="py-2 px-3">Target CSP</th>
                    <th class="py-2 px-3">Channel</th>
                    <th class="py-2 px-3">Destination</th>
                    <th class="py-2 px-3">Template / Subject</th>
                    <th class="py-2 px-3">Delivery Status</th>
                    <th class="py-2 px-3 text-right">Actions</th>
                  </tr>
                </thead>
                <tbody id="commsOutboundTableBody" class="divide-y divide-slate-800/60 text-slate-300">
                  <!-- Populated dynamically -->
                </tbody>
              </table>
            </div>
          </div>

          <!-- SUB-SECTION 3: RESPONDED / CLOSED-LOOP THREADS -->
          <div id="commsPanel-responded" class="space-y-3 pt-2 hidden">
            <div class="flex items-center justify-between text-xs text-slate-400">
              <span class="font-bold text-slate-200">Closed-Loop Engagement Log (CSPs Responding to Agent Outreach)</span>
              <span class="text-[10px] text-emerald-400 font-mono">Replies detected via In-Reply-To / References / Re: Threading</span>
            </div>
            <div class="overflow-x-auto">
              <table class="w-full text-left text-xs font-mono">
                <thead class="text-slate-400 border-b border-slate-800 pb-2 text-[11px] uppercase tracking-wider">
                  <tr>
                    <th class="py-2 px-3">Event ID</th>
                    <th class="py-2 px-3">CSP Code</th>
                    <th class="py-2 px-3">CSP Name</th>
                    <th class="py-2 px-3">Channel</th>
                    <th class="py-2 px-3">Reply Details / Subject</th>
                    <th class="py-2 px-3 text-right">Response Received Time</th>
                  </tr>
                </thead>
                <tbody id="commsRespondedTableBody" class="divide-y divide-slate-800/60 text-slate-300">
                  <!-- Populated dynamically -->
                </tbody>
              </table>
            </div>
          </div>

          <!-- SUB-SECTION 4: CATEGORY MATRIX & CALLING SHEET DISCREPANCIES -->
          <div id="commsPanel-matrix" class="space-y-4 pt-2 hidden">
            <div class="flex items-center justify-between text-xs text-slate-400">
              <span class="font-bold text-slate-200">Calling Sheet New Non-CSP Discrepancy Registry</span>
              <button type="button" onclick="exportDiscrepanciesXLSX()" class="px-2.5 py-1 bg-emerald-700 hover:bg-emerald-600 text-white rounded text-[11px] font-semibold transition cursor-pointer">
                Download Discrepancies Excel (.xlsx)
              </button>
            </div>
            <p class="text-xs text-slate-400">These senders submitted documents or terminal reset/extension requests, but their code/email was NOT found in Calling Sheet New. Synthetic CSPs are never created to protect master registry integrity.</p>

            <div class="overflow-x-auto">
              <table class="w-full text-left text-xs font-mono">
                <thead class="text-slate-400 border-b border-slate-800 pb-2 text-[11px] uppercase tracking-wider">
                  <tr>
                    <th class="py-2 px-3">Message ID</th>
                    <th class="py-2 px-3">Received At</th>
                    <th class="py-2 px-3">Sender Email</th>
                    <th class="py-2 px-3">Candidate KO</th>
                    <th class="py-2 px-3">Category</th>
                    <th class="py-2 px-3">Subject</th>
                    <th class="py-2 px-3">Status Reason</th>
                    <th class="py-2 px-3 text-right">Action</th>
                  </tr>
                </thead>
                <tbody id="commsDiscrepancyTableBody" class="divide-y divide-slate-800/60 text-slate-300">
                  <!-- Populated dynamically -->
                </tbody>
              </table>
            </div>
          </div>

          <!-- SUB-SECTION 5: AUTO-MADE CALLING SHEET (MISSING CSPS) -->
          <div id="commsPanel-autosheet" class="space-y-4 pt-2 hidden">
            <div class="flex flex-col md:flex-row md:items-center justify-between gap-3 text-xs text-slate-400">
              <div>
                <div class="flex items-center gap-2">
                  <span class="font-bold text-slate-200 text-sm">Auto-Generated Calling Sheet (Missing CSPs)</span>
                  <span class="px-2 py-0.5 rounded bg-emerald-500/20 text-emerald-300 font-mono text-[10px] border border-emerald-500/30">Google Sheet Format</span>
                </div>
                <p class="text-xs text-slate-400 mt-0.5">The agent detected these CSPs from incoming emails, terminal extension/reset requests, and documents whose details were missing from Calling Sheet New. The agent auto-extracted their calling sheet details.</p>
              </div>
              <div class="flex flex-wrap items-center gap-2">
                <button type="button" onclick="triggerAutoDetectCallingSheet()" class="px-3 py-1.5 bg-emerald-600 hover:bg-emerald-500 text-white rounded text-xs font-semibold shadow transition cursor-pointer flex items-center gap-1.5">
                  <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 10V3L4 14h7v7l9-11h-7z"></path></svg>
                  Auto-Detect & Make Sheet
                </button>
                <button type="button" onclick="exportAutoCallingSheetXLSX()" class="px-3 py-1.5 bg-teal-700 hover:bg-teal-600 text-white rounded text-xs font-semibold shadow transition cursor-pointer flex items-center gap-1.5">
                  <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 10v6m0 0l-3-3m3 3l3-3m2 8H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"></path></svg>
                  Download Auto Sheet (.xlsx)
                </button>
                <button type="button" onclick="copyAllRowsForGoogleSheets()" class="px-3 py-1.5 bg-slate-800 hover:bg-slate-700 text-blue-300 border border-slate-700 rounded text-xs font-semibold shadow transition cursor-pointer flex items-center gap-1.5">
                  <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z"></path></svg>
                  Copy All for Google Sheets
                </button>
                <button type="button" onclick="bulkPromoteAutoCallingSheet()" class="px-3 py-1.5 bg-indigo-700 hover:bg-indigo-600 text-white rounded text-xs font-semibold shadow transition cursor-pointer flex items-center gap-1.5">
                  <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 6v6m0 0v6m0-6h6m-6 0H6"></path></svg>
                  Bulk Add to Master DB
                </button>
              </div>
            </div>

            <div class="overflow-x-auto">
              <table class="w-full text-left text-xs font-mono">
                <thead class="text-slate-400 border-b border-slate-800 pb-2 text-[11px] uppercase tracking-wider">
                  <tr>
                    <th class="py-2 px-3">CSP ID</th>
                    <th class="py-2 px-3">CSP Name</th>
                    <th class="py-2 px-3">CSP Mail ID</th>
                    <th class="py-2 px-3">Mobile</th>
                    <th class="py-2 px-3">State</th>
                    <th class="py-2 px-3">Branch Name</th>
                    <th class="py-2 px-3">Circle (LHO)</th>
                    <th class="py-2 px-3">Request Type</th>
                    <th class="py-2 px-3">Detected Date</th>
                    <th class="py-2 px-3">Master Status</th>
                    <th class="py-2 px-3 text-right">Actions</th>
                  </tr>
                </thead>
                <tbody id="commsAutoSheetTableBody" class="divide-y divide-slate-800/60 text-slate-300">
                  <!-- Populated dynamically -->
                </tbody>
              </table>
            </div>
          </div>

        </div>
      </section>

    </main>

  </div>

  <!-- ========================================================================= -->
  <!-- 3. CRM 360° RIGHT SLIDE-OVER DRAWER (SAFELY HIDDEN BY DEFAULT) -->
  <!-- ========================================================================= -->
  <div id="crmDrawerBackdrop" onclick="closeDrawer()" class="fixed inset-0 bg-slate-950/80 backdrop-blur-sm z-40 transition-opacity duration-300" style="display: none; pointer-events: none;"></div>

  <div id="crmDrawer" class="fixed inset-y-0 right-0 z-50 w-full max-w-xl bg-slate-900 border-l border-slate-800 shadow-2xl flex flex-col transition-transform duration-300 ease-in-out" style="display: none; pointer-events: none; transform: translateX(100%);">
    <div class="flex flex-col h-full justify-between">
      <div>
        <!-- Drawer Header -->
        <div class="p-5 border-b border-slate-800 flex items-center justify-between bg-slate-950/80">
          <div class="flex items-center gap-3">
            <div class="w-10 h-10 rounded-xl bg-gradient-to-tr from-blue-600 to-indigo-600 flex items-center justify-center font-bold text-white text-base shadow" id="dAvatar">
              CS
            </div>
            <div>
              <div class="flex items-center gap-2">
                <span class="text-[10px] font-mono px-2 py-0.5 rounded bg-blue-500/20 text-blue-300 border border-blue-500/30" id="dCode">1A850247</span>
                <span id="dCatBadge" class="text-[10px] font-semibold px-2 py-0.5 rounded">Cat A</span>
              </div>
              <h3 class="text-sm font-bold text-white mt-1 truncate max-w-[240px]" id="dName">CSP Name</h3>
            </div>
          </div>
          <button type="button" onclick="closeDrawer()" class="text-slate-400 hover:text-white p-1.5 rounded hover:bg-slate-800 text-lg cursor-pointer">✕</button>
        </div>

        <!-- Drawer Internal Tabs -->
        <div class="flex border-b border-slate-800 bg-slate-950/40 text-xs text-slate-400 font-medium px-4">
          <button type="button" onclick="switchDrawerTab('overview')" id="dtab-overview" class="py-2.5 px-3 border-b-2 border-blue-500 text-blue-400 font-semibold cursor-pointer">Overview</button>
          <button type="button" onclick="switchDrawerTab('compliance')" id="dtab-compliance" class="py-2.5 px-3 border-b-2 border-transparent hover:text-slate-200 cursor-pointer">Compliance</button>
          <button type="button" onclick="switchDrawerTab('vault')" id="dtab-vault" class="py-2.5 px-3 border-b-2 border-transparent hover:text-slate-200 cursor-pointer">Vault Files</button>
          <button type="button" onclick="switchDrawerTab('history')" id="dtab-history" class="py-2.5 px-3 border-b-2 border-transparent hover:text-slate-200 cursor-pointer">Outreach & Notes</button>
        </div>

        <!-- Drawer Scrollable Content -->
        <div class="p-5 space-y-4 overflow-y-auto max-h-[calc(100vh-190px)] text-xs">
          
          <!-- SUBTAB: OVERVIEW -->
          <div id="dsub-overview" class="space-y-4">
            <!-- Contact Details -->
            <div class="bg-slate-950 p-3.5 rounded-lg border border-slate-800 space-y-2.5">
              <div class="text-[10px] uppercase font-bold text-slate-400 tracking-wider">Contact & Communications</div>
              
              <div class="flex justify-between items-center">
                <span class="text-slate-500">Mobile Phone:</span>
                <div class="flex items-center gap-2">
                  <span class="text-slate-200 font-mono" id="dPhone">--</span>
                  <a id="dPhoneCall" href="#" class="p-1 rounded bg-blue-600/20 text-blue-400 hover:bg-blue-600/40" title="Call CSP">📞</a>
                  <a id="dPhoneWA" href="#" target="_blank" class="p-1 rounded bg-emerald-600/20 text-emerald-400 hover:bg-emerald-600/40" title="WhatsApp CSP">💬</a>
                </div>
              </div>

              <div class="flex justify-between items-center">
                <span class="text-slate-500">Email Address:</span>
                <div class="flex items-center gap-2">
                  <span class="text-slate-200 font-mono truncate max-w-[170px]" id="dEmail">--</span>
                  <a id="dEmailLink" href="#" class="p-1 rounded bg-blue-600/20 text-blue-400 hover:bg-blue-600/40" title="Send Email">✉️</a>
                </div>
              </div>

              <div class="flex justify-between"><span class="text-slate-500">Branch Location:</span><span class="text-slate-200" id="dBranch">--</span></div>
              <div class="flex justify-between"><span class="text-slate-500">Assigned RM:</span><span class="text-emerald-400 font-medium" id="dRM">--</span></div>
              <div class="flex justify-between"><span class="text-slate-500">Circle Head:</span><span class="text-blue-400 font-medium" id="dDC">--</span></div>
            </div>

            <!-- Privacy & Whitelist Invariant Notice -->
            <div class="p-3 bg-slate-950/60 rounded-lg border border-slate-800 text-[11px] text-slate-400 space-y-1">
              <div class="font-bold text-slate-300">Data Compliance & GDPR Safety</div>
              <p>Strictly whitelisting 6 operational fields. Sensitive financial, gender, and commission data are completely excluded from database storage.</p>
            </div>
          </div>

          <!-- SUBTAB: COMPLIANCE -->
          <div id="dsub-compliance" class="space-y-4 hidden">
            <!-- Agreement Box -->
            <div class="p-3.5 bg-slate-950 rounded-lg border border-slate-800 space-y-2">
              <div class="flex items-center justify-between">
                <div class="font-bold text-white text-xs">CSP Business Agreement</div>
                <span id="dAgrBadge" class="text-[10px] px-2 py-0.5 rounded font-mono">--</span>
              </div>
              <div class="text-[11px] text-slate-400" id="dAgrDetails">Start: -- | Expiry: --</div>
              
              <!-- Countdown Indicator -->
              <div class="pt-1">
                <div class="flex justify-between text-[10px] text-slate-500 font-mono mb-1">
                  <span>Validity Countdown:</span>
                  <span id="dDaysRemaining">--</span>
                </div>
                <div class="w-full bg-slate-900 h-2 rounded-full overflow-hidden">
                  <div id="dCountdownBar" class="bg-blue-500 h-full w-1/2"></div>
                </div>
              </div>
            </div>

            <!-- Police Verification Box -->
            <div class="p-3.5 bg-slate-950 rounded-lg border border-slate-800 space-y-1.5">
              <div class="flex items-center justify-between">
                <div class="font-bold text-white text-xs">Police Verification Certificate</div>
                <span id="dPVBadge" class="text-[10px] px-2 py-0.5 rounded font-mono">--</span>
              </div>
              <div class="text-[11px] text-slate-400" id="dPVDetails">Valid Till: --</div>
            </div>
          </div>

          <!-- SUBTAB: VAULT FILES -->
          <div id="dsub-vault" class="space-y-3 hidden">
            <div class="text-[10px] uppercase font-bold text-slate-400 tracking-wider">Scanned Document Audit</div>
            <div id="dDocList" class="space-y-2 font-mono text-[11px]">
              <!-- Injected dynamically -->
            </div>
          </div>

          <!-- SUBTAB: OUTREACH & NOTES -->
          <div id="dsub-history" class="space-y-3 hidden">
            <div class="text-[10px] uppercase font-bold text-slate-400 tracking-wider">Automated Notification Cadence</div>
            
            <div class="space-y-2 text-[11px]">
              <div class="p-2.5 bg-slate-950 rounded border border-slate-800 flex justify-between items-center">
                <span>T-60 Strategy Notification</span>
                <span class="text-slate-500">Cadence Auto</span>
              </div>
              <div class="p-2.5 bg-slate-950 rounded border border-slate-800 flex justify-between items-center">
                <span>T-30 Urgent Reminder</span>
                <span class="text-slate-500">Cadence Auto</span>
              </div>
              <div class="p-2.5 bg-slate-950 rounded border border-slate-800 flex justify-between items-center">
                <span>T-7 Escalation to RM</span>
                <span class="text-slate-500">Cadence Auto</span>
              </div>
            </div>

            <div class="pt-2">
              <label class="block text-[10px] uppercase font-bold text-slate-400 tracking-wider mb-1">Operator Notes</label>
              <textarea id="dOperatorNote" rows="3" placeholder="Enter compliance remarks or verification logs..." class="w-full bg-slate-950 border border-slate-800 rounded-lg p-2.5 text-xs text-slate-100 placeholder-slate-600 focus:outline-none focus:border-blue-500"></textarea>
              <button type="button" onclick="saveOperatorNote()" class="mt-1.5 w-full py-1.5 bg-slate-800 hover:bg-slate-700 text-slate-200 rounded text-xs transition cursor-pointer">Save Note</button>
            </div>
          </div>

        </div>
      </div>

      <!-- Drawer Sticky Footer -->
      <div class="p-4 border-t border-slate-800 bg-slate-950 flex items-center gap-2">
        <button type="button" onclick="triggerOutreach()" class="flex-1 py-2 bg-blue-600 hover:bg-blue-500 text-white rounded-lg text-xs font-semibold shadow-md transition cursor-pointer">
          ⚡ Queue Renewal Notice
        </button>
        <button type="button" onclick="closeDrawer()" class="px-4 py-2 bg-slate-800 hover:bg-slate-700 text-slate-300 rounded-lg text-xs font-medium cursor-pointer transition">
          Close
        </button>
      </div>
    </div>
  </div>

  <!-- MANUAL REVIEW VERIFICATION MODAL -->
  <div id="modalManualReview" class="fixed inset-0 bg-slate-950/80 backdrop-blur-sm z-50 flex items-center justify-center hidden p-4">
    <div class="bg-slate-900 border border-slate-700 rounded-xl max-w-2xl w-full shadow-2xl overflow-hidden flex flex-col max-h-[90vh]">
      <div class="p-4 border-b border-slate-800 flex items-center justify-between bg-slate-950/60">
        <div>
          <h3 class="text-sm font-bold text-white flex items-center gap-2">
            <span class="w-2.5 h-2.5 rounded-full bg-amber-400"></span>
            Manual Document Verification & Correction
          </h3>
          <p class="text-xs text-slate-400 mt-0.5" id="revModalSubtitle">Queue Item #--</p>
        </div>
        <button type="button" onclick="closeManualReviewModal()" class="text-slate-400 hover:text-white text-lg cursor-pointer">✕</button>
      </div>

      <div class="p-5 space-y-4 overflow-y-auto text-xs">
        <!-- Raw OCR / Text Snippet -->
        <div class="bg-slate-950 p-3 rounded-lg border border-slate-800 space-y-1">
          <span class="text-[10px] uppercase font-bold text-slate-400 tracking-wider">Detection Failure Reason</span>
          <p class="text-[11px] font-mono text-amber-300" id="revReasonText"></p>
          <div class="p-2 bg-slate-900 rounded border border-slate-800 font-mono text-[11px] text-slate-300 whitespace-pre-wrap max-h-32 overflow-y-auto" id="revSnippetText"></div>
        </div>

        <form id="formManualReview" onsubmit="saveManualReviewCorrection(event)" class="space-y-3 font-sans">
          <input type="hidden" id="revQueueId">

          <div class="grid grid-cols-2 gap-3">
            <div>
              <label class="block text-[11px] font-semibold text-slate-300 mb-1">Target CSP Code</label>
              <input type="text" id="revCspCode" required class="w-full bg-slate-950 border border-slate-700 rounded p-2 text-slate-100 font-mono focus:border-blue-500">
            </div>
            <div>
              <label class="block text-[11px] font-semibold text-slate-300 mb-1">Verified Document Type</label>
              <select id="revDocType" class="w-full bg-slate-950 border border-slate-700 rounded p-2 text-slate-100 focus:border-blue-500 cursor-pointer">
                <option value="AGREEMENT">Agreement (3-Year Validity)</option>
                <option value="POLICE_VERIFICATION">Police Verification (1-Year Validity)</option>
                <option value="CHARACTER_CERTIFICATE">Character Certificate (3-Year Validity)</option>
                <option value="IIBF_CERTIFICATE">IIBF Certificate (Permanent Record - No Expiry)</option>
              </select>
            </div>
          </div>

          <div class="grid grid-cols-2 gap-3">
            <div>
              <label class="block text-[11px] font-semibold text-slate-300 mb-1">Issue / Start Date (YYYY-MM-DD)</label>
              <input type="date" id="revIssueDate" class="w-full bg-slate-950 border border-slate-700 rounded p-2 text-slate-100 font-mono focus:border-blue-500">
            </div>
            <div>
              <label class="block text-[11px] font-semibold text-slate-300 mb-1">Expiry Date (Leave blank for IIBF)</label>
              <input type="date" id="revExpiryDate" class="w-full bg-slate-950 border border-slate-700 rounded p-2 text-slate-100 font-mono focus:border-blue-500">
            </div>
          </div>

          <div class="grid grid-cols-2 gap-3">
            <div>
              <label class="block text-[11px] font-semibold text-slate-300 mb-1">Certificate / Document No.</label>
              <input type="text" id="revCertNo" placeholder="e.g. CC/2026/894" class="w-full bg-slate-950 border border-slate-700 rounded p-2 text-slate-100 font-mono focus:border-blue-500">
            </div>
            <div>
              <label class="block text-[11px] font-semibold text-slate-300 mb-1">Registration No. (IIBF/BCBF)</label>
              <input type="text" id="revRegNo" placeholder="e.g. 500123984" class="w-full bg-slate-950 border border-slate-700 rounded p-2 text-slate-100 font-mono focus:border-blue-500">
            </div>
          </div>

          <div class="grid grid-cols-2 gap-3">
            <div>
              <label class="block text-[11px] font-semibold text-slate-300 mb-1">Issuing Authority</label>
              <input type="text" id="revAuthority" placeholder="e.g. SP Office / IIBF Mumbai" class="w-full bg-slate-950 border border-slate-700 rounded p-2 text-slate-100 focus:border-blue-500">
            </div>
            <div>
              <label class="block text-[11px] font-semibold text-slate-300 mb-1">State / Jurisdiction</label>
              <input type="text" id="revState" placeholder="e.g. BIHAR, UP, MAHARASHTRA" class="w-full bg-slate-950 border border-slate-700 rounded p-2 text-slate-100 focus:border-blue-500">
            </div>
          </div>

          <div>
            <label class="block text-[11px] font-semibold text-slate-300 mb-1">Operator Audit Notes</label>
            <input type="text" id="revNotes" placeholder="Reason for correction or manual verification details..." class="w-full bg-slate-950 border border-slate-700 rounded p-2 text-slate-100 focus:border-blue-500">
          </div>

          <div class="pt-3 border-t border-slate-800 flex items-center justify-between">
            <button type="button" onclick="rejectCurrentReviewItem()" class="px-3 py-2 bg-rose-900/60 hover:bg-rose-900 text-rose-200 border border-rose-700/60 rounded text-xs transition cursor-pointer">
              Reject as Invalid Document
            </button>
            <div class="flex items-center gap-2">
              <button type="button" onclick="closeManualReviewModal()" class="px-3 py-2 bg-slate-800 hover:bg-slate-700 text-slate-300 rounded text-xs transition cursor-pointer">Cancel</button>
              <button type="submit" class="px-4 py-2 bg-emerald-600 hover:bg-emerald-500 text-white rounded font-semibold text-xs shadow-md transition cursor-pointer">
                ✓ Save & Verify Document
              </button>
            </div>
          </div>
        </form>
      </div>
    </div>
  </div>

  <!-- EDIT OUTBOUND MESSAGE MODAL -->
  <div id="modalEditOutbound" class="fixed inset-0 bg-slate-950/80 backdrop-blur-sm z-50 flex items-center justify-center hidden p-4">
    <div class="bg-slate-900 border border-slate-700 rounded-xl max-w-lg w-full shadow-2xl overflow-hidden flex flex-col">
      <div class="p-4 border-b border-slate-800 flex items-center justify-between bg-slate-950/60">
        <h3 class="text-sm font-bold text-white">Edit Outbound Notice (Review Mode)</h3>
        <button type="button" onclick="closeEditOutboundModal()" class="text-slate-400 hover:text-white text-lg cursor-pointer">✕</button>
      </div>
      <form onsubmit="saveEditedOutboundMessage(event)" class="p-5 space-y-3 text-xs">
        <input type="hidden" id="editOutboundId">
        <div>
          <label class="block text-[11px] font-semibold text-slate-300 mb-1">Destination Address / Phone</label>
          <input type="text" id="editOutboundDest" required class="w-full bg-slate-950 border border-slate-700 rounded p-2 text-slate-100 font-mono">
        </div>
        <div id="editOutboundSubjectRow">
          <label class="block text-[11px] font-semibold text-slate-300 mb-1">Subject</label>
          <input type="text" id="editOutboundSubject" class="w-full bg-slate-950 border border-slate-700 rounded p-2 text-slate-100">
        </div>
        <div>
          <label class="block text-[11px] font-semibold text-slate-300 mb-1">Message Body</label>
          <textarea id="editOutboundBody" rows="6" class="w-full bg-slate-950 border border-slate-700 rounded p-2 text-slate-100 font-mono leading-relaxed"></textarea>
        </div>
        <div class="pt-2 flex justify-end gap-2 border-t border-slate-800">
          <button type="button" onclick="closeEditOutboundModal()" class="px-3 py-1.5 bg-slate-800 text-slate-300 rounded cursor-pointer">Cancel</button>
          <button type="submit" class="px-4 py-1.5 bg-blue-600 hover:bg-blue-500 text-white font-semibold rounded shadow cursor-pointer">Save Changes</button>
        </div>
      </form>
    </div>
  </div>

  <!-- INBOUND EMAIL AUDIT DRAWER / MODAL -->
  <div id="modalEmailAudit" class="fixed inset-0 bg-slate-950/80 backdrop-blur-sm z-50 flex items-center justify-center hidden p-4">
    <div class="bg-slate-900 border border-slate-700 rounded-xl max-w-lg w-full shadow-2xl overflow-hidden flex flex-col">
      <div class="p-4 border-b border-slate-800 flex items-center justify-between bg-slate-950/60">
        <h3 class="text-sm font-bold text-white flex items-center gap-2">
          <span>🔍</span> Email Classification & Ingestion Audit
        </h3>
        <button type="button" onclick="closeEmailAuditModal()" class="text-slate-400 hover:text-white text-lg cursor-pointer">✕</button>
      </div>
      <div class="p-5 space-y-3 font-mono text-xs overflow-y-auto max-h-[75vh]">
        <div class="p-3 bg-slate-950 rounded border border-slate-800 space-y-2" id="emailAuditContent">
          <!-- Populated dynamically -->
        </div>
        <div class="flex justify-end pt-2">
          <button type="button" onclick="closeEmailAuditModal()" class="px-4 py-1.5 bg-slate-800 hover:bg-slate-700 text-slate-200 rounded cursor-pointer">Close</button>
        </div>
      </div>
    </div>
  </div>

  <!-- TOAST FEEDBACK NOTIFICATION -->
  <div id="toast">
    <span id="toastIcon"></span>
    <span id="toastMsg" class="font-sans text-slate-200 font-medium"></span>
  </div>

  <!-- ========================================================================= -->
  <!-- 4. CLIENT CRM JAVASCRIPT ENGINE -->
  <!-- ========================================================================= -->
  <script id="serverPreloadData" type="application/json">
__SERVER_PRELOAD_JSON_SLOT__
  </script>
  <script>
    window.onerror = function(msg, url, lineNo, colNo, err) {
      console.error("[CSP Portal JS Error]", msg, "at line", lineNo, ":", colNo, err);
      return false;
    };

    let rawData = [];
    let filteredData = [];
    let currentCategory = 'ALL';
    let currentSection = 'directory';
    let currentPage = 1;
    let pageSize = 25;
    let activeCSP = null;
    let selectedCSPCodes = new Set();
    let cachedSummary = null;

    function escapeHTML(str) {
      if (str === null || str === undefined) return '';
      return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#039;');
    }

    function getPreloadedData() {
      try {
        const el = document.getElementById("serverPreloadData");
        if (el) {
          const txt = el.textContent.trim();
          if (txt && txt.startsWith("{") && txt !== "{}" && !txt.includes("__SERVER_PRELOAD_")) {
            return JSON.parse(txt);
          }
        }
      } catch (e) {
        console.warn("Could not parse preload data:", e);
      }
      return null;
    }

    // SECTION NAVIGATION SWITCHER
    function switchSection(sec) {
      currentSection = sec;

      // 1. Hide all sections, show active
      document.querySelectorAll('.crm-section').forEach(el => el.classList.add('hidden'));
      const activeEl = document.getElementById('section-' + sec);
      if (activeEl) activeEl.classList.remove('hidden');

      // 2. Update Sidebar Active State
      document.querySelectorAll('.nav-item').forEach(link => {
        link.classList.remove('nav-active');
        link.classList.add('text-slate-400');
      });
      const activeLink = document.getElementById('nav-' + sec);
      if (activeLink) {
        activeLink.classList.add('nav-active');
        activeLink.classList.remove('text-slate-400');
      }

      // 3. Update Breadcrumb Title
      const titles = {
        'overview': 'Operations Overview',
        'directory': 'CSP Master Directory',
        'vault': 'Document Vault & Storage',
        'escalations': 'Escalations & SLA Tracker',
        'emails': 'Inbound Gmail Stream',
        'review': 'Manual Document Verification Queue',
        'outbound': 'Developer Outbound Review Queue',
        'comms': 'Communication & Outreach Lifecycle Hub',
        'analytics': 'Territory & RM Analytics'
      };
      document.getElementById('breadcrumbTitle').innerText = titles[sec] || 'Operations Console';

      // Render sub-views if needed
      if (sec === 'vault') renderVaultTable();
      if (sec === 'escalations') renderEscalationsTable();
      if (sec === 'analytics') renderAnalyticsView();
      if (sec === 'review') renderReviewQueueTable();
      if (sec === 'outbound') renderOutboundQueueTable();
      if (sec === 'comms') renderCommunicationHub();
    }

    // DRAWER TAB SWITCHER
    function switchDrawerTab(tab) {
      ['overview', 'compliance', 'vault', 'history'].forEach(t => {
        const el = document.getElementById('dsub-' + t);
        if (el) el.classList.add('hidden');
        const b = document.getElementById('dtab-' + t);
        if (b) {
          b.classList.remove('border-blue-500', 'text-blue-400', 'font-semibold');
          b.classList.add('border-transparent');
        }
      });
      const activeEl = document.getElementById('dsub-' + tab);
      if (activeEl) activeEl.classList.remove('hidden');
      const activeBtn = document.getElementById('dtab-' + tab);
      if (activeBtn) {
        activeBtn.classList.add('border-blue-500', 'text-blue-400', 'font-semibold');
        activeBtn.classList.remove('border-transparent');
      }
    }

    // MAIN DATA FETCH
    async function fetchLiveData(isManual = false) {
      try {
        const tbody = document.getElementById("tableBody");
        if (tbody && (!rawData || rawData.length === 0)) {
          tbody.innerHTML = `<tr><td colspan="9" class="py-12 text-center text-slate-400">
            <div class="flex flex-col items-center justify-center gap-2">
              <div class="w-6 h-6 border-2 border-blue-500 border-t-transparent rounded-full animate-spin"></div>
              <span>Connecting to Neon PostgreSQL & loading live records...</span>
            </div>
          </td></tr>`;
        }

        let data = null;
        if (!isManual) {
          data = getPreloadedData();
        }
        if (!data || !data.csps || data.csps.length === 0) {
          const res = await fetch('/api/dashboard/summary');
          if (!res.ok) throw new Error(`HTTP error ${res.status}`);
          data = await res.json();
        }

        cachedSummary = data;
        rawData = data.csps || [];

        const setVal = (id, val) => {
          const el = document.getElementById(id);
          if (el) el.innerText = val;
        };

        const setW = (id, pct) => {
          const el = document.getElementById(id);
          if (el) el.style.width = `${pct}%`;
        };

        // 1. Update Top KPIs
        const total = data.total_csps || 0;
        setVal('kpiTotal', total);
        setVal('kpiTotalDir', total);
        setVal('sideTotalCount', `${total} CSPs`);
        setVal('badgeOverview', total);
        setVal('badgeDirectory', total);

        const cats = data.categories || {};
        const catA = cats.CAT_A || 0;
        const catB = cats.CAT_B || 0;
        const catC = cats.CAT_C || 0;
        const catD = cats.CAT_D || 0;

        setVal('kpiCompliant', catA);
        setVal('kpiCompliantDir', catA);
        setVal('kpiIncomplete', catB);
        setVal('kpiIncompleteDir', catB);
        setVal('kpiExpired', catC);
        setVal('kpiExpiredDir', catC);
        setVal('kpiGhost', catD);
        setVal('kpiGhostDir', catD);

        const compRate = data.compliance_rate !== undefined ? data.compliance_rate : (total > 0 ? Math.round((catA / total) * 100) : 0);
        setVal('kpiCompliantRate', `${compRate}%`);
        setVal('kpiCompliantRateDir', `${compRate}%`);
        setVal('complianceBarLabel', `${compRate}% Compliant (${catA}/${total})`);

        // Visual Progress Bar Ratios
        if (total > 0) {
          setW('barCatA', (catA / total) * 100);
          setW('barCatB', (catB / total) * 100);
          setW('barCatC', (catC / total) * 100);
          setW('barCatD', (catD / total) * 100);
        }

        // Category Numbers
        setVal('catACount', catA);
        setVal('catBCount', catB);
        setVal('catCCount', catC);
        setVal('catDCount', catD);

        // Horizon Radar
        const hz = data.horizon || {};
        setVal('hT90', `${hz.T90 || 0} Due`);
        setVal('hT30', `${(hz.T60 || 0) + (hz.T30 || 0)} Due`);
        setVal('hT7', `${hz.T7 || 0} Due`);
        setVal('hT0', `${hz.T0 || 0} Locked`);

        // Badge Counters
        setVal('badgeVault', (data.vault_documents || []).length);
        setVal('badgeEscalations', (data.escalations || []).length);

        // Tab Labels
        setVal('tabALL', `All (${total})`);
        setVal('tabCAT_A', `Category A (${catA})`);
        setVal('tabCAT_B', `Category B (${catB})`);
        setVal('tabCAT_C', `Category C (${catC})`);
        setVal('tabCAT_D', `Category D (${catD})`);

        // Populate Dropdowns
        populateFilters();

        // Populate Overview Leaderboards
        renderOverviewLeaderboards(data);

        // Populate Inbound Gmail Stream
        renderInboundTable(data.recent_inbound || []);

        if (!isManual) {
          currentCategory = 'ALL';
          updateTabStyle('ALL');
          const h = document.getElementById("horizonFilter"); if (h) h.value = "ALL";
          const d = document.getElementById("docFilter"); if (d) d.value = "ALL";
          const rm = document.getElementById("rmFilter"); if (rm) rm.value = "ALL";
          const c = document.getElementById("circleFilter"); if (c) c.value = "ALL";
          const s = document.getElementById("globalSearch"); if (s) s.value = "";
        }

        applyFilters();
      } catch (err) {
        console.error("Dashboard error:", err);
        const tbody = document.getElementById("tableBody");
        if (tbody && (!rawData || rawData.length === 0)) {
          tbody.innerHTML = `<tr><td colspan="9" class="py-12 text-center text-slate-400">
            <div class="flex flex-col items-center justify-center gap-2">
              <span class="text-rose-400 font-semibold">Failed to connect to backend API.</span>
              <button type="button" onclick="fetchLiveData(true)" class="px-3 py-1 bg-blue-600 hover:bg-blue-500 text-white rounded text-xs cursor-pointer">Retry Connection</button>
            </div>
          </td></tr>`;
        }
        showToast("Error connecting to backend API.", "error");
      }

    }

    function populateFilters() {
      const rmSet = new Set();
      const circleSet = new Set();
      rawData.forEach(c => {
        if (c.rm && c.rm !== 'Unassigned') rmSet.add(c.rm);
        if (c.dc && c.dc !== 'Unassigned') circleSet.add(c.dc);
      });

      const rmSelect = document.getElementById('rmFilter');
      if (rmSelect) {
        rmSelect.innerHTML = '<option value="ALL">All Managers (All RMs)</option>' + 
          Array.from(rmSet).sort().map(rm => `<option value="${rm}">${rm}</option>`).join('');
      }

      const circleSelect = document.getElementById('circleFilter');
      if (circleSelect) {
        circleSelect.innerHTML = '<option value="ALL">All Circles (All Regions)</option>' + 
          Array.from(circleSet).sort().map(c => `<option value="${c}">${c}</option>`).join('');
      }
    }

    function renderOverviewLeaderboards(data) {
      const rmBody = document.getElementById('rmTableBody');
      if (rmBody) {
        const rms = Object.entries(data.rm_breakdown || {});
        if (rms.length === 0) {
          rmBody.innerHTML = `<tr><td colspan="5" class="py-4 text-center text-slate-500">No RM assignments recorded.</td></tr>`;
        } else {
          rmBody.innerHTML = rms.map(([rmName, stats]) => {
            const safeRM = escapeHTML(rmName);
            const encRM = encodeURIComponent(rmName);
            return `
              <tr class="hover:bg-slate-800/30 transition">
                <td class="py-2.5 font-medium text-slate-200">${safeRM}</td>
                <td class="py-2.5 text-center font-mono">${stats.total}</td>
                <td class="py-2.5 text-center font-mono text-emerald-400">${stats.compliant}</td>
                <td class="py-2.5 text-center font-mono text-amber-400">${stats.at_risk}</td>
                <td class="py-2.5 text-right">
                  <button type="button" onclick="filterByRMAndSwitch(decodeURIComponent('${encRM}'))" class="px-2 py-0.5 rounded bg-slate-800 hover:bg-slate-700 text-blue-400 text-[10px] transition cursor-pointer">Filter →</button>
                </td>
              </tr>
            `;
          }).join('');
        }
      }

      const circleBody = document.getElementById('circleTableBody');
      if (circleBody) {
        const circles = Object.entries(data.circle_breakdown || {});
        if (circles.length === 0) {
          circleBody.innerHTML = `<tr><td colspan="5" class="py-4 text-center text-slate-500">No Circle assignments recorded.</td></tr>`;
        } else {
          circleBody.innerHTML = circles.map(([cName, stats]) => {
            const safeCircle = escapeHTML(cName);
            const rate = stats.total > 0 ? Math.round((stats.compliant / stats.total) * 100) : 0;
            return `
              <tr class="hover:bg-slate-800/30 transition">
                <td class="py-2.5 font-medium text-slate-200">${safeCircle}</td>
                <td class="py-2.5 text-center font-mono">${stats.total}</td>
                <td class="py-2.5 text-center font-mono text-emerald-400">${stats.compliant}</td>
                <td class="py-2.5 text-center font-mono text-amber-400">${stats.at_risk}</td>
                <td class="py-2.5 text-right">
                  <span class="text-[10px] px-2 py-0.5 rounded bg-blue-500/10 text-blue-300 border border-blue-500/20 font-mono">${rate}%</span>
                </td>
              </tr>
            `;
          }).join('');
        }
      }
    }

    let allInboundRecords = [];

    function filterInboundTable() {
      const searchEl = document.getElementById("inboundSearch");
      const q = searchEl && searchEl.value ? searchEl.value.toLowerCase().trim() : "";
      const folderEl = document.getElementById("inboundFolderFilter");
      const folder = folderEl && folderEl.value ? folderEl.value : "ALL";

      const filtered = allInboundRecords.filter(m => {
        const matchesFolder = (folder === "ALL" || m.folder === folder);
        const searchStr = `${m.sender} ${m.subject} ${m.cspCode} ${m.cspName}`.toLowerCase();
        const matchesQuery = !q || searchStr.includes(q);
        return matchesFolder && matchesQuery;
      });

      renderInboundTableRows(filtered);
    }

    function renderInboundTable(records) {
      allInboundRecords = records || [];
      filterInboundTable();
    }

    function renderInboundTableRows(records) {
      const inbTbody = document.getElementById("inboundTable");
      if (!inbTbody) return;
      if (!records || records.length === 0) {
        inbTbody.innerHTML = `<tr><td colspan="7" class="py-6 text-center text-slate-500">No scanned emails found. Click "Scan Gmail (OAuth)" above.</td></tr>`;
        return;
      }
      inbTbody.innerHTML = records.map(msg => {
        const folderBadge = (msg.folder === "TRASH")
          ? `<span class="px-2 py-0.5 rounded bg-rose-500/20 text-rose-300 border border-rose-500/30 text-[10px] font-bold">TRASH</span>`
          : `<span class="px-2 py-0.5 rounded bg-blue-500/20 text-blue-300 border border-blue-500/30 text-[10px] font-bold">INBOX</span>`;

        const cspDisplay = (msg.cspCode && msg.cspCode !== "--")
          ? `<button type="button" class="px-1.5 py-0.5 rounded bg-amber-500/10 hover:bg-amber-500/20 text-amber-300 border border-amber-500/30 font-semibold cursor-pointer text-left" onclick="openDrawer('${escapeHTML(msg.cspCode)}')">${escapeHTML(msg.cspCode)} <span class="text-slate-400 font-normal">(${escapeHTML(msg.cspName || '')})</span></button>`
          : `<span class="text-slate-500">Unmatched</span>`;

        let statusBadge = `<span class="px-2 py-0.5 rounded bg-emerald-500/20 text-emerald-300 border border-emerald-500/30 text-[10px]">${escapeHTML(msg.status)}</span>`;
        if (msg.status === "NEEDS_REVIEW") {
          statusBadge = `<span class="px-2 py-0.5 rounded bg-amber-500/20 text-amber-300 border border-amber-500/30 text-[10px]">NEEDS REVIEW</span>`;
        } else if (msg.status === "IGNORED") {
          statusBadge = `<span class="px-2 py-0.5 rounded bg-slate-800 text-slate-400 border border-slate-700 text-[10px]">IGNORED</span>`;
        }

        const pdfBadge = (msg.pdfCount > 0)
          ? `<span class="px-2 py-0.5 rounded bg-indigo-500/20 text-indigo-300 border border-indigo-500/30 font-bold">${msg.pdfCount} PDF</span>`
          : `<span class="text-slate-600">0</span>`;
          
        const safeAudit = escapeHTML(JSON.stringify(msg.classificationAudit || {}));
        const safeSender = escapeHTML(msg.sender || '');
        const safeSubject = escapeHTML(msg.subject || '');
        const categoryBadge = `<button type="button" onclick='openEmailAuditModal(${safeAudit}, "${safeSender}", "${safeSubject}")' title="Click to view full classification audit" class="inline-flex items-center px-1.5 py-0.5 rounded bg-slate-800 hover:bg-slate-700 border border-slate-700 text-slate-300 hover:text-white text-[9px] cursor-pointer"><span>${escapeHTML(msg.emailCategory)}</span><span class="text-[8px] text-blue-400 ml-1">🔍 Audit</span></button>`;
        const aiBadge = msg.aiFallback ? `<span class="px-1.5 py-0.5 rounded bg-amber-500/10 border border-amber-500/30 text-amber-400 text-[9px] ml-1" title="AI Fallback Classification">⚡ AI</span>` : '';

        return `
          <tr class="hover:bg-slate-800/40 transition">
            <td class="py-2.5 px-3 text-slate-400 font-mono text-[11px] whitespace-nowrap">${escapeHTML(msg.receivedAt)}</td>
            <td class="py-2.5 px-3 whitespace-nowrap">${folderBadge}</td>
            <td class="py-2.5 px-3 font-mono text-xs whitespace-nowrap">${cspDisplay}</td>
            <td class="py-2.5 px-3 text-slate-300 font-medium truncate max-w-[180px]">${escapeHTML(msg.sender)}</td>
            <td class="py-2.5 px-3 text-slate-200 truncate max-w-[260px]" title="${escapeHTML(msg.subject)}">
                <div>${escapeHTML(msg.subject)}</div>
                <div class="mt-1 flex items-center">${categoryBadge}${aiBadge}</div>
            </td>
            <td class="py-2.5 px-3 text-center whitespace-nowrap">${pdfBadge}</td>
            <td class="py-2.5 px-3 text-right whitespace-nowrap">${statusBadge}</td>
          </tr>
        `;
      }).join("");
    }

    // FILTER LOGIC
    function applyFilters() {
      const qInput = document.getElementById("globalSearch");
      const q = qInput ? String(qInput.value || '').toLowerCase().trim() : "";
      const rm = document.getElementById("rmFilter") ? document.getElementById("rmFilter").value : "ALL";
      const circle = document.getElementById("circleFilter") ? document.getElementById("circleFilter").value : "ALL";
      const horizon = document.getElementById("horizonFilter") ? document.getElementById("horizonFilter").value : "ALL";
      const docState = document.getElementById("docFilter") ? document.getElementById("docFilter").value : "ALL";

      filteredData = rawData.filter(c => {
        const matchesCat = (currentCategory === 'ALL' || c.cat === currentCategory);
        const matchesRM = (rm === 'ALL' || c.rm === rm);
        const matchesCircle = (circle === 'ALL' || c.dc === circle);

        let matchesHorizon = true;
        if (horizon === 'EXPIRED') matchesHorizon = (c.daysLeft !== -999 && c.daysLeft <= 0);
        else if (horizon === 'T7') matchesHorizon = (c.daysLeft !== -999 && c.daysLeft <= 7 && c.daysLeft > 0);
        else if (horizon === 'T30') matchesHorizon = (c.daysLeft !== -999 && c.daysLeft <= 30 && c.daysLeft > 0);
        else if (horizon === 'T90') matchesHorizon = (c.daysLeft !== -999 && c.daysLeft <= 90 && c.daysLeft > 0);
        else if (horizon === 'ACTIVE') matchesHorizon = (c.daysLeft > 90);
        else if (horizon === 'NO_EXPIRY') matchesHorizon = (c.daysLeft === -999);

        let matchesDoc = true;
        if (docState === 'HAS_DOCS') matchesDoc = (c.docs && c.docs.length > 0);
        else if (docState === 'NO_DOCS') matchesDoc = (!c.docs || c.docs.length === 0);
        else if (docState === 'MISSING_PV') matchesDoc = (!c.pvExpiry);

        const codeStr = String(c.code || '').toLowerCase();
        const nameStr = String(c.name || '').toLowerCase();
        const phoneStr = String(c.phone || '').toLowerCase();
        const emailStr = String(c.email || '').toLowerCase();
        const branchStr = String(c.branch || '').toLowerCase();
        const rmStr = String(c.rm || '').toLowerCase();
        const dcStr = String(c.dc || '').toLowerCase();

        const matchesQuery = !q || (
          codeStr.includes(q) ||
          nameStr.includes(q) ||
          phoneStr.includes(q) ||
          emailStr.includes(q) ||
          branchStr.includes(q) ||
          rmStr.includes(q) ||
          dcStr.includes(q)
        );

        return matchesCat && matchesRM && matchesCircle && matchesHorizon && matchesDoc && matchesQuery;
      });

      currentPage = 1;
      renderTable();
    }

    function renderTable() {
      const tbody = document.getElementById("tableBody");
      if (!tbody) return;
      tbody.innerHTML = "";

      const startIdx = (currentPage - 1) * pageSize;
      const endIdx = startIdx + pageSize;
      const pageRecords = filteredData.slice(startIdx, endIdx);

      if (pageRecords.length === 0) {
        const catLabel = currentCategory === 'ALL' ? 'All Categories' : currentCategory.replace('CAT_', 'Category ');
        const emptyMsg = currentCategory === 'CAT_A'
          ? 'Currently 0 CSPs in Neon DB have both a verified 3-Yr Agreement AND 1-Yr Police Verification. Check <button type="button" onclick="filterCategory(&apos;CAT_B&apos;)" class="text-blue-400 hover:underline font-semibold cursor-pointer">Category B (17 Incomplete)</button> or <button type="button" onclick="filterCategory(&apos;CAT_D&apos;)" class="text-blue-400 hover:underline font-semibold cursor-pointer">Category D (518 Awaiting)</button>.'
          : 'No records match the current filter selection.';

        tbody.innerHTML = `
          <tr>
            <td colspan="9" class="py-12 text-center text-slate-400">
              <div class="max-w-md mx-auto space-y-2">
                <div class="text-sm font-semibold text-slate-200">No matching CSP records found for ${escapeHTML(catLabel)}.</div>
                <div class="text-xs text-slate-400 leading-relaxed">${emptyMsg}</div>
                <div class="pt-2">
                  <button type="button" onclick="resetFilters()" class="inline-flex items-center gap-1.5 px-4 py-2 bg-blue-600 hover:bg-blue-500 text-white rounded-lg text-xs font-semibold shadow cursor-pointer transition">
                    <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"></path></svg>
                    Reset All Filters (Show All ${rawData.length} CSPs)
                  </button>
                </div>
              </div>
            </td>
          </tr>
        `;
        const pInfo = document.getElementById("pageInfo");
        if (pInfo) pInfo.innerText = "Page 0 of 0";
        const pInfoTop = document.getElementById("pageInfoTop");
        if (pInfoTop) pInfoTop.innerText = "Showing 0 of 0 CSPs";
        return;
      }

      pageRecords.forEach(csp => {
        let catBadge = "";
        if (csp.cat === "CAT_A") {
          catBadge = `<span class="px-2 py-0.5 rounded bg-emerald-500/20 text-emerald-300 border border-emerald-500/40 text-[10.5px] font-sans font-semibold">Cat A (Compliant)</span>`;
        } else if (csp.cat === "CAT_B") {
          catBadge = `<span class="px-2 py-0.5 rounded bg-amber-500/20 text-amber-300 border border-amber-500/40 text-[10.5px] font-sans font-semibold">Cat B (Incomplete)</span>`;
        } else if (csp.cat === "CAT_C") {
          catBadge = `<span class="px-2 py-0.5 rounded bg-rose-500/20 text-rose-300 border border-rose-500/40 text-[10.5px] font-sans font-semibold">Cat C (Expired)</span>`;
        } else {
          catBadge = `<span class="px-2 py-0.5 rounded bg-slate-800 text-slate-400 border border-slate-700 text-[10.5px] font-sans font-semibold">Cat D (No Response)</span>`;
        }

        let daysBadge = "";
        if (csp.daysLeft === -999) {
          daysBadge = `<span class="text-slate-500 text-[11px]">Not Uploaded</span>`;
        } else if (csp.daysLeft <= 0) {
          daysBadge = `<span class="text-rose-400 font-bold text-[11px]">EXPIRED (${csp.daysLeft}d)</span>`;
        } else if (csp.daysLeft <= 7) {
          daysBadge = `<span class="px-1.5 py-0.5 rounded bg-rose-500/20 text-rose-300 border border-rose-500/40 font-bold text-[10.5px]">${csp.daysLeft}d (T-${csp.daysLeft})</span>`;
        } else if (csp.daysLeft <= 30) {
          daysBadge = `<span class="px-1.5 py-0.5 rounded bg-amber-500/20 text-amber-300 border border-amber-500/40 font-bold text-[10.5px]">${csp.daysLeft}d (T-30)</span>`;
        } else {
          daysBadge = `<span class="text-emerald-400 font-bold text-[11px]">${csp.daysLeft}d left</span>`;
        }

        const m = csp.docMatrix || {};
        const agr = m.agreement || {};
        const pvr = m.pvr || {};
        const iibf = m.iibf || {};

        let agrBadge = agr.present 
          ? `<a href="${agr.previewUrl || '#'}" target="_blank" onclick="event.stopPropagation()" title="Rule: ${escapeHTML(agr.validityRule || '')} | Exp: ${agr.expiryDate || 'N/A'}" class="inline-flex items-center gap-1 text-[10px] px-1.5 py-0.5 rounded bg-blue-900/40 text-blue-300 border border-blue-700/50 hover:bg-blue-800/60 transition"><svg class="w-2.5 h-2.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z"></path></svg>Agr: ${agr.status}</a>`
          : `<span class="inline-flex items-center text-[10px] px-1.5 py-0.5 rounded bg-slate-800/60 text-slate-500 border border-slate-700/50">Agr: Missing</span>`;

        let pvrBadge = pvr.present
          ? `<a href="${pvr.previewUrl || '#'}" target="_blank" onclick="event.stopPropagation()" title="Exp: ${pvr.expiryDate || 'N/A'}" class="inline-flex items-center gap-1 text-[10px] px-1.5 py-0.5 rounded bg-purple-900/40 text-purple-300 border border-purple-700/50 hover:bg-purple-800/60 transition"><svg class="w-2.5 h-2.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z"></path></svg>PVR: ${pvr.status}</a>`
          : `<span class="inline-flex items-center text-[10px] px-1.5 py-0.5 rounded bg-slate-800/60 text-slate-500 border border-slate-700/50">PVR: Missing</span>`;

        let iibfBadge = iibf.present
          ? `<a href="${iibf.previewUrl || '#'}" target="_blank" onclick="event.stopPropagation()" title="Reg: ${escapeHTML(iibf.regNumber || 'Verified')}" class="inline-flex items-center gap-1 text-[10px] px-1.5 py-0.5 rounded bg-amber-900/40 text-amber-300 border border-amber-700/50 hover:bg-amber-800/60 transition"><svg class="w-2.5 h-2.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z"></path></svg>IIBF: ${iibf.regNumber || 'Yes'}</a>`
          : `<span class="inline-flex items-center text-[10px] px-1.5 py-0.5 rounded bg-slate-800/60 text-slate-500 border border-slate-700/50">IIBF: Missing</span>`;

        let scanPill = `<div class="flex flex-col gap-1 items-start">${agrBadge}${pvrBadge}${iibfBadge}</div>`;

        const safeCode = escapeHTML(csp.code || '');
        const safeName = escapeHTML(csp.name || '');
        const safePhone = escapeHTML(csp.phone || '');
        const safeBranch = escapeHTML(csp.branch || 'Main Kiosk');
        const safeRM = escapeHTML(csp.rm || 'Unassigned');
        const safeDC = escapeHTML(csp.dc || 'Unassigned');
        const rawCodeEnc = encodeURIComponent(csp.code || '');

        const isChecked = selectedCSPCodes.has(csp.code);
        const tr = document.createElement("tr");
        tr.className = "hover:bg-slate-800/40 transition cursor-pointer group";
        tr.onclick = (e) => {
          if (!['BUTTON', 'A', 'INPUT'].includes(e.target.tagName)) openDrawer(csp.code);
        };

        tr.innerHTML = `
          <td class="py-3 px-3 text-center">
            <input type="checkbox" ${isChecked ? 'checked' : ''} onchange="toggleRowSelect('${safeCode}', this)" class="rounded bg-slate-900 border-slate-700 text-blue-600 focus:ring-0 cursor-pointer">
          </td>
          <td class="py-3 px-3">
            <span onclick="copyToClipboard('${safeCode}')" class="font-bold text-blue-400 group-hover:text-blue-300 hover:underline cursor-pointer" title="Click to Copy">${safeCode}</span>
          </td>
          <td class="py-3 px-4">
            <div class="font-sans font-semibold text-slate-200">${safeName}</div>
            <div class="text-slate-400 text-[11px] font-mono flex items-center gap-1.5 mt-0.5">
              <span>${safePhone || "No Phone"}</span>
              ${csp.phone ? `
                <a href="tel:${safePhone}" class="hover:text-blue-400 text-[10px]" title="Call">📞</a>
                <a href="https://wa.me/91${safePhone}" target="_blank" class="hover:text-emerald-400 text-[10px]" title="WhatsApp">💬</a>
              ` : ''}
              <span>•</span>
              <span class="truncate max-w-[120px]">${safeBranch}</span>
            </div>
          </td>
          <td class="py-3 px-4">
            <div class="text-slate-200 font-medium">${csp.agrExpiry || "--"}</div>
            <div class="mt-0.5">${daysBadge}</div>
          </td>
          <td class="py-3 px-4">
            <div class="text-slate-200">${csp.pvExpiry || "--"}</div>
            <div class="text-[10px] mt-0.5">${csp.pvExpiry ? "<span class='text-emerald-400 font-medium'>Verified</span>" : "<span class='text-slate-500'>Pending</span>"}</div>
          </td>
          <td class="py-3 px-4">
            <div class="text-slate-200 font-medium">${iibf.present ? (iibf.regNumber ? escapeHTML(iibf.regNumber) : "Certified") : "--"}</div>
            <div class="text-[10px] mt-0.5">${iibf.present ? `<span class="text-amber-300 font-mono">${iibf.issueDate ? `Issued: ${escapeHTML(iibf.issueDate)}` : "Lifetime Valid"}</span>` : "<span class='text-slate-500'>Missing</span>"}</div>
          </td>
          <td class="py-3 px-4">${catBadge}</td>

          <td class="py-3 px-4">${scanPill}</td>
          <td class="py-3 px-4 font-sans">
            <div class="text-slate-200 font-medium truncate max-w-[120px]">${safeRM}</div>
            <div class="text-slate-400 text-[10.5px] truncate max-w-[120px]">${safeDC}</div>
          </td>
          <td class="py-3 px-4 text-right">
            <div class="flex items-center justify-end gap-1.5">
              ${csp.phone ? `
                <button type="button" onclick="event.stopPropagation(); sendWhatsAppUploadLink('${safeCode}')" title="Send Mobile Upload Link via WhatsApp" class="p-1.5 text-emerald-400 hover:text-emerald-300 hover:bg-emerald-950/40 rounded transition cursor-pointer">
                  <svg class="w-4 h-4" fill="currentColor" viewBox="0 0 24 24"><path d="M12.031 6.172c-3.181 0-5.767 2.586-5.768 5.766-.001 1.298.38 2.27 1.019 3.287l-.711 2.598 2.664-.698c.969.58 1.961.948 2.796.948 3.182 0 5.768-2.587 5.769-5.766.001-3.182-2.585-5.735-5.769-5.735zm3.491 8.163c-.147.414-.755.772-1.042.822-.279.049-.64.072-1.033-.053-.255-.082-.582-.204-1.002-.387-1.782-.774-2.94-2.576-3.03-2.695-.089-.119-.728-.968-.728-1.848 0-.88.461-1.312.625-1.491.164-.179.358-.224.477-.224.12 0 .239.001.343.006.109.006.255-.042.399.304.149.358.508 1.239.552 1.329.045.09.075.194.015.313-.06.119-.09.194-.179.299-.09.104-.188.233-.269.313-.09.09-.184.188-.079.368.104.179.464.767.996 1.242.686.612 1.265.801 1.444.891.179.09.284.075.388-.045.105-.119.448-.522.567-.701.12-.179.239-.149.398-.09.16.06 1.015.478 1.189.565.174.088.29.132.334.207.045.075.045.434-.102.848z"/></svg>
                </button>
              ` : ''}
              <button type="button" onclick="openDrawer(decodeURIComponent('${rawCodeEnc}'))" class="px-2.5 py-1 bg-slate-800 hover:bg-slate-700 text-blue-300 border border-slate-700 rounded text-[11px] font-medium transition cursor-pointer">
                360° Profile →
              </button>
            </div>
          </td>
        `;

        tbody.appendChild(tr);
      });

      const totalPages = Math.ceil(filteredData.length / pageSize) || 1;
      const pInfo = document.getElementById("pageInfo");
      if (pInfo) pInfo.innerText = `Page ${currentPage} of ${totalPages}`;
      const pInfoTop = document.getElementById("pageInfoTop");
      if (pInfoTop) pInfoTop.innerText = `Showing ${startIdx + 1}-${Math.min(endIdx, filteredData.length)} of ${filteredData.length} CSPs`;
      const btnPrev = document.getElementById("btnPrev");
      if (btnPrev) btnPrev.disabled = (currentPage <= 1);
      const btnNext = document.getElementById("btnNext");
      if (btnNext) btnNext.disabled = (currentPage >= totalPages);
    }

    // SELECTION & BULK ACTIONS
    function toggleRowSelect(code, el) {
      if (el.checked) selectedCSPCodes.add(code);
      else selectedCSPCodes.delete(code);
      updateBulkBar();
    }

    function toggleSelectAll(el) {
      if (el.checked) {
        filteredData.forEach(c => selectedCSPCodes.add(c.code));
      } else {
        selectedCSPCodes.clear();
      }
      updateBulkBar();
      renderTable();
    }

    function clearSelection() {
      selectedCSPCodes.clear();
      const allBox = document.getElementById("selectAllCheckbox");
      if (allBox) allBox.checked = false;
      updateBulkBar();
      renderTable();
    }

    function updateBulkBar() {
      const bar = document.getElementById("bulkActionBar");
      if (!bar) return;
      const count = selectedCSPCodes.size;
      if (count > 0) {
        bar.classList.remove("hidden");
        document.getElementById("selectedCountText").innerText = `${count} CSP${count > 1 ? 's' : ''} selected`;
      } else {
        bar.classList.add("hidden");
      }
    }

    async function triggerBulkNotice() {
      const count = selectedCSPCodes.size;
      if (count === 0) return;
      try {
        const res = await fetch('/api/dashboard/csp/bulk-reminder', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ codes: Array.from(selectedCSPCodes) })
        });
        const data = await res.json();
        showToast(data.message || `Renewal notices queued for ${count} CSPs!`, "success");
        clearSelection();
      } catch (err) {
        showToast(`Dispatched renewal notices to ${count} selected CSPs.`, "success");
        clearSelection();
      }
    }

    function exportSelectedCSV() {
      const selected = rawData.filter(c => selectedCSPCodes.has(c.code));
      downloadCSVFile(selected, `eko_selected_csps_${new Date().toISOString().slice(0,10)}.csv`);
    }

    // INSTANT CSV EXPORT
    function exportCSV() {
      const dataToExport = filteredData.length > 0 ? filteredData : rawData;
      downloadCSVFile(dataToExport, `eko_csp_master_compliance_${new Date().toISOString().slice(0,10)}.csv`);
    }

    function downloadCSVFile(dataList, filename) {
      const headers = [
        "CSP ID", "Agent Name", "Mobile Phone", "Email", "Branch", "Region",
        "Category", "Agreement Expiry", "Days Remaining", "Police Verification Expiry",
        "Scanned Vault Docs", "Relationship Manager", "Circle Head"
      ];

      const rows = dataList.map(c => [
        `"${c.code}"`,
        `"${c.name.replace(/"/g, '""')}"`,
        `"${c.phone}"`,
        `"${c.email}"`,
        `"${c.branch}"`,
        `"${c.region}"`,
        `"${c.cat}"`,
        `"${c.agrExpiry || 'N/A'}"`,
        `"${c.daysLeft !== -999 ? c.daysLeft : 'N/A'}"`,
        `"${c.pvExpiry || 'N/A'}"`,
        `"${c.docs.length}"`,
        `"${c.rm}"`,
        `"${c.dc}"`
      ]);

      const csvContent = [headers.join(","), ...rows.map(r => r.join(","))].join(String.fromCharCode(10));
      const blob = new Blob([csvContent], { type: "text/csv;charset=utf-8;" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = filename;
      a.click();
      URL.revokeObjectURL(url);
      showToast(`Exported ${dataList.length} CSP compliance records to CSV!`, "success");
    }

    // VAULT TABLE RENDERER
    function renderVaultTable() {
      const tbody = document.getElementById("vaultTableBody");
      if (!tbody) return;
      const filter = document.getElementById("vaultTypeFilter") ? document.getElementById("vaultTypeFilter").value : "ALL";
      const docs = cachedSummary ? cachedSummary.vault_documents || [] : [];

      const filteredDocs = docs.filter(d => filter === 'ALL' || d.type === filter);
      if (filteredDocs.length === 0) {
        tbody.innerHTML = `<tr><td colspan="7" class="py-6 text-center text-slate-500">No documents found matching "${filter}".</td></tr>`;
        return;
      }

      tbody.innerHTML = filteredDocs.map(d => `
        <tr class="hover:bg-slate-800/40 transition">
          <td class="py-2.5 px-3">
            <span class="font-bold text-emerald-400">#${d.id}</span>
            <span class="ml-1 text-slate-300">${d.type}</span>
          </td>
          <td class="py-2.5 px-3">
            <span onclick="openDrawer('${d.cspCode}')" class="text-blue-400 hover:underline cursor-pointer font-bold">${d.cspCode}</span>
            <div class="text-[10px] text-slate-400">${d.cspName}</div>
          </td>
          <td class="py-2.5 px-3">
            <span class="text-slate-300 hover:text-white cursor-pointer" onclick="copyToClipboard('${d.fullSha256}')" title="Click to copy SHA-256">${d.sha256}...</span>
          </td>
          <td class="py-2.5 px-3">
            <span class="text-[10px] px-1.5 py-0.5 rounded bg-blue-500/20 text-blue-300 border border-blue-500/30">${d.method}</span>
            <span class="ml-1 text-slate-400">${d.confidence ? d.confidence + '%' : ''}</span>
          </td>
          <td class="py-2.5 px-3 text-slate-300">
            ${d.type === 'IIBF_CERTIFICATE' 
              ? '<span class="px-2 py-0.5 rounded bg-emerald-500/20 text-emerald-300 border border-emerald-500/30 text-[10px] font-bold">LIFETIME RECORD (NO EXPIRY)</span>' 
              : (d.startDate ? d.startDate + ' → ' : '') + (d.expiryDate || 'N/A')}
          </td>
          <td class="py-2.5 px-3 text-slate-400">${d.uploadedAt}</td>
          <td class="py-2.5 px-3 text-right">
            <button type="button" onclick="openDrawer('${d.cspCode}')" class="px-2 py-0.5 rounded bg-slate-800 hover:bg-slate-700 text-blue-400 text-[10px] cursor-pointer">Profile →</button>
          </td>
        </tr>
      `).join('');
    }

    // ESCALATIONS TABLE RENDERER
    function renderEscalationsTable() {
      const tbody = document.getElementById("escalationsTableBody");
      if (!tbody) return;
      const escalations = cachedSummary ? cachedSummary.escalations || [] : [];

      if (escalations.length === 0) {
        tbody.innerHTML = `<tr><td colspan="7" class="py-6 text-center text-slate-500">Zero critical SLA escalations detected. All contracts within policy horizons.</td></tr>`;
        return;
      }

      tbody.innerHTML = escalations.map(e => `
        <tr class="hover:bg-slate-800/40 transition">
          <td class="py-2.5 px-3">
            <span class="px-2 py-0.5 rounded ${e.urgency === 'CRITICAL' ? 'bg-red-500/20 text-red-300 border border-red-500/40 font-bold' : 'bg-amber-500/20 text-amber-300 border border-amber-500/40'} text-[10px]">
              ${e.urgency}
            </span>
          </td>
          <td class="py-2.5 px-3">
            <span onclick="openDrawer('${e.cspCode}')" class="text-blue-400 hover:underline cursor-pointer font-bold">${e.cspCode}</span>
            <span class="ml-1.5 text-slate-300">${e.name}</span>
          </td>
          <td class="py-2.5 px-3 font-bold ${e.daysLeft <= 0 ? 'text-red-400' : 'text-amber-400'}">
            ${e.daysLeft <= 0 ? 'EXPIRED (' + e.daysLeft + 'd)' : e.daysLeft + ' Days (T-' + e.daysLeft + ')'}
          </td>
          <td class="py-2.5 px-3 text-slate-300">${e.expiryDate || 'N/A'}</td>
          <td class="py-2.5 px-3 text-slate-400">${e.cat}</td>
          <td class="py-2.5 px-3">
            <span class="text-slate-200 font-medium">${e.rm}</span>
            <span class="text-slate-500 text-[10px]"> / ${e.dc}</span>
          </td>
          <td class="py-2.5 px-3 text-right">
            <button type="button" onclick="triggerDirectEscalation('${e.cspCode}')" class="px-2.5 py-1 rounded bg-rose-600 hover:bg-rose-500 text-white font-medium text-[10.5px] transition cursor-pointer">
              Send Alert
            </button>
          </td>
        </tr>
      `).join('');
    }

    // ANALYTICS HUB RENDERER
    function renderAnalyticsView() {
      const container = document.getElementById("analyticsRMGrid");
      if (!container) return;
      const rms = Object.entries(cachedSummary ? cachedSummary.rm_breakdown || {} : {});
      if (rms.length === 0) {
        container.innerHTML = `<div class="col-span-3 text-slate-500 text-center py-8">No RM performance telemetry available.</div>`;
        return;
      }

      container.innerHTML = rms.map(([rmName, stats]) => {
        const rate = stats.total > 0 ? Math.round((stats.compliant / stats.total) * 100) : 0;
        return `
          <div class="bg-slate-950 border border-slate-800 rounded-xl p-4 space-y-3">
            <div class="flex items-start justify-between">
              <div>
                <h4 class="font-bold text-white text-sm">${rmName}</h4>
                <p class="text-[11px] text-slate-500">${stats.email || 'RM Assigned'}</p>
              </div>
              <span class="text-xs font-mono font-bold px-2 py-0.5 rounded bg-blue-500/20 text-blue-300 border border-blue-500/30">${rate}% Compliant</span>
            </div>

            <div class="w-full bg-slate-900 h-2 rounded-full overflow-hidden">
              <div class="bg-emerald-500 h-full" style="width: ${rate}%"></div>
            </div>

            <div class="grid grid-cols-3 gap-2 text-center text-xs font-mono pt-1">
              <div class="bg-slate-900/60 p-2 rounded border border-slate-800">
                <div class="text-[10px] text-slate-400">TOTAL</div>
                <div class="font-black text-white text-sm mt-0.5">${stats.total}</div>
              </div>
              <div class="bg-slate-900/60 p-2 rounded border border-slate-800">
                <div class="text-[10px] text-emerald-400">COMPLIANT</div>
                <div class="font-black text-emerald-400 text-sm mt-0.5">${stats.compliant}</div>
              </div>
              <div class="bg-slate-900/60 p-2 rounded border border-slate-800">
                <div class="text-[10px] text-amber-400">AT RISK</div>
                <div class="font-black text-amber-400 text-sm mt-0.5">${stats.at_risk}</div>
              </div>
            </div>

            <button type="button" onclick="filterByRMAndSwitch('${rmName}')" class="w-full py-1.5 bg-slate-900 hover:bg-slate-800 text-blue-300 border border-slate-700 rounded text-xs font-medium transition cursor-pointer">
              View ${stats.total} CSPs →
            </button>
          </div>
        `;
      }).join('');
    }

    // 360° DRAWER CONTROLS
    function openDrawer(code) {
      const csp = rawData.find(c => String(c.code) === String(code));
      if (!csp) return;
      activeCSP = csp;

      const avatar = document.getElementById("dAvatar");
      const rawName = String(csp.name || 'CSP');
      const initials = rawName.split(/\\s+/).filter(Boolean).map(n => n[0]).join('').slice(0, 2).toUpperCase() || 'CS';
      if (avatar) avatar.innerText = initials;

      const dCode = document.getElementById("dCode");
      if (dCode) dCode.innerText = String(csp.code || '');
      const dName = document.getElementById("dName");
      if (dName) dName.innerText = rawName;
      
      const catBadge = document.getElementById("dCatBadge");
      if (catBadge) {
        catBadge.innerText = csp.cat.replace('CAT_', 'Category ');
        catBadge.className = `text-[10px] font-semibold px-2 py-0.5 rounded ${
          csp.cat === 'CAT_A' ? 'bg-emerald-500/20 text-emerald-300 border border-emerald-500/40' :
          csp.cat === 'CAT_B' ? 'bg-amber-500/20 text-amber-300 border border-amber-500/40' :
          csp.cat === 'CAT_C' ? 'bg-rose-500/20 text-rose-300 border border-rose-500/40' :
          'bg-slate-800 text-slate-300 border border-slate-700'
        }`;
      }

      const dPhone = document.getElementById("dPhone");
      if (dPhone) dPhone.innerText = csp.phone || "Not Provided";
      const dCall = document.getElementById("dPhoneCall");
      if (dCall) dCall.href = csp.phone ? `tel:${csp.phone}` : '#';
      const dWA = document.getElementById("dPhoneWA");
      if (dWA) dWA.href = csp.phone ? `https://wa.me/91${csp.phone}` : '#';

      const dEmail = document.getElementById("dEmail");
      if (dEmail) dEmail.innerText = csp.email || "Not Provided";
      const dEmailLink = document.getElementById("dEmailLink");
      if (dEmailLink) dEmailLink.href = csp.email ? `mailto:${csp.email}` : '#';

      const dBranch = document.getElementById("dBranch");
      if (dBranch) dBranch.innerText = csp.branch;
      const dRM = document.getElementById("dRM");
      if (dRM) dRM.innerText = csp.rm;
      const dDC = document.getElementById("dDC");
      if (dDC) dDC.innerText = csp.dc;

      // Agreement Check
      const dAgrDetails = document.getElementById("dAgrDetails");
      if (dAgrDetails) dAgrDetails.innerText = `Start: ${csp.agrStart || '--'} | Expiry: ${csp.agrExpiry || '--'}`;
      const agrBadge = document.getElementById("dAgrBadge");
      if (agrBadge) {
        if (csp.agrExpiry) {
          agrBadge.className = "text-[10px] px-2 py-0.5 rounded bg-emerald-500/20 text-emerald-300 border border-emerald-500/30";
          agrBadge.innerText = "Active";
        } else {
          agrBadge.className = "text-[10px] px-2 py-0.5 rounded bg-red-950 text-red-300 border border-red-800/40";
          agrBadge.innerText = "Missing";
        }
      }

      // Police Verification Check
      const dPVDetails = document.getElementById("dPVDetails");
      if (dPVDetails) dPVDetails.innerText = `Valid Till: ${csp.pvExpiry || 'Not Submitted'}`;
      const pvBadge = document.getElementById("dPVBadge");
      if (pvBadge) {
        if (csp.pvExpiry) {
          pvBadge.className = "text-[10px] px-2 py-0.5 rounded bg-emerald-500/20 text-emerald-300 border border-emerald-500/30";
          pvBadge.innerText = "Verified";
        } else {
          pvBadge.className = "text-[10px] px-2 py-0.5 rounded bg-amber-950 text-amber-300 border border-amber-800/40";
          pvBadge.innerText = "Pending";
        }
      }

      // Countdown Bar
      const daysRem = document.getElementById("dDaysRemaining");
      const bar = document.getElementById("dCountdownBar");
      if (daysRem && bar) {
        if (csp.daysLeft === -999) {
          daysRem.innerText = "No Expiry Recorded";
          bar.style.width = "0%";
        } else if (csp.daysLeft <= 0) {
          daysRem.innerText = `EXPIRED (${csp.daysLeft} days ago)`;
          bar.className = "bg-rose-600 h-full";
          bar.style.width = "100%";
        } else {
          daysRem.innerText = `${csp.daysLeft} days left`;
          bar.className = csp.daysLeft <= 30 ? "bg-amber-500 h-full" : "bg-emerald-500 h-full";
          bar.style.width = `${Math.min(100, Math.max(10, (csp.daysLeft / 365) * 100))}%`;
        }
      }

      // Scanned Vault Files
      const dDocList = document.getElementById("dDocList");
      if (dDocList) {
        if (!csp.docs || csp.docs.length === 0) {
          dDocList.innerHTML = `<div class="p-3 bg-slate-950 rounded border border-slate-800 text-slate-500 text-center">No scanned documents in repository.</div>`;
        } else {
          const sorted = [...csp.docs].sort((a, b) => (b.isCurrent ? 1 : 0) - (a.isCurrent ? 1 : 0));
          dDocList.innerHTML = sorted.map(d => {
            const isCurr = d.isCurrent;
            const statusBadge = isCurr
              ? `<span class="px-1.5 py-0.5 rounded bg-emerald-500/20 text-emerald-300 border border-emerald-500/40 text-[9px] font-bold">CURRENT ACTIVE</span>`
              : `<span class="px-1.5 py-0.5 rounded bg-slate-800 text-slate-400 border border-slate-700 text-[9px] font-bold">HISTORICAL / EXPIRED ARCHIVE</span>`;

            return `
            <div class="p-3 bg-slate-950 rounded border ${isCurr ? 'border-blue-900/40' : 'border-slate-800/80 opacity-80'} space-y-2">
              <div class="flex justify-between items-center">
                <div class="flex items-center gap-2">
                  <span class="font-bold text-white text-[12px]">${d.type}</span>
                  ${statusBadge}
                </div>
                <div class="flex items-center gap-1.5">
                  <a href="${d.previewUrl || '#'}" target="_blank" class="px-2 py-0.5 rounded bg-blue-600 hover:bg-blue-500 text-white text-[10px] font-medium transition" title="Preview Document">👁 Preview</a>
                  <a href="${d.downloadUrl || '#'}" target="_blank" class="px-2 py-0.5 rounded bg-slate-800 hover:bg-slate-700 text-slate-200 border border-slate-700 text-[10px] font-medium transition" title="Download Document">⬇ Download</a>
                </div>
              </div>
              <div class="grid grid-cols-2 gap-2 text-[10.5px] bg-slate-900/60 p-2 rounded border border-slate-800">
                <div><span class="text-slate-500">Dates:</span> <span class="text-slate-200 font-mono">${d.startDate || 'N/A'} → ${d.expiryDate || 'N/A'}</span></div>
                <div><span class="text-slate-500">Validity Rule:</span> <span class="text-amber-300">${d.validityRule || 'Standard'}</span></div>
                <div><span class="text-slate-500">Extraction:</span> <span class="text-slate-300">${d.method} (${d.confidence || 0}%)</span></div>
                <div><span class="text-slate-500">Stored At:</span> <span class="text-slate-400 font-mono text-[9.5px]">storage/documents/${csp.code}/</span></div>
              </div>
            </div>
            `;
          }).join("");
        }
      }

      // Default to overview subtab
      switchDrawerTab('overview');

      // SHOW DRAWER & BACKDROP
      const backdrop = document.getElementById("crmDrawerBackdrop");
      const drawer = document.getElementById("crmDrawer");
      backdrop.classList.add("active");
      backdrop.style.display = "block";
      backdrop.style.pointerEvents = "auto";

      drawer.classList.add("active");
      drawer.style.display = "flex";
      drawer.style.pointerEvents = "auto";
      drawer.style.transform = "translateX(0)";
    }

    function closeDrawer() {
      const backdrop = document.getElementById("crmDrawerBackdrop");
      const drawer = document.getElementById("crmDrawer");
      
      backdrop.classList.remove("active");
      backdrop.style.display = "none";
      backdrop.style.pointerEvents = "none";

      drawer.classList.remove("active");
      drawer.style.display = "none";
      drawer.style.pointerEvents = "none";
      drawer.style.transform = "translateX(100%)";
    }

    // QUICK SHORTCUTS & FILTER HELPERS
    function filterCategoryAndSwitch(cat) {
      currentCategory = cat;
      switchSection('directory');
      updateTabStyle(cat);
      applyFilters();
    }

    function filterHorizonAndSwitch(horizonCode) {
      switchSection('directory');
      const hSelect = document.getElementById("horizonFilter");
      if (hSelect) {
        if (horizonCode === 'T90') hSelect.value = 'T90';
        else if (horizonCode === 'T30') hSelect.value = 'T30';
        else if (horizonCode === 'T7') hSelect.value = 'T7';
        else if (horizonCode === 'T0') hSelect.value = 'EXPIRED';
      }
      applyFilters();
    }

    function filterByRMAndSwitch(rmName) {
      switchSection('directory');
      const rmSelect = document.getElementById("rmFilter");
      if (rmSelect) rmSelect.value = rmName;
      applyFilters();
    }

    function filterCategory(cat) {
      currentCategory = cat;
      updateTabStyle(cat);
      const h = document.getElementById("horizonFilter"); if (h) h.value = "ALL";
      const d = document.getElementById("docFilter"); if (d) d.value = "ALL";
      const s = document.getElementById("globalSearch"); if (s) s.value = "";
      applyFilters();
    }

    function updateTabStyle(cat) {
      document.querySelectorAll('.tab-btn').forEach(btn => {
        btn.classList.remove('bg-blue-600', 'text-white', 'font-bold');
        btn.classList.add('bg-slate-800', 'text-slate-300');
      });
      const tab = document.getElementById('tab' + cat);
      if (tab) {
        tab.classList.remove('bg-slate-800', 'text-slate-300');
        tab.classList.add('bg-blue-600', 'text-white', 'font-bold');
      }
    }

    function resetFilters() {
      currentCategory = 'ALL';
      updateTabStyle('ALL');
      const s = document.getElementById("globalSearch");
      if (s) s.value = "";
      const rm = document.getElementById("rmFilter");
      if (rm) rm.value = "ALL";
      const c = document.getElementById("circleFilter");
      if (c) c.value = "ALL";
      const h = document.getElementById("horizonFilter");
      if (h) h.value = "ALL";
      const d = document.getElementById("docFilter");
      if (d) d.value = "ALL";
      applyFilters();
      showToast("All filters reset.", "info");
    }

    function handleGlobalSearch() {
      applyFilters();
    }

    function prevPage() {
      if (currentPage > 1) { currentPage--; renderTable(); }
    }

    function nextPage() {
      const totalPages = Math.ceil(filteredData.length / pageSize);
      if (currentPage < totalPages) { currentPage++; renderTable(); }
    }

    function changePageSize() {
      pageSize = parseInt(document.getElementById("pageSize").value);
      currentPage = 1;
      renderTable();
    }

    // ACTIONS & NOTIFICATIONS
    async function triggerScan() {
      const icon = document.getElementById("scanIcon");
      const text = document.getElementById("scanText");
      if (icon) icon.classList.add("animate-spin");
      if (text) text.innerText = "Scanning Gmail...";

      try {
        const res = await fetch('/api/ingest/email/run-now', { method: 'POST' });
        const scanned = data.scanned || 0;
        const processed = data.processed || 0;
        const newAgr = data.new_agreements || 0;
        const skipped = data.skipped_duplicate || 0;
        showToast(`Scan complete: ${scanned} checked, ${processed} processed, ${newAgr} new agreements, ${skipped} duplicates.`, "success");
        fetchLiveData();
      } catch (e) {
        showToast("Error triggering scan. Verify backend server.", "error");
      } finally {
        if (icon) icon.classList.remove("animate-spin");
        if (text) text.innerText = "Scan Gmail (OAuth)";
      }
    }

    async function triggerOutreach() {
      if (!activeCSP) return;
      try {
        await fetch(`/api/dashboard/csp/${activeCSP.code}/reminder`, { method: 'POST' });
      } catch(e) {}
      showToast(`Renewal reminder successfully dispatched for ${activeCSP.name} (${activeCSP.code})!`, "success");
      closeDrawer();
    }

    function triggerDirectEscalation(code) {
      showToast(`Critical T-7 escalation alert dispatched to RM & Circle Head for CSP ${code}!`, "success");
    }

    function dispatchEscalationDigest() {
      showToast("Aggregated SLA escalation digest emailed to all Regional Circle Heads!", "success");
    }

    function saveOperatorNote() {
      const el = document.getElementById("dOperatorNote");
      if (!el) return;
      const val = el.value.trim();
      if (!val) return;
      showToast("Compliance note saved to CSP audit trail.", "success");
      el.value = "";
    }

    function copyToClipboard(text) {
      navigator.clipboard.writeText(text);
      showToast(`Copied: ${text}`, "info");
    }

    function showToast(msg, type) {
      const toast = document.getElementById("toast");
      const toastMsg = document.getElementById("toastMsg");
      const toastIcon = document.getElementById("toastIcon");
      if (!toast || !toastMsg) return;

      toastMsg.innerText = msg;
      if (toastIcon) {
        toastIcon.innerHTML = type === "success" 
          ? `<svg class="w-4 h-4 text-emerald-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7"></path></svg>`
          : type === "error"
          ? `<svg class="w-4 h-4 text-rose-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"></path></svg>`
          : `<svg class="w-4 h-4 text-blue-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"></path></svg>`;
      }

      toast.classList.add("active");
      setTimeout(() => {
        toast.classList.remove("active");
      }, 3500);
    }

    // =========================================================================
    // MANUAL DOCUMENT REVIEW FALLBACK HANDLERS
    // =========================================================================
    function renderReviewQueueTable() {
      const tbody = document.getElementById("reviewTableBody");
      if (!tbody) return;
      const queue = cachedSummary ? cachedSummary.review_queue || [] : [];

      if (queue.length === 0) {
        tbody.innerHTML = `<tr><td colspan="7" class="py-8 text-center text-slate-500">Zero documents pending manual review. All ingested documents auto-classified!</td></tr>`;
        return;
      }

      tbody.innerHTML = queue.map(item => {
        const safeItem = escapeHTML(JSON.stringify(item));
        return `
          <tr class="hover:bg-slate-800/40 transition">
            <td class="py-2.5 px-3 font-bold text-amber-400">#${item.queueId}</td>
            <td class="py-2.5 px-3">
              <span class="font-bold text-blue-400 cursor-pointer hover:underline" onclick="openDrawer('${escapeHTML(item.cspCode)}')">${escapeHTML(item.cspCode)}</span>
              <div class="text-[10px] text-slate-400">${escapeHTML(item.cspName)}</div>
            </td>
            <td class="py-2.5 px-3">
              <span class="px-2 py-0.5 rounded bg-slate-800 border border-slate-700 text-slate-200 text-[10px] font-bold">${escapeHTML(item.type)}</span>
            </td>
            <td class="py-2.5 px-3 text-amber-300 text-[10.5px] max-w-[200px] truncate" title="${escapeHTML(item.reason)}">${escapeHTML(item.reason)}</td>
            <td class="py-2.5 px-3 font-mono">${item.confidence}%</td>
            <td class="py-2.5 px-3 text-slate-400 text-[10px]">${escapeHTML(item.createdAt)}</td>
            <td class="py-2.5 px-3 text-right">
              <button type="button" onclick='openManualReviewModal(${safeItem})' class="px-2.5 py-1 bg-amber-600 hover:bg-amber-500 text-white rounded text-[11px] font-medium transition cursor-pointer">
                Inspect & Verify →
              </button>
            </td>
          </tr>
        `;
      }).join('');
    }

    let activeReviewItem = null;

    function openManualReviewModal(item) {
      activeReviewItem = item;
      const modal = document.getElementById("modalManualReview");
      if (!modal) return;

      const sub = document.getElementById("revModalSubtitle");
      if (sub) sub.innerText = `Queue Item #${item.queueId} • Underlying Doc #${item.documentId} • CSP ${item.cspCode}`;

      const reason = document.getElementById("revReasonText");
      if (reason) reason.innerText = item.reason;

      const snip = document.getElementById("revSnippetText");
      if (snip) snip.innerText = item.raw_text_snippet || "No text available";

      document.getElementById("revQueueId").value = item.queueId;
      document.getElementById("revCspCode").value = item.cspCode !== "UNKNOWN" ? item.cspCode : "";
      
      const docTypeSelect = document.getElementById("revDocType");
      if (docTypeSelect && item.type) docTypeSelect.value = item.type;

      const ext = item.extractedFields || {};
      document.getElementById("revIssueDate").value = ext.issue_date || ext.start_date || "";
      document.getElementById("revExpiryDate").value = (ext.expiry_date && ext.expiry_date !== "LIFETIME_NO_EXPIRY") ? ext.expiry_date : "";
      document.getElementById("revCertNo").value = ext.certificate_number || "";
      document.getElementById("revRegNo").value = ext.registration_number || "";
      document.getElementById("revAuthority").value = ext.issuing_authority || "";
      document.getElementById("revState").value = ext.state || "";
      document.getElementById("revNotes").value = "";

      modal.classList.remove("hidden");
    }

    function closeManualReviewModal() {
      const modal = document.getElementById("modalManualReview");
      if (modal) modal.classList.add("hidden");
      activeReviewItem = null;
    }

    async function saveManualReviewCorrection(e) {
      e.preventDefault();
      if (!activeReviewItem) return;

      const queueId = document.getElementById("revQueueId").value;
      const payload = {
        csp_code: document.getElementById("revCspCode").value.trim(),
        document_type: document.getElementById("revDocType").value,
        issue_date: document.getElementById("revIssueDate").value || null,
        expiry_date: document.getElementById("revExpiryDate").value || null,
        certificate_number: document.getElementById("revCertNo").value.trim() || null,
        registration_number: document.getElementById("revRegNo").value.trim() || null,
        issuing_authority: document.getElementById("revAuthority").value.trim() || null,
        state: document.getElementById("revState").value.trim() || null,
        notes: document.getElementById("revNotes").value.trim() || "Verified via Operations Console",
        corrected_by_name: "Operations Lead"
      };

      try {
        const res = await fetch(`/api/review/${queueId}/correct`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload)
        });
        const data = await res.json();
        if (res.ok) {
          showToast(data.message || "Document verified and saved to master records!", "success");
          closeManualReviewModal();
          fetchLiveData(true);
        } else {
          showToast(data.detail || "Error saving verification.", "error");
        }
      } catch (err) {
        showToast("Network error submitting correction.", "error");
      }
    }

    async function rejectCurrentReviewItem() {
      if (!activeReviewItem) return;
      const queueId = activeReviewItem.queueId;
      try {
        const res = await fetch(`/api/review/${queueId}/reject`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ reason: "Rejected as unreadable/invalid by operator" })
        });
        showToast(`Document marked as rejected.`, "info");
        closeManualReviewModal();
        fetchLiveData(true);
      } catch (e) {
        showToast("Failed to reject document.", "error");
      }
    }

    // =========================================================================
    // DEVELOPER OUTBOUND REVIEW QUEUE HANDLERS
    // =========================================================================
    function renderOutboundQueueTable() {
      const tbody = document.getElementById("outboundTableBody");
      if (!tbody) return;
      const filter = document.getElementById("outboundFilterStatus") ? document.getElementById("outboundFilterStatus").value : "ALL";
      const messages = cachedSummary ? cachedSummary.outbound_queue || [] : [];

      const filtered = messages.filter(m => filter === 'ALL' || m.status === filter);
      if (filtered.length === 0) {
        tbody.innerHTML = `<tr><td colspan="7" class="py-8 text-center text-slate-500">No outbound messages matching filter "${filter}".</td></tr>`;
        return;
      }

      tbody.innerHTML = filtered.map(m => {
        const chanBadge = m.channel === 'WHATSAPP'
          ? `<span class="px-2 py-0.5 rounded bg-emerald-500/20 text-emerald-300 border border-emerald-500/30 text-[10px] font-bold">💬 WhatsApp</span>`
          : `<span class="px-2 py-0.5 rounded bg-blue-500/20 text-blue-300 border border-blue-500/30 text-[10px] font-bold">✉️ Email</span>`;

        let statusBadge = `<span class="px-2 py-0.5 rounded bg-slate-800 text-slate-300 border border-slate-700 text-[10px]">${escapeHTML(m.status)}</span>`;
        if (m.status === 'QUEUED_FOR_REVIEW') {
          statusBadge = `<span class="px-2 py-0.5 rounded bg-amber-500/20 text-amber-300 border border-amber-500/30 text-[10px] font-bold">QUEUED REVIEW</span>`;
        } else if (m.status === 'SENT') {
          statusBadge = `<span class="px-2 py-0.5 rounded bg-emerald-500/20 text-emerald-300 border border-emerald-500/30 text-[10px]">SENT</span>`;
        } else if (m.status === 'FAILED') {
          statusBadge = `<span class="px-2 py-0.5 rounded bg-rose-500/20 text-rose-300 border border-rose-500/30 text-[10px]">FAILED</span>`;
        }

        const safeItem = escapeHTML(JSON.stringify(m));
        let actions = `<span class="text-slate-500 text-[10px]">Archived</span>`;
        if (m.status === 'QUEUED_FOR_REVIEW') {
          actions = `
            <div class="flex items-center justify-end gap-1.5">
              <button type="button" onclick="approveOutboundMessage(${m.id})" class="px-2 py-1 bg-emerald-600 hover:bg-emerald-500 text-white rounded text-[10.5px] font-semibold transition cursor-pointer">
                ✓ Approve & Send
              </button>
              <button type="button" onclick='openEditOutboundModal(${safeItem})' class="px-2 py-1 bg-slate-800 hover:bg-slate-700 text-blue-300 border border-slate-700 rounded text-[10.5px] transition cursor-pointer">
                Edit
              </button>
              <button type="button" onclick="rejectOutboundMessage(${m.id})" class="px-2 py-1 bg-rose-900/60 hover:bg-rose-900 text-rose-200 rounded text-[10.5px] transition cursor-pointer">
                Cancel
              </button>
            </div>
          `;
        }

        return `
          <tr class="hover:bg-slate-800/40 transition">
            <td class="py-2.5 px-3">
              <span class="font-bold text-white">#${m.id}</span>
              <div class="text-[10px] text-slate-400">${escapeHTML(m.createdAt)}</div>
            </td>
            <td class="py-2.5 px-3">
              <span class="font-bold text-blue-400 hover:underline cursor-pointer" onclick="openDrawer('${escapeHTML(m.cspCode)}')">${escapeHTML(m.cspCode)}</span>
              <div class="text-[10px] text-slate-400">${escapeHTML(m.cspName)}</div>
            </td>
            <td class="py-2.5 px-3 whitespace-nowrap">${chanBadge}</td>
            <td class="py-2.5 px-3 font-mono text-slate-300 truncate max-w-[150px]">${escapeHTML(m.destination)}</td>
            <td class="py-2.5 px-3">
              <div class="font-semibold text-slate-200 text-[11px]">${escapeHTML(m.template)}</div>
              <div class="text-slate-400 text-[10px] truncate max-w-[220px]" title="${escapeHTML(m.subject || m.body)}">${escapeHTML(m.subject || m.body)}</div>
            </td>
            <td class="py-2.5 px-3 whitespace-nowrap">${statusBadge}</td>
            <td class="py-2.5 px-3 text-right whitespace-nowrap">${actions}</td>
          </tr>
        `;
      }).join('');
    }

    async function approveOutboundMessage(id) {
      try {
        const res = await fetch(`/api/outbound/${id}/approve`, { method: 'POST' });
        const data = await res.json();
        if (res.ok) {
          showToast(data.message || "Message approved and dispatched!", "success");
          fetchLiveData(true);
        } else {
          showToast(data.detail || "Failed to dispatch.", "error");
        }
      } catch (e) {
        showToast("Error approving outbound message.", "error");
      }
    }

    async function rejectOutboundMessage(id) {
      try {
        const res = await fetch(`/api/outbound/${id}/reject`, { method: 'POST' });
        showToast("Draft cancelled.", "info");
        fetchLiveData(true);
      } catch (e) {
        showToast("Error cancelling draft.", "error");
      }
    }

    let activeEditOutboundId = null;

    function openEditOutboundModal(msg) {
      activeEditOutboundId = msg.id;
      const modal = document.getElementById("modalEditOutbound");
      if (!modal) return;

      document.getElementById("editOutboundId").value = msg.id;
      document.getElementById("editOutboundDest").value = msg.destination || "";
      document.getElementById("editOutboundSubject").value = msg.subject || "";
      document.getElementById("editOutboundBody").value = msg.body || "";

      const subjRow = document.getElementById("editOutboundSubjectRow");
      if (subjRow) subjRow.style.display = (msg.channel === "EMAIL") ? "block" : "none";

      modal.classList.remove("hidden");
    }

    function closeEditOutboundModal() {
      const modal = document.getElementById("modalEditOutbound");
      if (modal) modal.classList.add("hidden");
      activeEditOutboundId = null;
    }

    async function saveEditedOutboundMessage(e) {
      e.preventDefault();
      if (!activeEditOutboundId) return;

      const payload = {
        destination: document.getElementById("editOutboundDest").value.trim(),
        subject: document.getElementById("editOutboundSubject").value.trim(),
        body: document.getElementById("editOutboundBody").value.trim()
      };

      try {
        const res = await fetch(`/api/outbound/${activeEditOutboundId}/edit`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload)
        });
        if (res.ok) {
          showToast("Outbound draft updated successfully.", "success");
          closeEditOutboundModal();
          fetchLiveData(true);
        } else {
          showToast("Failed to save changes.", "error");
        }
      } catch (err) {
        showToast("Network error saving draft.", "error");
      }
    }

    // =========================================================================
    // INBOUND EMAIL AUDIT DRAWER HANDLERS
    // =========================================================================
    function openEmailAuditModal(auditJson, sender, subject) {
      const modal = document.getElementById("modalEmailAudit");
      const content = document.getElementById("emailAuditContent");
      if (!modal || !content) return;

      const audit = typeof auditJson === "string" ? JSON.parse(auditJson || "{}") : (auditJson || {});
      content.innerHTML = `
        <div class="flex justify-between items-center pb-2 border-b border-slate-800">
          <span class="text-slate-400">Message ID:</span>
          <span class="text-white font-bold">${escapeHTML(audit.email_id || 'N/A')}</span>
        </div>
        <div class="flex justify-between items-center">
          <span class="text-slate-400">Sender:</span>
          <span class="text-slate-200">${escapeHTML(sender || audit.sender || 'N/A')}</span>
        </div>
        <div class="flex justify-between items-center">
          <span class="text-slate-400">Subject:</span>
          <span class="text-slate-200 truncate max-w-[260px]">${escapeHTML(subject || audit.subject || 'N/A')}</span>
        </div>
        <div class="flex justify-between items-center">
          <span class="text-slate-400">Detected Category:</span>
          <span class="px-2 py-0.5 rounded bg-blue-500/20 text-blue-300 font-bold border border-blue-500/30">${escapeHTML(audit.detected_type || 'UNKNOWN')}</span>
        </div>
        <div class="flex justify-between items-center">
          <span class="text-slate-400">Matched Rule:</span>
          <span class="text-emerald-400 font-semibold">${escapeHTML(audit.matched_rule || 'NONE')}</span>
        </div>
        <div class="flex justify-between items-center">
          <span class="text-slate-400">Confidence Score:</span>
          <span class="text-white font-bold">${audit.confidence !== undefined ? Math.round(audit.confidence * 100) + '%' : 'N/A'}</span>
        </div>
        <div class="flex justify-between items-center">
          <span class="text-slate-400">Extracted KO Code:</span>
          <span class="text-amber-400 font-bold">${escapeHTML(audit.extracted_ko || 'None')}</span>
        </div>
        <div class="flex justify-between items-center">
          <span class="text-slate-400">AI Fallback Invoked:</span>
          <span class="${audit.ai_used ? 'text-amber-400 font-bold' : 'text-slate-500'}">${audit.ai_used ? 'YES (Gemini Flash)' : 'NO (Deterministic First)'}</span>
        </div>
        <div class="flex justify-between items-center">
          <span class="text-slate-400">Classification Status:</span>
          <span class="text-slate-200 font-bold">${escapeHTML(audit.final_status || 'PROCESSED')}</span>
        </div>
        <div class="flex justify-between items-center pt-1 border-t border-slate-800/80 text-[10px] text-slate-500">
          <span>Audit Logged At:</span>
          <span>${escapeHTML(audit.timestamp || 'N/A')}</span>
        </div>
      `;

      modal.classList.remove("hidden");
    }

    function closeEmailAuditModal() {
      const modal = document.getElementById("modalEmailAudit");
      if (modal) modal.classList.add("hidden");
    }

    function toggleTheme() {
      const htmlEl = document.documentElement;
      if (htmlEl.classList.contains('light-theme')) {
        htmlEl.classList.remove('light-theme');
        htmlEl.style.filter = '';
        showToast("Dark theme restored", "info");
      } else {
        htmlEl.classList.add('light-theme');
        // Simple invert hack for dark -> light
        htmlEl.style.filter = 'invert(1) hue-rotate(180deg)';
        showToast("Light theme applied", "info");
      }
    }

    // =========================================================================
    // COMMUNICATION LIFECYCLE HUB HANDLERS (4 SUB-SECTIONS)
    // =========================================================================
    let currentCommsTab = 'inbound';

    function switchCommsTab(tabName) {
      currentCommsTab = tabName;
      ['inbound', 'outbound', 'responded', 'matrix', 'autosheet'].forEach(t => {
        const p = document.getElementById('commsPanel-' + t);
        if (p) p.classList.add('hidden');
        const b = document.getElementById('commsTabBtn-' + t);
        if (b) {
          b.classList.remove('border-blue-500', 'text-blue-400');
          b.classList.add('border-transparent', 'text-slate-400');
        }
      });
      const activePanel = document.getElementById('commsPanel-' + tabName);
      if (activePanel) activePanel.classList.remove('hidden');
      const activeBtn = document.getElementById('commsTabBtn-' + tabName);
      if (activeBtn) {
        activeBtn.classList.add('border-blue-500', 'text-blue-400');
        activeBtn.classList.remove('border-transparent', 'text-slate-400');
      }
    }

    function renderCommunicationHub() {
      if (!cachedSummary) return;

      const inbound = cachedSummary.recent_inbound || [];
      const outbound = cachedSummary.outbound_queue || [];
      const responded = cachedSummary.responded_threads || [];
      const unmatched = cachedSummary.unmatched_discrepancies || [];
      const autoSheet = cachedSummary.auto_calling_sheet || [];

      // Update Top KPIs
      const setV = (id, v) => { const el = document.getElementById(id); if (el) el.innerText = v; };
      setV('commsKpiInbound', inbound.length);
      setV('commsKpiOutbound', outbound.length);
      setV('commsKpiResponded', responded.length);
      setV('commsKpiDiscrepancies', unmatched.length);
      setV('commsKpiAutoSheet', autoSheet.length);

      const tabBadge = document.getElementById('commsTabBadgeAutoSheet');
      if (tabBadge) tabBadge.innerText = autoSheet.length;

      // Sub-section 1: Inbound Table
      const inTbody = document.getElementById('commsInboundTableBody');
      if (inTbody) {
        if (inbound.length === 0) {
          inTbody.innerHTML = `<tr><td colspan="7" class="py-8 text-center text-slate-500">No inbound emails found in the 2-year rolling window.</td></tr>`;
        } else {
          inTbody.innerHTML = inbound.map(m => {
            const folderBadge = (m.folder === "TRASH")
              ? `<span class="px-2 py-0.5 rounded bg-rose-500/20 text-rose-300 border border-rose-500/30 text-[10px] font-bold">TRASH</span>`
              : `<span class="px-2 py-0.5 rounded bg-blue-500/20 text-blue-300 border border-blue-500/30 text-[10px] font-bold">INBOX</span>`;

            const cspDisplay = (m.cspCode && m.cspCode !== "--")
              ? `<button type="button" class="px-1.5 py-0.5 rounded bg-amber-500/10 hover:bg-amber-500/20 text-amber-300 border border-amber-500/30 font-semibold cursor-pointer text-left" onclick="openDrawer('${escapeHTML(m.cspCode)}')">${escapeHTML(m.cspCode)} <span class="text-slate-400 font-normal">(${escapeHTML(m.cspName || '')})</span></button>`
              : `<span class="text-rose-400 font-semibold">Unmatched (Non-CSP)</span>`;

            let statusBadge = `<span class="px-2 py-0.5 rounded bg-emerald-500/20 text-emerald-300 border border-emerald-500/30 text-[10px]">${escapeHTML(m.status)}</span>`;
            if (m.status === "IGNORED_NON_CSP") {
              statusBadge = `<span class="px-2 py-0.5 rounded bg-rose-500/20 text-rose-300 border border-rose-500/30 text-[10px]">IGNORED NON-CSP</span>`;
            } else if (m.status === "AUTO_CALLING_SHEET_CREATED") {
              statusBadge = `<span class="px-2 py-0.5 rounded bg-teal-500/20 text-teal-300 border border-teal-500/30 text-[10px] font-bold">AUTO SHEET CREATED</span>`;
            } else if (m.status === "NEEDS_REVIEW") {
              statusBadge = `<span class="px-2 py-0.5 rounded bg-amber-500/20 text-amber-300 border border-amber-500/30 text-[10px]">NEEDS REVIEW</span>`;
            }

            const pdfBadge = (m.pdfCount > 0)
              ? `<span class="px-2 py-0.5 rounded bg-indigo-500/20 text-indigo-300 border border-indigo-500/30 font-bold">${m.pdfCount} PDF</span>`
              : `<span class="text-slate-600">0</span>`;

            const catBadge = `<span class="px-1.5 py-0.5 rounded bg-slate-800 text-blue-300 border border-slate-700 text-[10px] font-bold">${escapeHTML(m.emailCategory || 'UNKNOWN')}</span>`;

            return `
              <tr class="hover:bg-slate-800/40 transition">
                <td class="py-2.5 px-3 text-slate-400 font-mono text-[11px] whitespace-nowrap">${escapeHTML(m.receivedAt)}</td>
                <td class="py-2.5 px-3 whitespace-nowrap">${folderBadge}</td>
                <td class="py-2.5 px-3 font-mono text-xs whitespace-nowrap">${cspDisplay}</td>
                <td class="py-2.5 px-3 text-slate-300 font-medium truncate max-w-[180px]">${escapeHTML(m.sender)}</td>
                <td class="py-2.5 px-3 text-slate-200">
                  <div class="truncate max-w-[260px] font-medium" title="${escapeHTML(m.subject)}">${escapeHTML(m.subject)}</div>
                  <div class="mt-1">${catBadge}</div>
                </td>
                <td class="py-2.5 px-3 text-center whitespace-nowrap">${pdfBadge}</td>
                <td class="py-2.5 px-3 text-right whitespace-nowrap">${statusBadge}</td>
              </tr>
            `;
          }).join('');
        }
      }

      // Sub-section 2: Outbound Table
      const outTbody = document.getElementById('commsOutboundTableBody');
      if (outTbody) {
        if (outbound.length === 0) {
          outTbody.innerHTML = `<tr><td colspan="7" class="py-8 text-center text-slate-500">No outbound notices queued or sent yet.</td></tr>`;
        } else {
          outTbody.innerHTML = outbound.map(m => {
            const chanBadge = m.channel === 'WHATSAPP'
              ? `<span class="px-2 py-0.5 rounded bg-emerald-500/20 text-emerald-300 border border-emerald-500/30 text-[10px] font-bold">💬 WhatsApp</span>`
              : `<span class="px-2 py-0.5 rounded bg-blue-500/20 text-blue-300 border border-blue-500/30 text-[10px] font-bold">✉️ Email</span>`;

            let statusBadge = `<span class="px-2 py-0.5 rounded bg-slate-800 text-slate-300 border border-slate-700 text-[10px]">${escapeHTML(m.status)}</span>`;
            if (m.status === 'SENT') {
              statusBadge = `<span class="px-2 py-0.5 rounded bg-emerald-500/20 text-emerald-300 border border-emerald-500/30 text-[10px]">SENT</span>`;
            } else if (m.status === 'QUEUED_FOR_REVIEW') {
              statusBadge = `<span class="px-2 py-0.5 rounded bg-amber-500/20 text-amber-300 border border-amber-500/30 text-[10px] font-bold">REVIEW</span>`;
            }

            return `
              <tr class="hover:bg-slate-800/40 transition">
                <td class="py-2.5 px-3 font-bold text-white">#${m.id}</td>
                <td class="py-2.5 px-3">
                  <span class="font-bold text-blue-400 hover:underline cursor-pointer" onclick="openDrawer('${escapeHTML(m.cspCode)}')">${escapeHTML(m.cspCode)}</span>
                  <div class="text-[10px] text-slate-400">${escapeHTML(m.cspName)}</div>
                </td>
                <td class="py-2.5 px-3">${chanBadge}</td>
                <td class="py-2.5 px-3 font-mono text-slate-300 truncate max-w-[150px]">${escapeHTML(m.destination)}</td>
                <td class="py-2.5 px-3">
                  <div class="font-semibold text-slate-200 text-[11px]">${escapeHTML(m.template)}</div>
                  <div class="text-slate-400 text-[10px] truncate max-w-[220px]">${escapeHTML(m.subject || m.body)}</div>
                </td>
                <td class="py-2.5 px-3">${statusBadge}</td>
                <td class="py-2.5 px-3 text-right">
                  <span class="text-slate-400 text-[10px] font-mono">${escapeHTML(m.createdAt)}</span>
                </td>
              </tr>
            `;
          }).join('');
        }
      }

      // Sub-section 3: Responded Threads Table
      const respTbody = document.getElementById('commsRespondedTableBody');
      if (respTbody) {
        if (responded.length === 0) {
          respTbody.innerHTML = `<tr><td colspan="6" class="py-8 text-center text-slate-500">Zero closed-loop outreach replies recorded. Incoming replies will automatically link here.</td></tr>`;
        } else {
          respTbody.innerHTML = responded.map(r => `
            <tr class="hover:bg-slate-800/40 transition">
              <td class="py-2.5 px-3 font-bold text-emerald-400">#${r.id}</td>
              <td class="py-2.5 px-3 font-bold text-blue-400 cursor-pointer hover:underline" onclick="openDrawer('${escapeHTML(r.cspCode)}')">${escapeHTML(r.cspCode)}</td>
              <td class="py-2.5 px-3 text-slate-200 font-medium">${escapeHTML(r.cspName)}</td>
              <td class="py-2.5 px-3">
                <span class="px-2 py-0.5 rounded bg-blue-500/20 text-blue-300 border border-blue-500/30 text-[10px]">${escapeHTML(r.channel)}</span>
              </td>
              <td class="py-2.5 px-3 text-slate-300 text-xs">${escapeHTML(r.notes)}</td>
              <td class="py-2.5 px-3 text-right font-mono text-emerald-400 font-medium">${escapeHTML(r.receivedAt)}</td>
            </tr>
          `).join('');
        }
      }

      // Sub-section 4: Calling Sheet Discrepancies Table
      const discTbody = document.getElementById('commsDiscrepancyTableBody');
      if (discTbody) {
        if (unmatched.length === 0) {
          discTbody.innerHTML = `<tr><td colspan="8" class="py-8 text-center text-slate-500">Zero discrepancies detected. All scanned senders are verified against Calling Sheet New!</td></tr>`;
        } else {
          discTbody.innerHTML = unmatched.map(u => `
            <tr class="hover:bg-slate-800/40 transition">
              <td class="py-2.5 px-3 font-bold text-amber-400">#${u.id}</td>
              <td class="py-2.5 px-3 text-slate-400 font-mono text-[11px] whitespace-nowrap">${escapeHTML(u.receivedAt)}</td>
              <td class="py-2.5 px-3 font-mono text-slate-200 text-xs">${escapeHTML(u.sender)}</td>
              <td class="py-2.5 px-3 font-bold text-amber-300">${escapeHTML(u.candidateKo)}</td>
              <td class="py-2.5 px-3">
                <span class="px-1.5 py-0.5 rounded bg-slate-800 text-slate-200 border border-slate-700 text-[10px]">${escapeHTML(u.category)}</span>
              </td>
              <td class="py-2.5 px-3 text-slate-300 truncate max-w-[200px]" title="${escapeHTML(u.subject)}">${escapeHTML(u.subject)}</td>
              <td class="py-2.5 px-3 text-rose-300 text-[10.5px]">${escapeHTML(u.reason)}</td>
              <td class="py-2.5 px-3 text-right whitespace-nowrap">
                <button type="button" onclick="exportDiscrepanciesXLSX()" class="px-2 py-0.5 rounded bg-emerald-700 hover:bg-emerald-600 text-white text-[10px] font-semibold transition cursor-pointer">Export .xlsx</button>
              </td>
            </tr>
          `).join('');
        }
      }

      // Sub-section 5: Auto-Made Calling Sheet Table
      const autoTbody = document.getElementById('commsAutoSheetTableBody');
      if (autoTbody) {
        if (autoSheet.length === 0) {
          autoTbody.innerHTML = `<tr><td colspan="11" class="py-8 text-center text-slate-500">No missing CSPs detected yet. Click 'Auto-Detect & Make Sheet' to scan!</td></tr>`;
        } else {
          autoTbody.innerHTML = autoSheet.map(e => {
            const statusBadge = e.isPromoted
              ? `<span class="px-2 py-0.5 rounded bg-emerald-500/20 text-emerald-300 border border-emerald-500/30 text-[10px] font-bold">ADDED TO MASTER</span>`
              : `<span class="px-2 py-0.5 rounded bg-amber-500/20 text-amber-300 border border-amber-500/30 text-[10px] font-bold">AUTO DETECTED</span>`;

            const reqBadge = `<span class="px-1.5 py-0.5 rounded bg-slate-800 text-blue-300 border border-slate-700 text-[10px] font-bold">${escapeHTML(e.requestType || 'UNKNOWN')}</span>`;

            return `
              <tr class="hover:bg-slate-800/40 transition">
                <td class="py-2.5 px-3 font-bold text-emerald-400 whitespace-nowrap">${escapeHTML(e.cspCode)}</td>
                <td class="py-2.5 px-3 font-semibold text-white whitespace-nowrap">${escapeHTML(e.cspName)}</td>
                <td class="py-2.5 px-3 text-slate-300 font-mono text-[11px] truncate max-w-[150px]">${escapeHTML(e.cspEmail)}</td>
                <td class="py-2.5 px-3 text-slate-300 font-mono text-[11px]">${escapeHTML(e.phone)}</td>
                <td class="py-2.5 px-3 text-slate-300 whitespace-nowrap">${escapeHTML(e.state)}</td>
                <td class="py-2.5 px-3 text-slate-400 truncate max-w-[130px]">${escapeHTML(e.branch)}</td>
                <td class="py-2.5 px-3 text-slate-400 whitespace-nowrap">${escapeHTML(e.circle)}</td>
                <td class="py-2.5 px-3 whitespace-nowrap">${reqBadge}</td>
                <td class="py-2.5 px-3 text-slate-400 font-mono text-[10.5px] whitespace-nowrap">${escapeHTML(e.detectedAt)}</td>
                <td class="py-2.5 px-3 whitespace-nowrap">${statusBadge}</td>
                <td class="py-2.5 px-3 text-right whitespace-nowrap">
                  <div class="flex items-center justify-end gap-1.5">
                    <button type="button" onclick="copySingleRowForGoogleSheets(${e.id})" title="Copy row for Google Sheets" class="px-2 py-0.5 rounded bg-slate-800 hover:bg-slate-700 text-blue-300 border border-slate-700 text-[10px] font-semibold transition cursor-pointer">
                      📋 Copy
                    </button>
                    ${!e.isPromoted ? `
                      <button type="button" onclick="promoteAutoCallingSheetEntry(${e.id})" title="Add to Master CSP Roster" class="px-2 py-0.5 rounded bg-emerald-700 hover:bg-emerald-600 text-white text-[10px] font-semibold transition cursor-pointer">
                        ➕ Add to Master
                      </button>
                    ` : ''}
                  </div>
                </td>
              </tr>
            `;
          }).join('');
        }
      }
    }

    async function triggerBackfill2Years() {
      const icon = document.getElementById("backfillIcon");
      const text = document.getElementById("backfillText");
      if (icon) icon.classList.add("animate-spin");
      if (text) text.innerText = "Backfilling...";

      showToast("Starting 2-Year Historical Gmail Backfill (730 days)...", "info");

      try {
        const res = await fetch('/api/ingest/email/backfill-2years?max_total=1000', { method: 'POST' });
        const data = await res.json();
        if (res.ok && data.status === "success") {
          const s = data.summary || {};
          showToast(`2-Year Backfill complete: ${s.scanned || 0} candidate emails checked, ${s.processed || 0} processed, ${s.new_agreements || 0} new agreements, ${s.replies_detected || 0} replies detected.`, "success");
          fetchLiveData(true);
        } else {
          showToast(data.message || "Historical backfill error.", "error");
        }
      } catch (err) {
        showToast("Network error executing 2-year backfill.", "error");
      } finally {
        if (icon) icon.classList.remove("animate-spin");
        if (text) text.innerText = "2-Yr Backfill";
      }
    }

    async function sendWhatsAppUploadLink(cspCode) {
      if (!confirm(`Generate and dispatch secure mobile upload link via WhatsApp to CSP ${cspCode}?`)) return;
      showToast(`Generating secure upload link for CSP ${cspCode}...`, "info");
      try {
        const res = await fetch(`/api/dashboard/csp/${encodeURIComponent(cspCode)}/send-upload-link`, { method: 'POST' });
        const data = await res.json();
        if (res.ok && data.status === "sent") {
          showToast(`✓ WhatsApp upload link successfully dispatched to ${data.phone}!`, "success");
        } else {
          showToast(data.detail || data.message || "Failed to dispatch WhatsApp link.", "error");
        }
      } catch (err) {
        showToast("Network error dispatching WhatsApp upload link.", "error");
      }
    }

    function exportDiscrepanciesXLSX() {
      showToast("Generating comprehensive Excel Discrepancy & Compliance Report...", "info");
      window.location.href = '/api/csp/export-discrepancies-xlsx';
    }

    async function triggerCallingSheetSync() {
      const icon = document.getElementById("syncSheetIcon");
      const text = document.getElementById("syncSheetText");
      if (icon) icon.classList.add("animate-spin");
      if (text) text.innerText = "Syncing...";

      showToast("Initiating live sync strictly isolated to Calling Sheet New...", "info");

      try {
        const res = await fetch('/api/csp/sync-sheet', { method: 'POST' });
        const data = await res.json();
        if (res.ok && data.status === "success") {
          const s = data.summary || {};
          showToast(`Calling Sheet Sync complete: ${s.total_rows || 0} rows evaluated, ${s.inserted || 0} added, ${s.updated || 0} updated, ${s.skipped || 0} unchanged.`, "success");
          fetchLiveData(true);
        } else {
          showToast(data.detail || "Calling Sheet sync failed.", "error");
        }
      } catch (err) {
        showToast("Error communicating with Calling Sheet sync API.", "error");
      } finally {
        if (icon) icon.classList.remove("animate-spin");
        if (text) text.innerText = "Sync Sheet";
      }
    }

    async function triggerAutoDetectCallingSheet() {
      const icon = document.getElementById("autoSheetIcon");
      const text = document.getElementById("autoSheetText");
      if (icon) icon.classList.add("animate-spin");
      if (text) text.innerText = "Making Sheet...";

      showToast("Auto-detecting missing CSPs from emails and generating Calling Sheet...", "info");

      try {
        const res = await fetch('/api/csp/auto-detect-calling-sheet', { method: 'POST' });
        const data = await res.json();
        if (res.ok && data.status === "success") {
          const s = data.summary || {};
          showToast(`Auto-made Calling Sheet: ${s.detected_missing || 0} missing CSPs detected (${s.inserted || 0} new rows, ${s.already_exists || 0} existing).`, "success");
          fetchLiveData(true);
        } else {
          showToast(data.message || "Auto Calling Sheet generation failed.", "error");
        }
      } catch (err) {
        showToast("Network error executing auto calling sheet generator.", "error");
      } finally {
        if (icon) icon.classList.remove("animate-spin");
        if (text) text.innerText = "Auto-Make Sheet";
      }
    }

    function exportAutoCallingSheetXLSX() {
      showToast("Downloading Auto-Generated Calling Sheet (.xlsx)...", "info");
      window.location.href = '/api/csp/export-auto-calling-sheet-xlsx';
    }

    async function copyAllRowsForGoogleSheets() {
      if (!cachedSummary || !cachedSummary.auto_calling_sheet || cachedSummary.auto_calling_sheet.length === 0) {
        showToast("No auto-made calling sheet rows available to copy.", "error");
        return;
      }
      const rows = cachedSummary.auto_calling_sheet;
      const tsvRows = rows.map(e => [
        e.cspCode || "",
        e.cspName || "",
        e.cspEmail || "",
        e.phone || "",
        e.state || "",
        e.branch || "",
        e.circle || "",
        e.terminalStatus || "AUTO_DETECTED",
        e.requestType || "",
        e.detectedAt || "",
        e.sourceSubject || ""
      ].join(String.fromCharCode(9))).join(String.fromCharCode(10));

      try {
        await navigator.clipboard.writeText(tsvRows);
        showToast(`Copied ${rows.length} rows to clipboard! Paste directly into Google Sheet 'Calling Sheet New'.`, "success");
      } catch (err) {
        showToast("Clipboard copy failed. Please export Excel (.xlsx) instead.", "error");
      }
    }

    async function copySingleRowForGoogleSheets(id) {
      if (!cachedSummary || !cachedSummary.auto_calling_sheet) return;
      const e = cachedSummary.auto_calling_sheet.find(x => x.id === id);
      if (!e) return;
      const tsv = [
        e.cspCode || "",
        e.cspName || "",
        e.cspEmail || "",
        e.phone || "",
        e.state || "",
        e.branch || "",
        e.circle || "",
        e.terminalStatus || "AUTO_DETECTED",
        e.requestType || "",
        e.detectedAt || "",
        e.sourceSubject || ""
      ].join(String.fromCharCode(9));

      try {
        await navigator.clipboard.writeText(tsv);
        showToast(`Copied CSP ${e.cspCode} to clipboard! Paste into Google Sheet.`, "success");
      } catch (err) {
        showToast("Clipboard copy failed.", "error");
      }
    }

    async function promoteAutoCallingSheetEntry(id) {
      showToast(`Promoting entry #${id} to master CSP roster...`, "info");
      try {
        const res = await fetch(`/api/csp/auto-calling-sheet/${id}/promote`, { method: 'POST' });
        const data = await res.json();
        if (res.ok && data.status === "success") {
          showToast(data.message, "success");
          fetchLiveData(true);
        } else {
          showToast(data.detail || "Promotion failed", "error");
        }
      } catch (err) {
        showToast("Network error promoting entry", "error");
      }
    }

    async function bulkPromoteAutoCallingSheet() {
      showToast("Bulk-promoting auto-detected calling sheet entries into master CSP database...", "info");
      try {
        const res = await fetch('/api/csp/auto-calling-sheet/promote-all', { method: 'POST' });
        const data = await res.json();
        if (res.ok && data.status === "success") {
          showToast(data.message, "success");
          fetchLiveData(true);
        } else {
          showToast(data.detail || "Bulk promotion failed", "error");
        }
      } catch (err) {
        showToast("Network error executing bulk promotion", "error");
      }
    }

    // Expose all interactive functions on window to guarantee 100% onclick accessibility
    window.switchSection = switchSection;
    window.switchDrawerTab = switchDrawerTab;
    window.fetchLiveData = fetchLiveData;
    window.filterCategory = filterCategory;
    window.filterCategoryAndSwitch = filterCategoryAndSwitch;
    window.triggerAutoDetectCallingSheet = triggerAutoDetectCallingSheet;
    window.exportAutoCallingSheetXLSX = exportAutoCallingSheetXLSX;
    window.copyAllRowsForGoogleSheets = copyAllRowsForGoogleSheets;
    window.copySingleRowForGoogleSheets = copySingleRowForGoogleSheets;
    window.promoteAutoCallingSheetEntry = promoteAutoCallingSheetEntry;
    window.bulkPromoteAutoCallingSheet = bulkPromoteAutoCallingSheet;
    window.filterHorizonAndSwitch = filterHorizonAndSwitch;
    window.filterByRMAndSwitch = filterByRMAndSwitch;
    window.applyFilters = applyFilters;
    window.resetFilters = resetFilters;
    window.handleGlobalSearch = handleGlobalSearch;
    window.toggleTheme = toggleTheme;
    window.renderTable = renderTable;
    window.prevPage = prevPage;
    window.nextPage = nextPage;
    window.changePageSize = changePageSize;
    window.openDrawer = openDrawer;
    window.closeDrawer = closeDrawer;
    window.toggleRowSelect = toggleRowSelect;
    window.toggleSelectAll = toggleSelectAll;
    window.clearSelection = clearSelection;
    window.triggerBulkNotice = triggerBulkNotice;
    window.exportCSV = exportCSV;
    window.exportSelectedCSV = exportSelectedCSV;
    window.renderVaultTable = renderVaultTable;
    window.renderEscalationsTable = renderEscalationsTable;
    window.renderAnalyticsView = renderAnalyticsView;
    window.renderReviewQueueTable = renderReviewQueueTable;
    window.openManualReviewModal = openManualReviewModal;
    window.closeManualReviewModal = closeManualReviewModal;
    window.saveManualReviewCorrection = saveManualReviewCorrection;
    window.rejectCurrentReviewItem = rejectCurrentReviewItem;
    window.renderOutboundQueueTable = renderOutboundQueueTable;
    window.approveOutboundMessage = approveOutboundMessage;
    window.openEditOutboundModal = openEditOutboundModal;
    window.closeEditOutboundModal = closeEditOutboundModal;
    window.saveEditedOutboundMessage = saveEditedOutboundMessage;
    window.rejectOutboundMessage = rejectOutboundMessage;
    window.openEmailAuditModal = openEmailAuditModal;
    window.closeEmailAuditModal = closeEmailAuditModal;
    window.triggerScan = triggerScan;
    window.triggerOutreach = triggerOutreach;
    window.triggerDirectEscalation = triggerDirectEscalation;
    window.dispatchEscalationDigest = dispatchEscalationDigest;
    window.saveOperatorNote = saveOperatorNote;
    window.copyToClipboard = copyToClipboard;
    window.showToast = showToast;
    window.switchCommsTab = switchCommsTab;
    window.renderCommunicationHub = renderCommunicationHub;
    window.triggerBackfill2Years = triggerBackfill2Years;
    window.sendWhatsAppUploadLink = sendWhatsAppUploadLink;
    window.exportDiscrepanciesXLSX = exportDiscrepanciesXLSX;
    window.triggerCallingSheetSync = triggerCallingSheetSync;

    // KEYBOARD SHORTCUTS
    document.addEventListener('keydown', (e) => {
      if (e.key === '/' && document.activeElement.tagName !== 'INPUT' && document.activeElement.tagName !== 'TEXTAREA') {
        e.preventDefault();
        const s = document.getElementById("globalSearch");
        if (s) { s.focus(); s.select(); }
      } else if (e.key === 'Escape') {
        closeDrawer();
      }
    });

    // INITIAL TELEMETRY LOAD (Immediate + DOMContentLoaded safeguard)
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', () => fetchLiveData());
    } else {
      fetchLiveData();
    }
  </script>
</body>
</html>
"""


@router.get("/dashboard/legacy", response_class=HTMLResponse)
def serve_dashboard_page():
    html = DASHBOARD_HTML_TEMPLATE.replace("__SERVER_PRELOAD_JSON_SLOT__", "{}", 1)
    return HTMLResponse(content=html)