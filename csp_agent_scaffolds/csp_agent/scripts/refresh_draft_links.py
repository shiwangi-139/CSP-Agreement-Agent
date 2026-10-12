"""
Rewrite the upload link in every message that has not been sent yet, so it
uses the current PUBLIC_BASE_URL (e.g. after going live behind Nginx).
Drafts written earlier carry whatever address was set then, such as
http://127.0.0.1:8000 or a placeholder, which no CSP can open.

    python -m scripts.refresh_draft_links            # dry run: shows the old addresses found
    python -m scripts.refresh_draft_links --apply    # rewrite them

Only the part before "/upload?token=" changes; each CSP keeps its own token.
Sent messages are never touched.
"""
import argparse
import re
from collections import Counter

from app.config import PUBLIC_BASE_URL
from app.db import SessionLocal
from app.models import OutboundMessage, OutboundStatus

UNSENT = (OutboundStatus.QUEUED_FOR_REVIEW, OutboundStatus.DRAFT, OutboundStatus.APPROVED,
          OutboundStatus.READY_NOT_SENT, OutboundStatus.BLOCKED)
LINK = re.compile(r"\S+?(?=/upload\?token=)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    base = PUBLIC_BASE_URL.rstrip("/")
    if not base.startswith(("http://", "https://")) or "127.0.0.1" in base or "localhost" in base:
        raise SystemExit(f"PUBLIC_BASE_URL is {base!r}: set the public address in .env first.")
    db = SessionLocal()
    try:
        old, changed = Counter(), 0
        for m in db.query(OutboundMessage).filter(OutboundMessage.status.in_(UNSENT)):
            p = dict(m.payload_json or {})
            touched = False
            for key in ("body", "body_hi", "body_en", "html"):
                if isinstance(p.get(key), str) and "/upload?token=" in p[key]:
                    for found in LINK.findall(p[key]):
                        if found != base:
                            old[found] += 1
                    new = LINK.sub(base, p[key])
                    touched |= new != p[key]
                    p[key] = new
            if touched:
                changed += 1
                if args.apply:
                    m.payload_json = p
        if args.apply:
            db.commit()
        print("old addresses found:", dict(old) or "none")
        print(f"{changed} unsent messages {'updated' if args.apply else 'would be updated'} to {base}/upload?token=...")
    finally:
        db.close()


if __name__ == "__main__":
    main()
