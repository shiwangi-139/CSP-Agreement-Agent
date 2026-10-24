"""
app/agreement_pages.py
A CSP Agreement is about 13 pages. Many CSPs sent only one page (the e-stamp
sheet or the first page), and it was accepted. By its number of pages:

  1-4 pages    REFUSE    not the whole agreement: turned away at once on the
                         upload page / master link (from e-mail: Review queue,
                         not counted);
  5-9 pages    REVIEW    may be incomplete: a person checks (not counted yet);
  10 or more   OK        read and accepted as usual.

Photos count one page each.
"""
from typing import Optional

REFUSE_BELOW = 5
OK_FROM = 10
EXPECTED = 13


def verdict(pages: Optional[int]) -> Optional[str]:
    """'refuse', 'review' or None (fine / unknown)."""
    if not pages:
        return None
    if pages < REFUSE_BELOW:
        return "refuse"
    if pages < OK_FROM:
        return "review"
    return None


def message(pages: int) -> dict:
    return {"hi": f"यह एग्रीमेंट अधूरा है: सिर्फ़ {pages} पेज मिले। कृपया एग्रीमेंट के सभी पेज "
                  f"(लगभग {EXPECTED}) एक साथ अपलोड करें।",
            "en": f"This agreement is incomplete: only {pages} page(s) received. Please upload all pages "
                  f"of the agreement (about {EXPECTED}) together."}


def review_note(pages: int) -> str:
    return (f"Only {pages} page(s): a CSP Agreement has about {EXPECTED}. Check that the whole signed "
            "agreement is there; reject it if pages are missing.")
