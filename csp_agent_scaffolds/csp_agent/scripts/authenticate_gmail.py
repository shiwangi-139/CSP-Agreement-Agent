"""
scripts/authenticate_gmail.py
Run this once to complete the Google Workspace login and generate gmail_token.json.
Command:
    .venv/bin/python -m scripts.authenticate_gmail

The second inbox (read-only: the agent can't send from it or change it):
    GMAIL_SECOND_TOKEN=gmail_token_2.json .venv/bin/python -m scripts.authenticate_gmail --second
Sign in with the SECOND account in the browser that opens. It needs a browser,
so run it on a PC, then copy gmail_token_2.json into the project folder on the
server and add GMAIL_SECOND_TOKEN=gmail_token_2.json to .env there.
"""
import os
import sys

from app.comms.gmail_oauth import SECOND_SCOPES, get_gmail_service, second_token_file


def main():
    second = "--second" in sys.argv[1:]
    print("Testing Google Cloud Console Gmail API connection" + (" (second inbox, read-only)..." if second else "..."))
    if second:
        token = second_token_file()
        if token is None:
            sys.exit("Set GMAIL_SECOND_TOKEN first, e.g.  GMAIL_SECOND_TOKEN=gmail_token_2.json "
                     ".venv/bin/python -m scripts.authenticate_gmail --second")
        os.environ.setdefault("GMAIL_INTERACTIVE_AUTH", "1")
        service = get_gmail_service(token, SECOND_SCOPES)
    else:
        token = None
        service = get_gmail_service()
    profile = service.users().getProfile(userId="me").execute()
    print("\nSUCCESS! Authenticated successfully.")
    print(f"Connected Email Account : {profile.get('emailAddress')}")
    print(f"Total Messages in Inbox: {profile.get('messagesTotal')}")
    print(f"\n{token.name if token else 'gmail_token.json'} has been generated and saved in your project folder.")


if __name__ == "__main__":
    main()
