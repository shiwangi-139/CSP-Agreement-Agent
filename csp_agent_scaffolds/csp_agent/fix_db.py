from app.db import engine
from sqlalchemy import text

with engine.connect() as conn:
    try:
        conn.execute(text("ALTER TABLE csp ADD COLUMN IF NOT EXISTS calling_sheet_synced_at TIMESTAMP WITH TIME ZONE"))
        conn.execute(text("ALTER TABLE csp ADD COLUMN IF NOT EXISTS sheet_row_version VARCHAR"))
        conn.commit()
        print("Successfully added columns to CSP table.")
    except Exception as e:
        print(f"Error updating CSP table: {e}")

    try:
        conn.execute(text("ALTER TABLE inbound_messages ADD COLUMN IF NOT EXISTS email_category VARCHAR"))
        conn.execute(text("ALTER TABLE inbound_messages ADD COLUMN IF NOT EXISTS ai_classification_fallback BOOLEAN"))
        conn.commit()
        print("Successfully added columns to inbound_messages table.")
    except Exception as e:
        print(f"Error updating inbound_messages table: {e}")
        
    try:
        conn.execute(text("ALTER TABLE documents ADD COLUMN IF NOT EXISTS extraction_method VARCHAR"))
        conn.commit()
        print("Successfully added columns to documents table.")
    except Exception as e:
        print(f"Error updating documents table: {e}")
        
    try:
        # Create outbound_messages table just in case create_all didn't catch it
        from app.models import Base
        Base.metadata.create_all(bind=engine)
        print("Ensured all new tables are created.")
    except Exception as e:
        print(f"Error running create_all: {e}")

