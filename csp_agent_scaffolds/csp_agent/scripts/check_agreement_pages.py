"""
List the agreements on record (the current copy of each CSP) that have fewer
pages than a whole CSP Agreement (app/agreement_pages.py): 1-4 pages, and
5-9 pages. Changes nothing.

    .venv/bin/python -m scripts.check_agreement_pages
"""
from app import agreement_pages, vault
from app.db import SessionLocal
from app.models import CSP, Document


def pages_of(doc: Document) -> int:
    n = (doc.extracted_fields or {}).get("page_count")
    if n:
        return int(n)
    path = vault.abs_path(doc.storage_path) if doc.storage_path else None
    if path is None or not path.exists():
        return 0
    if (doc.mime_type or "").startswith("image/"):
        return 1
    import pymupdf
    with pymupdf.open(path) as d:
        return len(d)


def main():
    db = SessionLocal()
    try:
        rows = (db.query(Document, CSP).join(CSP, CSP.id == Document.csp_id)
                .filter(Document.document_type == "AGREEMENT", Document.is_current.is_(True),
                        CSP.is_active_in_calling_sheet.is_(True)).order_by(CSP.current_code).all())
        groups = {"refuse": [], "review": []}
        for doc, csp in rows:
            n = pages_of(doc)
            v = agreement_pages.verdict(n)
            if v:
                groups[v].append((csp, doc, n))
        for v, title in (("refuse", "1-4 pages (would be refused today)"), ("review", "5-9 pages (would go to review)")):
            print(f"\n== {title}: {len(groups[v])} ==")
            for csp, doc, n in groups[v]:
                print(f"  {csp.current_code}  {csp.name[:26]:26}  {n} page(s)  {doc.status.value:16} "
                      f"issued {doc.issue_date}  via {doc.upload_channel}")
        print(f"\n{len(rows)} current agreements checked. Nothing was changed.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
