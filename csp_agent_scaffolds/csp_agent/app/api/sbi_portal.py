"""
app/api/sbi_portal.py
Agent 3 on the dashboard: the SBI Upload page (app/sbi_upload.py). Read-only
worklist and status — which CSP documents are ready to upload to the SBI FI
portal, the exact values to enter, and what has already been uploaded. No
browser, no login, no credentials here; the uploading is done by a person on
a workstation (sbi_upload/upload_document.py). Mounted under /api/hub.
"""
import io
from collections import Counter
from datetime import date

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from .. import sbi_upload
from ..auth import Principal, require_admin, require_user
from ..db import get_db
from .hub import NOT_TEST, _rm_filter

router = APIRouter(dependencies=[Depends(require_user)])

STATUS_LABEL = {"ready": "Ready to upload", "uploaded": "Uploaded", "reupload": "Re-upload (newer copy)",
                "waiting": "Waiting for the CSP", "blocked": "File too large"}


def _scope(me: Principal):
    return None if me.is_admin else (NOT_TEST & _rm_filter(me))


def _filter(rows, status, kind, rm, dc, q):
    if status:
        rows = [r for r in rows if r["status"] == status]
    if kind:
        rows = [r for r in rows if r["kind"] == kind]
    if rm:
        rows = [r for r in rows if r["rm"] == rm]
    if dc:
        rows = [r for r in rows if r["dc"] == dc]
    if q.strip():
        n = q.strip().lower()
        rows = [r for r in rows if n in (r["csp"]["code"] or "").lower() or n in (r["csp"]["name"] or "").lower()]
    return rows


@router.get("/sbi-upload", dependencies=[Depends(require_admin)])
def sbi_upload_page(status: str = "", kind: str = "", rm: str = "", dc: str = "", q: str = "",
                    me: Principal = Depends(require_user), db: Session = Depends(get_db)):
    data = sbi_upload.dashboard_rows(db, _scope(me))
    all_rows = data["rows"]
    in_scope = _filter(all_rows, "", kind, rm, dc, q)
    shown = _filter(in_scope, status, "", "", "", "")
    opts = lambda key: [{"name": k, "n": n} for k, n in sorted(Counter(r[key] for r in all_rows).items(),
                                                               key=lambda x: str(x[0]).lower())]
    return {
        "signatory": data["signatory"], "max_mb": data["max_mb"], "total": len(all_rows),
        "last_upload": sbi_upload.last_upload(db),
        "statuses": [{"key": k, "label": label, "n": sum(r["status"] == k for r in in_scope)}
                     for k, label in STATUS_LABEL.items()],
        "kinds": [{"key": k, "label": sbi_upload.SHORT[k], "n": sum(r["kind"] == k for r in in_scope)}
                  for k in sbi_upload.DOC_KINDS],
        "options": {"rm": opts("rm"), "dc": opts("dc")},
        "rows": shown[:3000], "shown": len(shown),
    }


@router.get("/sbi-upload.xlsx", dependencies=[Depends(require_admin)])
def sbi_upload_xlsx(status: str = "", kind: str = "", rm: str = "", dc: str = "", q: str = "",
                    me: Principal = Depends(require_user), db: Session = Depends(get_db)):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    rows = _filter(sbi_upload.dashboard_rows(db, _scope(me))["rows"], status, kind, rm, dc, q)
    wb = Workbook()
    ws = wb.active
    ws.title = "SBI Upload"
    head = ["CSP", "KO code", "RM", "DC", "Document", "Status", "Values to enter", "File", "Uploaded"]
    ws.append(head)
    for r in rows:
        v = r["values"]
        vs = " · ".join(f"{k.replace('_', ' ')}: {x}" for k, x in v.items() if x) or (r["reason"] or "")
        up = r["uploaded"]
        ws.append([r["csp"]["name"], r["csp"]["code"], r["rm"], r["dc"], r["doc_label"],
                   STATUS_LABEL.get(r["status"], r["status"]), vs, r["file"] or "",
                   f"{up['at'][:10]} by {up['by']}" if up else ""])
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="1F4E78")
        c.alignment = Alignment(vertical="center")
    for col, w in zip("ABCDEFGHI", (26, 12, 16, 16, 12, 18, 52, 30, 22)):
        ws.column_dimensions[col].width = w
    ws.freeze_panes = "A2"
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    name = f"sbi_upload_{date.today():%Y%m%d}.xlsx"
    return StreamingResponse(buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                             headers={"Content-Disposition": f'attachment; filename="{name}"'})
