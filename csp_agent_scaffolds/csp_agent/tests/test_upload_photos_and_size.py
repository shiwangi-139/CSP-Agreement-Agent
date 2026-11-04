"""Upload pages: a PDF over the size limit is drawn page by page as photos on
the phone (pdf-page-N.jpg, no camera checks for those); the DC master link has
the camera, the photo checks and the size bar too; size messages say what to do."""
from datetime import date, timedelta
from pathlib import Path

from app import master_link
from app.api.portal import MSG
from app.models import Document
from tests.test_portal_and_api import _csp, client  # noqa: F401  (fixture)

WEB = Path(__file__).resolve().parent.parent / "app" / "web"
CSP_PAGE = (WEB / "portal.html").read_text(encoding="utf-8")
DC_PAGE = (WEB / "dc_master.html").read_text(encoding="utf-8")
ISSUE = date.today() - timedelta(days=20)


def _page(text: str = "", blank: bool = False) -> bytes:
    import cv2
    import numpy as np
    img = np.full((1600, 1200), 255, np.uint8)
    if not blank:
        for y in range(120, 1500, 60):
            cv2.putText(img, f"CHARACTER CERTIFICATE {text} line {y}", (60, y), cv2.FONT_HERSHEY_SIMPLEX, 1.1, 0, 2)
    return cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 70])[1].tobytes()


def _dc(Session):
    cid, code, _ = _csp(Session)
    s = Session()
    token = master_link.create(s, "t").split("dc=")[1]
    s.commit()
    s.close()
    return cid, code, token


def test_both_pages_compress_a_large_pdf_and_send_it_as_pages():
    for page in (CSP_PAGE, DC_PAGE):
        assert "pdf.js/3.11.174/" in page and '"pdf.min.js"' in page and "compressPdf(" in page and "pdf-page" in page
        assert "scan app" not in page                                      # no "make the PDF again yourself"
        assert "too_big: FAIL.too_big" in page                             # 413: what to do, not "try later"
    assert 'capture="environment"' in DC_PAGE and "photoProblem(img)" in DC_PAGE and "_size" in DC_PAGE
    assert "📷" in MSG["too_big"][0] and "Take photos" in MSG["too_big"][1]


def test_dc_master_link_photos_of_one_document_arrive_as_one_document(client):
    c, Session = client
    cid, code, token = _dc(Session)
    files = [("pvr_file", (f"page-{i}.jpg", _page(f"page {i}"), "image/jpeg")) for i in (1, 2)]
    r = c.post("/api/portal/upload", data={"pvr_issue_date": ISSUE.isoformat(), "dc": token, "code": code}, files=files)
    assert r.status_code == 200, r.text
    [res] = r.json()["results"]
    assert res["section"] == "pvr" and res.get("reason") not in ("too_big", "pdf_or_photos", "washed_out")
    s = Session()
    docs = s.query(Document).filter_by(csp_id=cid).all()
    assert len(docs) <= 1 and all(d.upload_channel == "DC_MASTER_LINK" for d in docs)
    s.close()


def test_a_blank_page_drawn_from_a_pdf_is_not_refused_as_a_bad_photo(client):
    c, Session = client
    _, code, token = _dc(Session)
    form = {"pvr_issue_date": ISSUE.isoformat(), "dc": token, "code": code}
    taken = [("pvr_file", ("page-1.jpg", _page("one"), "image/jpeg")), ("pvr_file", ("page-2.jpg", _page(blank=True), "image/jpeg"))]
    flash = lambda files: "flash" in c.post("/api/portal/upload", data=form, files=files).json()["results"][0]["message"]["en"]
    assert flash(taken)                                                    # a camera photo: retake it
    drawn = [("pvr_file", ("pdf-page-1.jpg", _page("one"), "image/jpeg")),
             ("pvr_file", ("pdf-page-2.jpg", _page(blank=True), "image/jpeg"))]
    assert not flash(drawn)
