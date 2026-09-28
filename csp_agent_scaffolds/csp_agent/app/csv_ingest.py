import csv
import io
import logging
from datetime import datetime
from sqlalchemy.orm import Session
from .models import Agreement

logger = logging.getLogger(__name__)


def process_csv_attachment(csv_bytes: bytes, csp_id: int, db: Session) -> dict:
    text = csv_bytes.decode("utf-8")
    reader = csv.DictReader(io.StringIO(text))
    created, errors = 0, []

    for row_num, row in enumerate(reader, start=2):  # header is row 1
        try:
            start_date = datetime.strptime(row["agreement_start_date"], "%Y-%m-%d").date()
            expiry_date = datetime.strptime(row["agreement_expiry_date"], "%Y-%m-%d").date()
            if start_date >= expiry_date:
                raise ValueError("start_date_not_before_expiry")

            pv_expiry = None
            if row.get("police_verification_expiry"):
                pv_expiry = datetime.strptime(row["police_verification_expiry"], "%Y-%m-%d").date()

            db.add(Agreement(
                csp_id=csp_id, start_date=start_date, expiry_date=expiry_date,
                police_verification_expiry=pv_expiry, current_csp_code=row.get("lookup_code"),
                is_active=True,
            ))
            created += 1
        except Exception as e:
            errors.append({"row": row_num, "error": str(e)})
            logger.warning("csv_row_rejected", extra={"reason": str(e)})
            continue

    db.commit()
    return {"created": created, "errors": errors}
