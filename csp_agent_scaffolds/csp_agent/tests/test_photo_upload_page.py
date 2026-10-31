"""The CSP upload page: photos (camera) or a PDF for each document, no
gallery, photos made small on the phone and sent as one document's pages,
no page count shown; the DC's number (else the RM's) for help."""
import json
import re
from datetime import date, timedelta
from pathlib import Path

from app.api.portal import MSG
from app.models import CSP, InternalUser
from tests.test_portal_and_api import _csp, _form, client  # noqa: F401  (fixture)

PAGE = (Path(__file__).resolve().parent.parent / "app" / "web" / "portal.html").read_text(encoding="utf-8")


def _ctx(c, token):
    html = c.get(f"/upload?token={token}").text
    return json.loads(re.search(r'<script type="application/json" id="ctx">(.*?)</script>', html, re.S).group(1))


def test_camera_or_pdf_only_and_photos_made_small():
    assert 'capture="environment"' in PAGE and 'accept="application/pdf" hidden' in PAGE
    assert "image/jpeg,image/png" not in PAGE                 # no picking photos from the gallery
    assert "LONG_SIDE = 1600" in PAGE and "QUALITY = 0.7" in PAGE and "MAX_SHOTS = 25" in PAGE
    assert "photoProblem(img)" in PAGE                        # every photo checked before it is kept


def test_help_shows_the_dcs_number_else_the_rms(client):
    c, Session = client
    cid, _, token = _csp(Session)
    assert _ctx(c, token)["help"]["role"] == "RM"            # no DC: the RM (9000000011)
    s = Session()
    dc = InternalUser(name="DC Test", role="DC", phone="+91 98765 43210")
    s.add(dc)
    s.flush()
    s.get(CSP, cid).dc_id = dc.id
    s.commit()
    s.close()
    h = _ctx(c, token)["help"]
    assert h == {"role": "DC", "name": "DC Test", "phone": "9876543210"}


def test_photos_of_one_document_arrive_as_one_pdf(client):
    """What the page sends: several small JPEGs under the same name."""
    import numpy as np
    import cv2
    c, Session = client
    _, _, token = _csp(Session)
    pages = []
    for i in range(2):
        img = np.full((1600, 1200), 255, np.uint8)
        for y in range(120, 1500, 60):
            cv2.putText(img, f"CHARACTER CERTIFICATE page {i + 1} line {y}", (60, y), cv2.FONT_HERSHEY_SIMPLEX, 1.1, 0, 2)
        pages.append(("pvr_file", (f"page-{i + 1}.jpg", cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 70])[1].tobytes(),
                                   "image/jpeg")))
    issue = date.today() - timedelta(days=20)
    r = c.post("/api/portal/upload", data=_form(token, pvr_issue_date=issue.isoformat()), files=pages)
    assert r.status_code == 200
    [res] = r.json()["results"]
    assert res["section"] == "pvr" and "pages" not in res     # the page count is checked, never shown
    for refused in ("pdf_or_photos", "too_big", "too_many_pages", "file_type"):
        assert res["message"]["en"] != MSG[refused][1]

