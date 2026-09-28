"""
scripts/authenticate_gmail.py
Run this once to complete the Google Workspace login and generate gmail_token.json.
Command:
    .venv/bin/python -m scripts.authenticate_gmail
"""

from app.comms.gmail_oauth import get_gmail_service

def main():
    print("Testing Google Cloud Console Gmail API connection...")
    service = get_gmail_service()
    profile = service.users().getProfile(userId="me").execute()
    print("\nSUCCESS! Authenticated successfully.")
    print(f"Connected Email Account : {profile.get('emailAddress')}")
    print(f"Total Messages in Inbox: {profile.get('messagesTotal')}")
    print("\ngmail_token.json has been generated and saved in your project folder.")

if __name__ == "__main__":
    main()