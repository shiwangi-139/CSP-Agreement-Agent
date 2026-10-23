"""
Make the light view copies (app/view_copy.py) of every stored document over
2 MB, so even the first view on the dashboard is quick. Originals are not
touched. Safe to stop and run again (done copies are skipped).

On the shared server, inside tmux, at night:
    nice -n 19 .venv/bin/python -m scripts.make_view_copies
"""
from app import vault
from app.db import SessionLocal
from app.models import Document
from app.view_copy import SMALL_ENOUGH, view_file


def main():
    db = SessionLocal()
    try:
        rows = [(d.id, d.storage_path) for d in db.query(Document.id, Document.storage_path)
                .filter(Document.storage_path.isnot(None), Document.mime_type.in_(["application/pdf", "image/jpeg", "image/png"]))]
    finally:
        db.close()
    made = small = missing = 0
    before = after = 0
    for i, (doc_id, sp) in enumerate(rows, 1):
        path = vault.abs_path(sp)
        if path is None or not path.exists():
            missing += 1
            continue
        if path.stat().st_size <= SMALL_ENOUGH:
            small += 1
            continue
        out, _ = view_file(doc_id, path)
        before += path.stat().st_size
        after += out.stat().st_size
        made += 1
        if i % 50 == 0:
            print(f"... {i}/{len(rows)}", flush=True)
    print(f"{made} view copies ({before / 1e9:.2f} GB -> {after / 1e9:.2f} GB), {small} already small, {missing} file missing")


if __name__ == "__main__":
    main()
