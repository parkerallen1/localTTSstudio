"""One-time Gmail OAuth consent for doc_watcher's completion emails.

doc_watcher.py sends the finished audio back to whoever shared a doc using the
Gmail API over OAuth (Google's recommended path — no app password). Run this
ONCE on a machine with a browser to grant that permission and write a token
file, then point doc_watcher.json's "email.oauth_token" at it (copying the
file to the watcher machine first if that's a different box — the token is not
machine-specific).

Google Cloud setup (same project as the Drive service account):
  1. APIs & Services -> Library -> enable "Gmail API".
  2. APIs & Services -> OAuth consent screen:
       - User type: External.
       - Add your Gmail address under "Test users".
       - Then click "PUBLISH APP" (Production). This matters: while an app is
         in "Testing", Google expires its refresh tokens after 7 days, which
         would silently break the pipeline every week. Published apps don't
         expire them. You'll still see an "unverified app" screen at consent
         time — it's your own app, so click through it.
  3. APIs & Services -> Credentials -> Create Credentials -> OAuth client ID
       -> Application type: "Desktop app". Download the client-secret JSON.

Then:
    pip install google-auth-oauthlib
    python gmail_auth.py --client-secrets /path/to/client_secret.json

A browser opens; sign in as the account you want emails to come FROM and
approve. The token is written to ~/.qwen_tts_studio/gmail_token.json (chmod
600). That address goes in doc_watcher.json as "email.from_address".
"""
import argparse
import os
import sys

DEFAULT_TOKEN = os.path.expanduser("~/.qwen_tts_studio/gmail_token.json")
SCOPES = ["https://www.googleapis.com/auth/gmail.send"]


def main():
    ap = argparse.ArgumentParser(description="Grant doc_watcher permission to send Gmail (one-time OAuth consent).")
    ap.add_argument("--client-secrets", required=True,
                    help="path to the OAuth 'Desktop app' client-secret JSON from Google Cloud Console")
    ap.add_argument("--token", default=DEFAULT_TOKEN,
                    help=f"where to write the resulting token (default {DEFAULT_TOKEN})")
    args = ap.parse_args()

    if not os.path.exists(args.client_secrets):
        sys.exit(f"client-secrets file not found: {args.client_secrets}")

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        sys.exit("Missing dependency: pip install google-auth-oauthlib")

    flow = InstalledAppFlow.from_client_secrets_file(args.client_secrets, SCOPES)
    # Desktop-app clients allow loopback redirects, so a local server on an
    # ephemeral port works without registering a redirect URI. Must run on a
    # machine with a browser; copy the token to the watcher host afterward.
    creds = flow.run_local_server(port=0, prompt="consent")

    token_path = os.path.expanduser(args.token)
    os.makedirs(os.path.dirname(token_path), exist_ok=True)
    with open(token_path, "w") as f:
        f.write(creds.to_json())
    os.chmod(token_path, 0o600)

    print(f"\nAuthorized. Token written to {token_path}")
    print("Set doc_watcher.json -> email.oauth_token to that path")
    print("(copy the file to the watcher machine first if it's a different box).")


if __name__ == "__main__":
    main()
