"""
app/reports_xlsx.py
Automated Excel (.xlsx) Discrepancy & Compliance Report Generator.
Produces a multi-tab workbook using openpyxl:
1. 'Calling Sheet Discrepancies': Senders/emails who sent documents or terminal requests
   but are NOT registered in Calling Sheet New.
2. 'Calling Sheet Compliance Status': Real-time compliance breakdown of all CSPs
   seeded from Calling Sheet New (agreements, PVR, Character Cert, IIBF, categories).
3. 'Outreach & Reply Tracking': History of notices sent and CSP responses received.
4. 'Executive Summary': High-level metrics for compliance officers and operations teams.
"""

import io
import json
from datetime import datetime, timezone, date
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter
from sqlalchemy.orm import Session

from .models import CSP, Agreement, Document, DocumentStatus, InboundMessage, OutboundMessage, AgreementEvent, AutoCallingSheetEntry
from .email_ingest import evaluate_csp_category


def create_header_style(bg_hex: str = "1F4E78", text_hex: str = "FFFFFF"):
    """Generates standard corporate header style."""
    font = Font(name="Calibri", size=11, bold=True, color=text_hex)
    fill = PatternFill(start_color=bg_hex, end_color=bg_hex, fill_type="solid")
    alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    border = Border(
        left=Side(style="thin", color="D9D9D9"),
        right=Side(style="thin", color="D9D9D9"),
        top=Side(style="thin", color="D9D9D9"),
        bottom=Side(style="medium", color="000000")
    )
    return font, fill, alignment, border


def style_worksheet_table(ws, title: str, subtitle: str, headers: list[str], header_bg: str = "1F4E78"):
    """Adds a standardized corporate header block and column headers."""
    # Title Block
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=len(headers))
    title_cell = ws.cell(row=1, column=1, value=title)
    title_cell.font = Font(name="Calibri", size=15, bold=True, color="1F4E78")
    title_cell.alignment = Alignment(horizontal="left", vertical="center")
    ws.row_dimensions[1].height = 28

    # Subtitle Block
    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=len(headers))
    sub_cell = ws.cell(row=2, column=1, value=f"{subtitle} | Generated on: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    sub_cell.font = Font(name="Calibri", size=10, italic=True, color="595959")
    sub_cell.alignment = Alignment(horizontal="left", vertical="center")
    ws.row_dimensions[2].height = 20

    ws.row_dimensions[3].height = 10  # spacing row

    # Column Headers (Row 4)
    font, fill, alignment, border = create_header_style(bg_hex=header_bg)
    ws.row_dimensions[4].height = 26
    for col_idx, h_text in enumerate(headers, 1):
        cell = ws.cell(row=4, column=col_idx, value=h_text)
        cell.font = font
        cell.fill = fill
        cell.alignment = alignment
        cell.border = border


def auto_fit_columns(ws, min_col=1, max_col=None, max_width_limit=50):
    """Adjusts column widths based on content length."""
    if max_col is None:
        max_col = ws.max_column

    for col in range(min_col, max_col + 1):
        max_len = 0
        col_letter = get_column_letter(col)
        for row in range(4, ws.max_row + 1):
            val = ws.cell(row=row, column=col).value
            if val is not None:
                max_len = max(max_len, len(str(val)))
        ws.column_dimensions[col_letter].width = min(max(max_len + 4, 12), max_width_limit)


def generate_discrepancy_workbook(db: Session) -> io.BytesIO:
    """
    Builds the complete multi-tab compliance and discrepancy Excel report.
    Returns in-memory BytesIO buffer ready for HTTP streaming.
    """
    wb = Workbook()

    # -------------------------------------------------------------
    # TAB 1: EXECUTIVE SUMMARY
    # -------------------------------------------------------------
    ws_exec = wb.active
    ws_exec.title = "Executive Summary"
    ws_exec.views.sheetView[0].showGridLines = True

    # -------------------------------------------------------------
    # TAB 2: CALLING SHEET DISCREPANCIES (UNMATCHED SENDERS)
    # -------------------------------------------------------------
    ws_unmatched = wb.create_sheet(title="Calling Sheet Discrepancies")
    ws_unmatched.views.sheetView[0].showGridLines = True

    unmatched_headers = [
        "Inbound ID",
        "External Message ID",
        "Date Received",
        "Sender Email / Name",
        "Subject",
        "Classified Category",
        "Extracted Candidate KO",
        "Folder",
        "Status Reason",
        "Recommended Action"
    ]
    style_worksheet_table(
        ws_unmatched,
        title="Calling Sheet Master Data Discrepancy Report",
        subtitle="Inbound Emails with CSP Documents/Requests NOT Found in Calling Sheet New",
        headers=unmatched_headers,
        header_bg="C00000"  # Crimson red for discrepancies
    )

    unmatched_msgs = db.query(InboundMessage).filter(
        (InboundMessage.status == "IGNORED_NON_CSP") |
        (InboundMessage.status == "NEEDS_REVIEW")
    ).order_by(InboundMessage.received_at.desc()).all()

    thin_border = Border(
        left=Side(style="thin", color="E0E0E0"),
        right=Side(style="thin", color="E0E0E0"),
        top=Side(style="thin", color="E0E0E0"),
        bottom=Side(style="thin", color="E0E0E0")
    )
    zebra_fill = PatternFill(start_color="F9F9F9", end_color="F9F9F9", fill_type="solid")

    row_idx = 5
    for msg in unmatched_msgs:
        meta = {}
        if msg.error_message:
            try:
                meta = json.loads(msg.error_message)
            except Exception:
                pass

        audit = msg.classification_audit or {}
        extracted_ko = audit.get("extracted_ko") or meta.get("csp_code") or "None"
        reason = meta.get("reason", "Sender / content not registered in Calling Sheet master data")
        cat_val = msg.email_category.value if hasattr(msg.email_category, 'value') else str(msg.email_category)
        date_str = msg.received_at.strftime("%Y-%m-%d %H:%M") if msg.received_at else "N/A"

        row_vals = [
            msg.id,
            msg.external_message_id,
            date_str,
            msg.sender,
            msg.subject,
            cat_val,
            extracted_ko,
            meta.get("folder", "INBOX"),
            reason,
            "Verify with Operations if CSP is newly onboarded, then add to Calling Sheet New"
        ]

        ws_unmatched.row_dimensions[row_idx].height = 22
        for col_idx, val in enumerate(row_vals, 1):
            cell = ws_unmatched.cell(row=row_idx, column=col_idx, value=val)
            cell.font = Font(name="Calibri", size=10)
            cell.border = thin_border
            if row_idx % 2 == 0:
                cell.fill = zebra_fill
            if col_idx in (1, 3, 7, 8):
                cell.alignment = Alignment(horizontal="center", vertical="center")
            else:
                cell.alignment = Alignment(horizontal="left", vertical="center")
        row_idx += 1

    auto_fit_columns(ws_unmatched)

    # -------------------------------------------------------------
    # TAB 3: CALLING SHEET COMPLIANCE STATUS
    # -------------------------------------------------------------
    ws_compliance = wb.create_sheet(title="Calling Sheet Compliance")
    ws_compliance.views.sheetView[0].showGridLines = True

    comp_headers = [
        "CSP ID / Code",
        "CSP Name",
        "Contact Phone",
        "Email Address",
        "State / Region",
        "Branch",
        "Compliance Category",
        "Category Definition",
        "Agreement Expiry Date",
        "PVR Expiry Date",
        "Character Cert Expiry",
        "IIBF Certified",
        "Total Documents",
        "Calling Sheet Sync Time"
    ]
    style_worksheet_table(
        ws_compliance,
        title="Calling Sheet Master Compliance & Expiry Matrix",
        subtitle="Continuous Compliance Tracking of all CSPs Seeded from Calling Sheet New",
        headers=comp_headers,
        header_bg="1F4E78"
    )

    all_csps = db.query(CSP).order_by(CSP.current_code.asc()).all()

    cat_counts = {"CATEGORY_A": 0, "CATEGORY_B": 0, "CATEGORY_C": 0, "CATEGORY_D": 0}

    # Pre-cache agreements and documents
    all_agreements = {a.csp_id: a for a in db.query(Agreement).filter(Agreement.is_active.is_(True)).all()}
    all_docs = db.query(Document).all()
    docs_by_csp = {}
    for d in all_docs:
        docs_by_csp.setdefault(d.csp_id, []).append(d)

    category_colors = {
        "CATEGORY_A": ("C6EFCE", "006100", "Category A: Fully Compliant"),
        "CATEGORY_B": ("FFEB9C", "9C6500", "Category B: Incomplete / Missing Docs"),
        "CATEGORY_C": ("FFC7CE", "9C0006", "Category C: Expired / Expiring"),
        "CATEGORY_D": ("E0E0E0", "3A3A3A", "Category D: Non-Responsive / No Docs")
    }

    row_idx = 5
    for csp in all_csps:
        agr = all_agreements.get(csp.id)
        docs = docs_by_csp.get(csp.id, [])
        cat = evaluate_csp_category(csp, agr, docs)
        cat_counts[cat] = cat_counts.get(cat, 0) + 1

        bg_col, text_col, cat_desc = category_colors.get(cat, ("FFFFFF", "000000", cat))

        agr_exp = agr.expiry_date.strftime("%Y-%m-%d") if agr and agr.expiry_date else "Missing"
        pvr_exp = agr.police_verification_expiry.strftime("%Y-%m-%d") if agr and agr.police_verification_expiry else "Missing"

        # Check for character cert and IIBF in docs
        has_cc = any(d.document_type == "CHARACTER_CERTIFICATE" for d in docs)
        cc_exp = "On File" if has_cc else "Missing"
        has_iibf = any(d.document_type == "IIBF_CERTIFICATE" for d in docs)
        iibf_str = "YES (Lifetime)" if has_iibf else "NO"

        sync_str = csp.calling_sheet_synced_at.strftime("%Y-%m-%d %H:%M") if csp.calling_sheet_synced_at else "Initial"

        row_vals = [
            csp.current_code or csp.lookup_code,
            csp.name,
            csp.phone or "N/A",
            csp.email or "N/A",
            csp.region or "N/A",
            csp.branch or "N/A",
            cat,
            cat_desc,
            agr_exp,
            pvr_exp,
            cc_exp,
            iibf_str,
            len(docs),
            sync_str
        ]

        ws_compliance.row_dimensions[row_idx].height = 20
        for col_idx, val in enumerate(row_vals, 1):
            cell = ws_compliance.cell(row=row_idx, column=col_idx, value=val)
            cell.font = Font(name="Calibri", size=10)
            cell.border = thin_border
            if col_idx == 7:  # Category badge
                cell.fill = PatternFill(start_color=bg_col, end_color=bg_col, fill_type="solid")
                cell.font = Font(name="Calibri", size=10, bold=True, color=text_col)
                cell.alignment = Alignment(horizontal="center", vertical="center")
            elif col_idx in (1, 3, 9, 10, 11, 12, 13, 14):
                cell.alignment = Alignment(horizontal="center", vertical="center")
            else:
                cell.alignment = Alignment(horizontal="left", vertical="center")
        row_idx += 1

    auto_fit_columns(ws_compliance)

    # -------------------------------------------------------------
    # TAB 4: OUTREACH & CLOSED-LOOP REPLIES
    # -------------------------------------------------------------
    ws_outreach = wb.create_sheet(title="Outreach & Reply Tracking")
    ws_outreach.views.sheetView[0].showGridLines = True

    outreach_headers = [
        "Notice ID",
        "CSP Code",
        "Channel",
        "Recipient Destination",
        "Template / Stage",
        "Status",
        "Sent Timestamp",
        "Reply Received?",
        "Reply Timestamp"
    ]
    style_worksheet_table(
        ws_outreach,
        title="Outreach Communication & Closed-Loop Response Log",
        subtitle="Automated Reminders, Escalations and CSP Inbound Replies",
        headers=outreach_headers,
        header_bg="385723"  # Forest green
    )

    outbounds = db.query(OutboundMessage).order_by(OutboundMessage.created_at.desc()).all()
    # Map CSP ID to code
    csp_map = {c.id: c.current_code or c.lookup_code for c in all_csps}

    # Find reply events
    reply_events = db.query(AgreementEvent).filter(
        AgreementEvent.event_type.in_(["CSP_REPLIED_TO_OUTREACH", "REPLY_RECEIVED"])
    ).all()
    csp_reply_map = {e.csp_id: e.response_received_at for e in reply_events if e.csp_id}

    row_idx = 5
    replies_count = 0
    for out in outbounds:
        csp_code = csp_map.get(out.csp_id, f"CSP-{out.csp_id}") if out.csp_id else "Unassigned"
        sent_str = out.sent_at.strftime("%Y-%m-%d %H:%M") if out.sent_at else "Draft/Queued"
        has_reply = out.csp_id in csp_reply_map
        if has_reply:
            replies_count += 1
            reply_str = "YES"
            reply_time_str = csp_reply_map[out.csp_id].strftime("%Y-%m-%d %H:%M") if csp_reply_map[out.csp_id] else "Recorded"
        else:
            reply_str = "Awaiting Reply"
            reply_time_str = "-"

        status_val = out.status.value if hasattr(out.status, 'value') else str(out.status)

        row_vals = [
            out.id,
            csp_code,
            out.channel,
            out.destination,
            out.template_name,
            status_val,
            sent_str,
            reply_str,
            reply_time_str
        ]

        ws_outreach.row_dimensions[row_idx].height = 20
        for col_idx, val in enumerate(row_vals, 1):
            cell = ws_outreach.cell(row=row_idx, column=col_idx, value=val)
            cell.font = Font(name="Calibri", size=10)
            cell.border = thin_border
            if col_idx in (1, 2, 3, 6, 7, 8, 9):
                cell.alignment = Alignment(horizontal="center", vertical="center")
            else:
                cell.alignment = Alignment(horizontal="left", vertical="center")
            if col_idx == 8 and val == "YES":
                cell.fill = PatternFill(start_color="C6EFCE", end_color="C6EFCE", fill_type="solid")
                cell.font = Font(name="Calibri", size=10, bold=True, color="006100")
        row_idx += 1

    auto_fit_columns(ws_outreach)

    # -------------------------------------------------------------
    # FILL EXECUTIVE SUMMARY (TAB 1)
    # -------------------------------------------------------------
    exec_headers = ["Metric Category", "Description", "Value", "Benchmark / Status"]
    style_worksheet_table(
        ws_exec,
        title="CSP Agreement Renewal & Compliance Executive Summary",
        subtitle="Autonomous Monitoring System Metrics & Calling Sheet Reconciliation",
        headers=exec_headers,
        header_bg="1F4E78"
    )

    total_scanned = db.query(InboundMessage).count()
    unmatched_count = len(unmatched_msgs)
    total_csps_count = len(all_csps)

    summary_data = [
        ("Calling Sheet Master Registry", "Total CSPs Synchronized from 'Calling Sheet New'", total_csps_count, "Master Baseline"),
        ("Calling Sheet Compliance", "Category A: Fully Compliant (Agreement + PVR Active)", cat_counts["CATEGORY_A"], f"{(cat_counts['CATEGORY_A']/max(total_csps_count,1)*100):.1f}% of Master"),
        ("Calling Sheet Compliance", "Category B: Incomplete / Missing Required Documents", cat_counts["CATEGORY_B"], "Target for Outreach"),
        ("Calling Sheet Compliance", "Category C: Expired Agreement or Police Verification", cat_counts["CATEGORY_C"], "Immediate Escalation"),
        ("Calling Sheet Compliance", "Category D: Zero Documents On File (Non-responsive)", cat_counts["CATEGORY_D"], "Action Required"),
        ("Calling Sheet Discrepancies", "Inbound Emails with Documents/Requests from Unmatched Senders", unmatched_count, "Review in Tab 2"),
        ("2-Year Email Ingestion", "Total Candidate Inbound Emails Processed (Inbox, Spam, Trash)", total_scanned, "Rolling 730 Days"),
        ("Outbound Outreach Lifecycle", "Total Automated Renewal & Terminal Notices Queued/Sent", len(outbounds), "Review in Tab 4"),
        ("Closed-Loop Engagement", "Total Inbound Responses / Replies from CSPs Detected", replies_count, "Closed-Loop"),
    ]

    row_idx = 5
    for item in summary_data:
        ws_exec.row_dimensions[row_idx].height = 24
        for col_idx, val in enumerate(item, 1):
            cell = ws_exec.cell(row=row_idx, column=col_idx, value=val)
            cell.border = thin_border
            cell.font = Font(name="Calibri", size=11, bold=(col_idx in (1, 3)))
            if col_idx == 3:
                cell.alignment = Alignment(horizontal="center", vertical="center")
                cell.fill = PatternFill(start_color="F2F2F2", end_color="F2F2F2", fill_type="solid")
            else:
                cell.alignment = Alignment(horizontal="left", vertical="center")
        row_idx += 1

    auto_fit_columns(ws_exec)

    # Save to in-memory bytes
    buffer = io.BytesIO()
    wb.save(buffer)
    buffer.seek(0)
    return buffer


def generate_auto_calling_sheet_workbook(db: Session) -> io.BytesIO:
    """
    Generates a dedicated Excel workbook formatted with the exact columns and layout
    of 'Calling Sheet New' containing all auto-detected unlisted CSPs.
    Ready for download or copy-pasting directly into Google Sheets.
    """
    wb = Workbook()
    ws = wb.active
    ws.title = "Calling Sheet New"
    ws.views.sheetView[0].showGridLines = True

    headers = [
        "CSP ID",
        "CSP Name",
        "CSP Mail ID",
        "Mobile",
        "State",
        "Branch Name",
        "Circle (LHO)",
        "Terminal Status",
        "Request / Document Type",
        "Date Detected",
        "Source Email Subject",
        "Promoted to Master"
    ]

    style_worksheet_table(
        ws,
        title="AUTO-GENERATED CALLING SHEET (DETECTED UNLISTED CSPS)",
        subtitle="Auto-Detected by CSP Agent • Formatted for 'Calling Sheet New' • Ready to Copy/Import",
        headers=headers,
        header_bg="1E7E34"  # Forest green for Calling Sheet
    )

    thin_border = Border(
        left=Side(style="thin", color="E0E0E0"),
        right=Side(style="thin", color="E0E0E0"),
        top=Side(style="thin", color="E0E0E0"),
        bottom=Side(style="thin", color="E0E0E0")
    )
    zebra_fill = PatternFill(start_color="F9FCF9", end_color="F9FCF9", fill_type="solid")

    entries = db.query(AutoCallingSheetEntry).order_by(AutoCallingSheetEntry.detected_at.desc()).all()

    row_idx = 5
    for entry in entries:
        ws.row_dimensions[row_idx].height = 22
        row_vals = [
            entry.csp_code or "",
            entry.csp_name or "",
            entry.csp_email or "",
            entry.phone or "",
            entry.state or "",
            entry.branch or "",
            entry.circle or "",
            entry.terminal_status or "AUTO_DETECTED",
            entry.request_type or "",
            entry.detected_at.strftime("%Y-%m-%d %H:%M") if entry.detected_at else "",
            entry.source_subject or "",
            "YES" if entry.is_promoted_to_master else "NO"
        ]

        for col_idx, val in enumerate(row_vals, 1):
            cell = ws.cell(row=row_idx, column=col_idx, value=val)
            cell.border = thin_border
            cell.font = Font(name="Calibri", size=10)
            if col_idx in (1, 4, 8, 9, 10, 12):
                cell.alignment = Alignment(horizontal="center", vertical="center")
            else:
                cell.alignment = Alignment(horizontal="left", vertical="center")

            if row_idx % 2 == 0:
                cell.fill = zebra_fill

            # Highlight promoted status
            if col_idx == 12:
                if val == "YES":
                    cell.font = Font(name="Calibri", size=10, bold=True, color="1E7E34")
                else:
                    cell.font = Font(name="Calibri", size=10, color="856404")

        row_idx += 1

    auto_fit_columns(ws)

    buffer = io.BytesIO()
    wb.save(buffer)
    buffer.seek(0)
    return buffer


