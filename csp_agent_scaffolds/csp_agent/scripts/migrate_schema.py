"""
scripts/migrate_schema.py
Safely applies schema additions to Neon PostgreSQL database:
- Adds is_active_in_calling_sheet, has_missing_contact to 'csp'
- Adds is_current, upload_channel, original_filename, issue_date, expiry_date,
  validity_rule_used, has_explicit_3year_clause, iibf_reg_number, field_confidences to 'documents'
"""

import sys
import os
import logging
from sqlalchemy import text

# Automatically add parent directory so it runs with or without PYTHONPATH=.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.db import engine, Base
import app.models
logger = logging.getLogger("migrate_schema")

MIGRATIONS = [
    # CSP table enhancements
    "ALTER TABLE csp ADD COLUMN IF NOT EXISTS is_active_in_calling_sheet BOOLEAN DEFAULT TRUE;",
    "ALTER TABLE csp ADD COLUMN IF NOT EXISTS has_missing_contact BOOLEAN DEFAULT FALSE;",
    "ALTER TABLE csp ADD COLUMN IF NOT EXISTS sub_slab VARCHAR;",
    "CREATE INDEX IF NOT EXISTS ix_csp_sub_slab ON csp (sub_slab);",
    "CREATE INDEX IF NOT EXISTS ix_csp_is_active_in_calling_sheet ON csp (is_active_in_calling_sheet);",

    # Document table enhancements
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS is_current BOOLEAN DEFAULT TRUE;",
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS upload_channel VARCHAR DEFAULT 'GMAIL_INBOUND';",
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS original_filename VARCHAR;",
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS issue_date DATE;",
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS expiry_date DATE;",
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS validity_rule_used VARCHAR;",
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS has_explicit_3year_clause BOOLEAN DEFAULT FALSE;",
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS iibf_reg_number VARCHAR;",
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS field_confidences JSONB;",
    "CREATE INDEX IF NOT EXISTS ix_documents_is_current ON documents (is_current);",
    "CREATE INDEX IF NOT EXISTS ix_documents_expiry_date ON documents (expiry_date);",
    "CREATE INDEX IF NOT EXISTS ix_documents_issue_date ON documents (issue_date);",
]


def run_migrations():
    db_url = str(engine.url)
    # Hide password in log
    safe_url = engine.url.render_as_string(hide_password=True)
    print(f"\n[+] Connecting to PostgreSQL at: {safe_url}")
    
    print("[+] Creating/verifying all core tables (CSP, Documents, Agreements, Events)...")
    import app.models  # registers all models with Base.metadata
    Base.metadata.create_all(bind=engine)
    print("[+] Core tables initialized successfully.")

    print("[+] Applying schema columns and performance indexes...")
    with engine.connect() as conn:
        for stmt in MIGRATIONS:
            try:
                conn.execute(text(stmt))
            except Exception as e:
                logger.warning(f"Notice on statement '{stmt.strip()[:40]}': {e}")
        conn.commit()

    print("[+] Verifying registered tables in database...")
    with engine.connect() as conn:
        tables_res = conn.execute(text("SELECT table_name FROM information_schema.tables WHERE table_schema='public' ORDER BY table_name;"))
        tables = [r[0] for r in tables_res.fetchall()]
        print(f"[SUCCESS] {len(tables)} tables ready in database: {', '.join(tables)}\n")


if __name__ == "__main__":
    run_migrations()

