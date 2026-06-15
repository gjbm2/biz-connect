"""Shared Google service-account auth for the Google connectors (gdocs, gsheets).

Credentials come from the central store (config.service_account_file()). Access to
any individual file/sheet is gated by SHARING that file with the service-account
email — so the blast radius stays tiny regardless of the broad scopes requested.

Impersonation (domain-wide delegation) is OPT-IN via the per-user central store only
(GOOGLE_IMPERSONATE_SUBJECT in secrets.env). When set, Google calls act as that
Workspace user — needed so newly created Docs are owned by a real user (the service
account has no Drive storage of its own). It is deliberately NOT read from the
committed connections.yaml, so a shared repo can never make a collaborator silently
impersonate someone. Enabling it requires the SA's client id to be authorised for the
drive/documents scopes in the Workspace admin console; otherwise calls 403.
"""
from __future__ import annotations

import json
import sys

from . import config

DRIVE = "https://www.googleapis.com/auth/drive"
DOCS = "https://www.googleapis.com/auth/documents"
SHEETS = "https://www.googleapis.com/auth/spreadsheets"


def _credentials(scopes, subject=None):
    try:
        from google.oauth2 import service_account
    except ImportError:
        sys.exit("google-auth missing — run via the launcher "
                 "(scripts/bizconnect.py bootstraps the central-store venv).")
    key = config.service_account_file()
    if not key.exists():
        sys.exit(
            f"Google service-account key not found at {key}.\n"
            f"Drop your service-account JSON there, or set GOOGLE_SERVICE_ACCOUNT_FILE "
            f"in {config.home() / 'secrets.env'}.  (`bizconnect doctor` checks this.)"
        )
    creds = service_account.Credentials.from_service_account_file(str(key), scopes=list(scopes))
    if subject:
        creds = creds.with_subject(subject)
    return creds


def build(api: str, version: str, scopes, subject=None):
    """Build a Google API client. If `subject` is set, impersonate that Workspace
    user via domain-wide delegation (the SA's client_id must be authorised for the
    requested scopes in the Workspace admin console)."""
    try:
        from googleapiclient.discovery import build as _build
    except ImportError:
        sys.exit("google-api-python-client missing — run via the launcher.")
    return _build(api, version, credentials=_credentials(scopes, subject), cache_discovery=False)


def impersonation_subject(data=None):
    """The Workspace user to impersonate for Google calls, if configured. Read ONLY from
    the per-user central store (GOOGLE_IMPERSONATE_SUBJECT) — never from the committed
    connections.yaml, so a shared repo can't make collaborators impersonate someone.
    (`data` is accepted for call-site symmetry but intentionally ignored.)"""
    return config.secret("GOOGLE_IMPERSONATE_SUBJECT")


def client_email():
    key = config.service_account_file()
    try:
        return json.loads(key.read_text(encoding="utf-8")).get("client_email")
    except Exception:
        return None


def http_error(e):
    """(status, message) from a googleapiclient HttpError."""
    status = getattr(getattr(e, "resp", None), "status", "?")
    try:
        detail = json.loads(e.content.decode()).get("error", {}).get("message", "")
    except Exception:
        detail = str(e)
    return status, detail


def comments_list(drive, file_id):
    """All comments on a Drive file (Doc), with anchor + quoted text + replies. Needs the
    DRIVE scope (already used by the Google connectors). Paginates fully."""
    fields = ("nextPageToken,comments(id,content,resolved,anchor,createdTime,modifiedTime,"
              "quotedFileContent(value),author(displayName),"
              "replies(content,createdTime,author(displayName)))")
    out, token = [], None
    while True:
        resp = drive.comments().list(fileId=file_id, fields=fields, pageSize=100,
                                     includeDeleted=False, pageToken=token).execute()
        out.extend(resp.get("comments", []))
        token = resp.get("nextPageToken")
        if not token:
            break
    return out


def comment_resolve(drive, file_id, comment_id, note="Resolved via biz-connect."):
    """Resolve a comment thread by posting a reply with action=resolve (idempotent close-out)."""
    return drive.replies().create(
        fileId=file_id, commentId=comment_id, fields="id,action",
        body={"content": note, "action": "resolve"}).execute()
