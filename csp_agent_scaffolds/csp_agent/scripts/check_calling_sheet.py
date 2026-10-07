"""
Which calling sheet the agent reads, without changing anything and without
printing the link or any CSP data.

    python -m scripts.check_calling_sheet
"""
from app.comms import sheet_source as s


def main():
    sid, gid = s.parse_sheet_link(s.CALLING_SHEET_LINK)
    print(f"CALLING_SHEET_LINK set: {bool(s.CALLING_SHEET_LINK)} | sheet id in it: {bool(sid)} | "
          f"tab (#gid=) in it: {bool(gid)}")
    try:
        rows = s.load_calling_sheet_rows()
    except Exception as e:
        raise SystemExit(f"could not read any calling sheet: {e}")
    heads = list(rows[0]) if rows else []
    print(f"read from: {s.LAST_SOURCE.get('source')} | rows: {len(rows)} | "
          f"RM columns present: {'Mobile No Of RM' in heads and 'Email of RM' in heads}")
    if s.LAST_SOURCE.get("note"):
        print(s.LAST_SOURCE["note"])


if __name__ == "__main__":
    main()
