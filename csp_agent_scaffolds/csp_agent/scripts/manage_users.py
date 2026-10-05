"""
Dashboard accounts (app/auth.py). Run on the server; passwords are typed in
(not shown, never stored in clear).

    python -m scripts.manage_users list
    python -m scripts.manage_users add-admin  --email you@eko.co.in --name "Your Name"
    python -m scripts.manage_users enable-rm  --email rm@eko.co.in          # an RM already on the calling sheet
    python -m scripts.manage_users enable-rm  --name "Shiwani" --email shiwani@eko.co.in
    python -m scripts.manage_users password   --email someone@eko.co.in     # set / reset a password
    python -m scripts.manage_users disable    --email someone@eko.co.in     # also ends their sessions
    python -m scripts.manage_users unlock     --email someone@eko.co.in

An RM's account is the internal_users row the calling sheet created for
them, so they automatically see exactly the CSPs assigned to them.
"""
import argparse
import getpass
import sys

from sqlalchemy import func

from app import auth
from app.db import SessionLocal
from app.models import AdminSession, CSP, InternalUser


def _ask_password() -> str:
    while True:
        pw = getpass.getpass("New password: ")
        problem = auth.password_problem(pw)
        if problem:
            print(problem)
            continue
        if getpass.getpass("Repeat it: ") != pw:
            print("The two passwords differ.")
            continue
        return pw


def _by_email(db, email: str):
    return db.query(InternalUser).filter(func.lower(InternalUser.email) == email.strip().lower()).first()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["list", "add-admin", "enable-rm", "password", "disable", "unlock"])
    ap.add_argument("--email")
    ap.add_argument("--name")
    a = ap.parse_args()
    db = SessionLocal()
    try:
        if a.action == "list":
            for u in db.query(InternalUser).order_by(InternalUser.role, InternalUser.name):
                csps = db.query(CSP).filter(CSP.rm_id == u.id).count() if (u.role or "").upper() == "RM" else ""
                state = "LOGIN ON" if u.login_enabled and u.password_hash else "no login"
                locked = " (locked)" if u.locked_until and u.locked_until > auth._now() else ""
                print(f"  {u.id:4}  {(u.role or ''):6} {u.name[:28]:28} {(u.email or '-'):34} {state}{locked}"
                      f"{f'  {csps} CSPs' if csps != '' else ''}")
            return
        if not a.email and a.action != "enable-rm":
            sys.exit("--email is required")
        if a.action == "add-admin":
            u = _by_email(db, a.email)
            if u is None:
                u = InternalUser(name=a.name or a.email.split("@")[0], email=a.email.strip().lower(), role="ADMIN")
                db.add(u)
            u.role, u.login_enabled = "ADMIN", True
            u.password_hash = auth.hash_password(_ask_password())
        elif a.action == "enable-rm":
            u = _by_email(db, a.email) if a.email else None
            if u is None and a.name:
                u = db.query(InternalUser).filter(func.lower(InternalUser.name) == a.name.strip().lower(),
                                                  func.upper(InternalUser.role) == "RM").first()
            if u is None:
                sys.exit("No RM found with that email or name (run 'list' to see them).")
            if (u.role or "").upper() != "RM":
                sys.exit(f"{u.name} is {u.role}, not an RM.")
            if a.email:
                u.email = a.email.strip().lower()
            if not u.email:
                sys.exit("This RM has no email yet: pass --email.")
            u.login_enabled = True
            u.password_hash = auth.hash_password(_ask_password())
            print(f"{u.name} will see {db.query(CSP).filter(CSP.rm_id == u.id).count()} CSPs.")
        else:
            u = _by_email(db, a.email)
            if u is None:
                sys.exit("No user with that email.")
            if a.action == "password":
                u.password_hash = auth.hash_password(_ask_password())
                db.query(AdminSession).filter(AdminSession.user_id == u.id).delete()
            elif a.action == "disable":
                u.login_enabled = False
                db.query(AdminSession).filter(AdminSession.user_id == u.id).delete()
            elif a.action == "unlock":
                u.locked_until, u.failed_logins = None, 0
        db.commit()
        print(f"done: {a.action} {u.email} ({u.role})")
    finally:
        db.close()


if __name__ == "__main__":
    main()
