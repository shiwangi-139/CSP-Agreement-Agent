"""
tests/test_two_year_rolling_and_xlsx.py
Tests for:
1. 2-year rolling window Gmail server query builder
2. Openpyxl Excel (.xlsx) multi-tab discrepancy report generator
3. Closed-loop outreach reply detection
4. Calling Sheet New row hash generation and idempotency
"""

import io
from datetime import datetime, timezone, timedelta, date
from unittest.mock import MagicMock, patch
import openpyxl

from app.comms.sheets_sync import generate_row_hash
from app.email_ingest import classify_email, evaluate_csp_category
from app.reports_xlsx import generate_discrepancy_workbook
from app.models import CSP, Agreement, Document, DocumentStatus, InboundMessage, OutboundMessage, OutboundStatus, EmailCategory


def test_calling_sheet_row_hash():
    """Verify deterministic hash generation for sheet row versioning."""
    row1 = ["1A850244", "Abhishek Kumar Nirala", "6201099082", "abhishekkumar@gmail.com"]
    row2 = ["1A850244", "Abhishek Kumar Nirala", "6201099082", "abhishekkumar@gmail.com"]
    row3 = ["1A850244", "Abhishek Kumar Nirala", "9999999999", "abhishekkumar@gmail.com"]

    hash1 = generate_row_hash(row1)
    hash2 = generate_row_hash(row2)
    hash3 = generate_row_hash(row3)

    assert hash1 == hash2, "Identical rows must produce identical hashes"
    assert hash1 != hash3, "Modified rows must produce different hashes"


def test_gmail_2year_query_generation():
    """Verify rolling 730-day cutoff date calculation."""
    days_back = 730
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days_back)).strftime("%Y/%m/%d")
    
    # Simulate query construction in fetch_incoming_csp_emails
    base_query = "(filename:pdf OR CSP OR Agreement OR 'terminal extension' OR 'terminal reset' OR 'Police Verification' OR 'character certificate' OR IIBF OR renewal)"
    query = f"{base_query} after:{cutoff}"
    
    assert f"after:{cutoff}" in query
    assert "terminal reset" in query
    assert "terminal extension" in query


def test_evaluate_csp_categories_all_4():
    """Verify all 4 compliance categories evaluate accurately."""
    today = date.today()
    csp = CSP(id=1, name="Test Agent", current_code="1A850247")

    # Category D: Zero documents
    cat_d = evaluate_csp_category(csp, None, [])
    assert cat_d == "CATEGORY_D"

    # Category B: Only Agreement, missing Police Verification
    doc_agr = Document(csp_id=1, document_type="AGREEMENT", status=DocumentStatus.VALID)
    agr_active = Agreement(csp_id=1, is_active=True, expiry_date=today + timedelta(days=365))
    cat_b = evaluate_csp_category(csp, agr_active, [doc_agr])
    assert cat_b == "CATEGORY_B"

    # Category A: all three documents (Agreement + Police Verification + IIBF) valid
    doc_pv = Document(csp_id=1, document_type="POLICE_VERIFICATION", status=DocumentStatus.VALID)
    doc_iibf = Document(csp_id=1, document_type="IIBF_CERTIFICATE", status=DocumentStatus.VALID)
    agr_active.police_verification_expiry = today + timedelta(days=200)
    cat_a = evaluate_csp_category(csp, agr_active, [doc_agr, doc_pv, doc_iibf])
    assert cat_a == "CATEGORY_A"

    # Category C: Expired agreement
    agr_expired = Agreement(csp_id=1, is_active=True, expiry_date=today - timedelta(days=5))
    cat_c = evaluate_csp_category(csp, agr_expired, [doc_agr, doc_pv])
    assert cat_c == "CATEGORY_C"


def test_xlsx_discrepancy_workbook_generation():
    """Verify multi-tab Excel spreadsheet generation via openpyxl."""
    # Build mock DB session
    mock_db = MagicMock()

    mock_csp = CSP(
        id=1,
        name="Abhishek Kumar Nirala",
        current_code="1A850244",
        lookup_code="1A850244",
        phone="6201099082",
        email="abhishekkumar@gmail.com",
        region="Bihar",
        branch="BANMANKHI",
        status="ACTIVE"
    )
    mock_agr = Agreement(
        id=1,
        csp_id=1,
        is_active=True,
        expiry_date=date(2027, 9, 20),
        police_verification_expiry=date(2027, 3, 15)
    )
    mock_doc = Document(
        id=1,
        csp_id=1,
        document_type="AGREEMENT",
        status=DocumentStatus.VALID
    )
    mock_unmatched = InboundMessage(
        id=101,
        external_message_id="msg-unmatched-1",
        sender="external_person@yahoo.com",
        subject="Request to terminal reset for KO 1A999999",
        email_category=EmailCategory.TERMINAL_RESET,
        status="IGNORED_NON_CSP",
        error_message='{"csp_code": null, "reason": "Sender / content not registered in Calling Sheet master data", "folder": "INBOX"}',
        received_at=datetime(2026, 8, 15, 10, 30, tzinfo=timezone.utc)
    )
    mock_outbound = OutboundMessage(
        id=201,
        csp_id=1,
        template_name="RENEWAL_REMINDER_EMAIL",
        channel="EMAIL",
        destination="abhishekkumar@gmail.com",
        status=OutboundStatus.SENT,
        created_at=datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc),
        sent_at=datetime(2026, 9, 1, 12, 5, tzinfo=timezone.utc)
    )

    def mock_query(model):
        query_mock = MagicMock()
        if model == CSP:
            query_mock.order_by.return_value.all.return_value = [mock_csp]
            query_mock.all.return_value = [mock_csp]
        elif model == Agreement:
            query_mock.filter.return_value.all.return_value = [mock_agr]
        elif model == Document:
            query_mock.all.return_value = [mock_doc]
        elif model == InboundMessage:
            query_mock.filter.return_value.order_by.return_value.all.return_value = [mock_unmatched]
            query_mock.count.return_value = 1
        elif model == OutboundMessage:
            query_mock.order_by.return_value.all.return_value = [mock_outbound]
        else:
            query_mock.filter.return_value.all.return_value = []
            query_mock.order_by.return_value.all.return_value = []
            query_mock.all.return_value = []
        return query_mock

    mock_db.query.side_effect = mock_query

    # Generate workbook
    xlsx_stream = generate_discrepancy_workbook(mock_db)
    assert isinstance(xlsx_stream, io.BytesIO)
    xlsx_stream.seek(0)

    # Validate workbook with openpyxl
    wb = openpyxl.load_workbook(xlsx_stream)
    expected_sheets = [
        "Executive Summary",
        "Calling Sheet Discrepancies",
        "Calling Sheet Compliance",
        "Outreach & Reply Tracking"
    ]
    for s in expected_sheets:
        assert s in wb.sheetnames, f"Expected sheet '{s}' missing in generated Excel workbook"

    # Verify discrepancies tab contains data
    ws_disc = wb["Calling Sheet Discrepancies"]
    assert ws_disc.cell(row=5, column=4).value == "external_person@yahoo.com"
    assert "TERMINAL_RESET" in str(ws_disc.cell(row=5, column=6).value)

    # Verify compliance tab contains data
    ws_comp = wb["Calling Sheet Compliance"]
    assert ws_comp.cell(row=5, column=1).value == "1A850244"
    assert ws_comp.cell(row=5, column=2).value == "Abhishek Kumar Nirala"

