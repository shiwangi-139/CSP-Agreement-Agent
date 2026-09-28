"""
Utility script to view, add, or update CSP contact details in the Neon PostgreSQL database.

Usage:
  python scripts/manage_csp.py list
  python scripts/manage_csp.py add --code 1A852474 --name "Sohit Kumar" --email "shiwangi.sinha.intern@eko.co.in" --phone "9876543210"
"""
import argparse
import sys
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv()

from app.db import SessionLocal
from app.models import CSP


def list_csps():
    db = SessionLocal()
    try:
        csps = db.query(CSP).all()
        print(f"\nFound {len(csps)} CSP(s) in database:")
        print("-" * 80)
        for c in csps:
            print(f"ID: {c.id:<4} | Code: {c.lookup_code:<10} | Name: {c.name:<22} | Email: {c.email or 'N/A':<28} | Phone: {c.phone or 'N/A'}")
        print("-" * 80)
    finally:
        db.close()


def upsert_csp(code: str, name: str, email: str | None = None, phone: str | None = None,
               branch: str | None = None, region: str | None = None, kiosk: str | None = None):
    db = SessionLocal()
    try:
        csp = db.query(CSP).filter(CSP.lookup_code == code).first()
        if not csp:
            csp = CSP(
                lookup_code=code,
                current_code=code,
                name=name,
                email=email,
                phone=phone,
                branch=branch,
                region=region,
                kiosk_location=kiosk,
                status="ACTIVE",
            )
            db.add(csp)
            print(f"[+] Created new CSP: Code={code}, Name='{name}', Email='{email}', Phone='{phone}'")
        else:
            csp.name = name or csp.name
            if email: csp.email = email
            if phone: csp.phone = phone
            if branch: csp.branch = branch
            if region: csp.region = region
            if kiosk: csp.kiosk_location = kiosk
            print(f"[*] Updated existing CSP {code}: Name='{csp.name}', Email='{csp.email}', Phone='{csp.phone}'")
        db.commit()
        db.refresh(csp)
        return csp
    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser(description="Manage CSP contact details")
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("list", help="List all CSPs in database")

    add_parser = subparsers.add_parser("add", help="Add or update a CSP")
    add_parser.add_argument("--code", required=True, help="CSP Lookup Code (e.g. 1A852474)")
    add_parser.add_argument("--name", required=True, help="CSP Full Name")
    add_parser.add_argument("--email", default=None, help="CSP Email Address")
    add_parser.add_argument("--phone", default=None, help="CSP Phone Number")
    add_parser.add_argument("--branch", default=None, help="Branch Name")
    add_parser.add_argument("--region", default=None, help="Region Name")
    add_parser.add_argument("--kiosk", default=None, help="Kiosk Location")

    args = parser.parse_args()

    if args.command == "list":
        list_csps()
    elif args.command == "add":
        upsert_csp(
            code=args.code,
            name=args.name,
            email=args.email,
            phone=args.phone,
            branch=args.branch,
            region=args.region,
            kiosk=args.kiosk,
        )
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
