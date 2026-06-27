"""Shared lazy Gmail/Google OAuth helper.

Centralizes the credentials.json -> token.json -> service dance that reachout,
mailbox, and mailmerge each used to inline. Lazy imports keep google-* off the
import path until a tool actually needs Gmail, so servers still start without it.
"""
from __future__ import annotations

from pathlib import Path
from typing import Sequence

# Default scope used by the outreach servers (compose covers drafts + sends).
DEFAULT_GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.compose"]


def gmail_auth_status(data_dir: str | Path) -> dict:
    """Report where credentials live and whether auth is ready — no network, no import.

    Useful for an `auth_status` tool: tells the user exactly which file to drop in.
    """
    d = Path(data_dir)
    cred = d / "credentials.json"
    token = d / "token.json"
    return {
        "data_dir": str(d),
        "credentials_path": str(cred),
        "token_path": str(token),
        "credentials_present": cred.exists(),
        "token_present": token.exists(),
        "ready": cred.exists() or token.exists(),
        "hint": (
            f"Put your Desktop OAuth client JSON at {cred}; the first authorized "
            "call opens a browser and writes token.json next to it."
        ),
    }


def get_gmail_service(
    data_dir: str | Path,
    scopes: Sequence[str] | None = None,
    *,
    credentials_file: str = "credentials.json",
    token_file: str = "token.json",
    open_browser: bool = True,
    return_creds: bool = False,
):
    """Build an authorized Gmail API client from files in `data_dir` (lazy imports).

    Looks for `token_file` (cached creds) and `credentials_file` (Desktop OAuth
    client). Refreshes an expired token silently; otherwise runs the local-server
    OAuth flow (set open_browser=False to disable, e.g. in headless contexts).
    The refreshed/new token is persisted back to `token_file`.

    Raises RuntimeError with an actionable message if credentials are missing.
    """
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    scope_list = list(scopes) if scopes else list(DEFAULT_GMAIL_SCOPES)
    d = Path(data_dir)
    d.mkdir(parents=True, exist_ok=True)
    token_path = d / token_file
    cred_path = d / credentials_file

    creds = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), scope_list)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not cred_path.exists():
                raise RuntimeError(
                    f"Missing {cred_path}. Create a Desktop OAuth client in Google "
                    f"Cloud (Gmail API enabled) and save it there, then retry."
                )
            flow = InstalledAppFlow.from_client_secrets_file(str(cred_path), scope_list)
            if open_browser:
                creds = flow.run_local_server(port=0)
            else:
                creds = flow.run_console()
        token_path.write_text(creds.to_json())

    svc = build("gmail", "v1", credentials=creds)
    return (svc, creds) if return_creds else svc
