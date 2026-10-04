"""One-time Google Calendar consent. Opens a browser; saves a refresh token to token_calendar.json.

Scopes: calendar.events (create/read/edit events) and gmail.send (send mail only; no read access).
"""

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/gmail.send",  # send-only: cannot read the mailbox
]

flow = InstalledAppFlow.from_client_secrets_file("oauth_client.json", SCOPES)
creds = flow.run_local_server(port=8765, prompt="consent", access_type="offline",
                              open_browser=True, timeout_seconds=300)
with open("token_calendar.json", "w") as f:
    f.write(creds.to_json())
print("saved token_calendar.json; refresh token present:", bool(creds.refresh_token))
