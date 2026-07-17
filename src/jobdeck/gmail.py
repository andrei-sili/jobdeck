"""Gmail connection and sending: OAuth tokens in, message ids out.

Synchronous by design — callers on the event loop go through asyncio.to_thread,
matching the sqlite and Anthropic-wrapper conventions in this codebase.

Scope policy: gmail.send only (a "sensitive" scope — the unverified personal-use
OAuth app keeps a non-expiring refresh token), plus openid/email so the UI can
show which account is connected. The "restricted" read scopes that reply
tracking will need are deliberately NOT requested here; they raise the OAuth
verification bar and are a Phase 3 decision.

Deliverability choices (researched 2026-07): the MIME body is
multipart/alternative (plain text + a faithful minimal HTML part) inside
multipart/mixed with the PDF — the same shape Gmail's own composer produces.
Message-ID and Date are left for Gmail to generate; attachment filenames are
forced to ASCII (RFC 2231 encodings are mishandled by common mail clients).
"""

import base64
import html
import logging
import os
import re
import threading
from email.headerregistry import Address
from email.message import EmailMessage
from pathlib import Path

import httplib2
import httpx
from google.auth.exceptions import GoogleAuthError, RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_httplib2 import AuthorizedHttp
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from jobdeck import config
from jobdeck.pdf import safe_filename

log = logging.getLogger(__name__)

SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"
SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    SEND_SCOPE,
]
USERINFO_ENDPOINT = "https://openidconnect.googleapis.com/v1/userinfo"
REVOKE_ENDPOINT = "https://oauth2.googleapis.com/revoke"

# token.json is written from send workers (refresh) and removed from the UI
# (Disconnect); serialize so a refresh can never resurrect a disconnected
# authorization, and two consent flows can never interleave their writes.
_token_lock = threading.Lock()
_connect_lock = threading.Lock()

# Bounds a hung request; a stuck send would otherwise hold the send lock.
REQUEST_TIMEOUT_S = 60.0
# The interactive consent flow blocks a worker thread until the browser
# redirect arrives — give up instead of hanging forever if it never does.
CONSENT_TIMEOUT_S = 300

_ADDR_RE = re.compile(r"^[^@\s.]+(\.[^@\s.]+)*@[^@\s.]+(\.[^@\s.]+)+$")


class GmailError(RuntimeError):
    """A Gmail API call failed."""


class GmailNotConnected(GmailError):
    """No usable Gmail authorization — connect (again) from Settings."""


class GmailRefused(GmailError):
    """Gmail answered and rejected the message: it was definitively NOT sent."""


class GmailUncertain(GmailError):
    """The request may have been accepted and only the response was lost.

    Callers must not treat this as a failed send — the message may already
    be in the recipient's inbox, so retrying risks a double-send."""


def is_connected() -> bool:
    """Cheap gate/UI check; load_credentials() is the real validation."""
    return config.TOKEN_PATH.exists()


def normalize_address(addr: str) -> str:
    """The wire form of an address: ASCII, with an IDNA-encoded domain.

    German postings do carry umlaut domains, but RFC 2047 encoded-words are
    illegal inside an addr-spec — IDNA is the only correct encoding, and
    without it the To header goes out malformed. Raises ValueError when the
    address cannot be represented on the wire."""
    addr = " ".join(addr.split())
    local, _, domain = addr.rpartition("@")
    if not local.isascii():
        raise ValueError("non-ASCII local part is not supported")
    if not domain.isascii():
        try:
            domain = domain.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise ValueError(f"invalid domain: {exc}") from exc
    return f"{local}@{domain}"


def is_plausible_address(addr: str) -> bool:
    """Just enough validation to refuse garbage recipients before a send."""
    if not _ADDR_RE.match(addr.strip()):
        return False
    try:
        normalize_address(addr)
    except ValueError:
        return False
    return True


def _save_token(creds: Credentials, only_if_exists: bool = False) -> None:
    """Persist the authorization with owner-only permissions.

    only_if_exists guards the refresh path: if the user disconnected while
    the refresh was in flight, writing would resurrect an authorization
    they believe is gone."""
    with _token_lock:
        if only_if_exists and not config.TOKEN_PATH.exists():
            log.info("Gmail was disconnected during a token refresh — "
                     "not restoring the token file")
            return
        fd = os.open(config.TOKEN_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                     0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(creds.to_json())
        os.chmod(config.TOKEN_PATH, 0o600)  # pre-existing file keeps 0600 too


def disconnect() -> None:
    """Revoke the authorization at Google, then remove it locally.

    Revoking first means a copy of token.json (a backup, a disk image)
    cannot mint access tokens after the user severed the connection.
    Best effort: a network failure must not block the local removal."""
    token = ""
    try:
        creds = Credentials.from_authorized_user_file(str(config.TOKEN_PATH))
        token = creds.refresh_token or creds.token or ""
    except (OSError, ValueError) as exc:
        log.info("no readable Gmail token to revoke: %s", exc)
    if token:
        try:
            httpx.post(REVOKE_ENDPOINT, data={"token": token}, timeout=30)
        except httpx.HTTPError as exc:
            log.warning("could not revoke the Gmail authorization at Google "
                        "(removing it locally anyway): %s", exc)
    with _token_lock:
        config.TOKEN_PATH.unlink(missing_ok=True)


def load_credentials() -> Credentials:
    """Load, validate and (if needed) refresh the saved authorization.

    Raises GmailNotConnected with a user-actionable message when the token
    is missing, unreadable, lacks the send scope, or was revoked (Google
    revokes Gmail-scoped tokens on password changes, among other causes).
    """
    if not config.TOKEN_PATH.exists():
        raise GmailNotConnected("Gmail is not connected — use Connect Gmail in Settings")
    try:
        creds = Credentials.from_authorized_user_file(str(config.TOKEN_PATH))
    except ValueError as exc:
        raise GmailNotConnected(
            f"the saved Gmail authorization is unreadable — reconnect in Settings ({exc})"
        ) from exc
    if SEND_SCOPE not in (creds.scopes or []):
        raise GmailNotConnected(
            "the saved Gmail authorization is missing the send permission — "
            "reconnect in Settings"
        )
    if creds.valid:
        return creds
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except RefreshError as exc:
            # Revoked (password change, user action): the token is dead — a
            # green "connected" tick based on the file would lie.
            config.TOKEN_PATH.unlink(missing_ok=True)
            raise GmailNotConnected(
                "the Gmail authorization was revoked (this happens after a "
                "Google password change) — reconnect in Settings"
            ) from exc
        _save_token(creds, only_if_exists=True)
        return creds
    raise GmailNotConnected("the saved Gmail authorization expired — reconnect in Settings")


def connect() -> str:
    """Run the interactive OAuth consent flow; returns the connected address.

    Blocks until the browser consent completes (or CONSENT_TIMEOUT_S) — run
    in a worker thread. The token lands in config.TOKEN_PATH.
    """
    # Imported here: google_auth_oauthlib pulls in the whole oauthlib stack,
    # which no other code path needs.
    from google_auth_oauthlib.flow import InstalledAppFlow

    if not config.CLIENT_SECRET_PATH.exists():
        raise GmailNotConnected(
            f"no OAuth client file at {config.CLIENT_SECRET_PATH} — create a "
            f"Desktop-app OAuth client in Google Cloud and save its JSON there"
        )
    if not _connect_lock.acquire(blocking=False):
        raise GmailError("a Gmail connection is already in progress — finish "
                         "it in the browser window that is already open")
    try:
        flow = InstalledAppFlow.from_client_secrets_file(
            str(config.CLIENT_SECRET_PATH), SCOPES
        )
        try:
            creds = flow.run_local_server(
                port=0,
                open_browser=True,
                timeout_seconds=CONSENT_TIMEOUT_S,
                success_message="JobDeck is connected to Gmail — you can "
                                "close this tab.",
            )
        except GmailError:
            raise
        except Exception as exc:  # oauthlib/socket errors, abandoned consent
            raise GmailError(
                f"Gmail authorization did not complete: {exc}") from exc
        _save_token(creds)
        return fetch_address(creds)
    finally:
        _connect_lock.release()


def fetch_address(creds: Credentials) -> str:
    """The authorized account's e-mail, for display and the From header.

    Best-effort: connection works without it, so failures return ''.
    """
    try:
        response = httpx.get(
            USERINFO_ENDPOINT,
            headers={"Authorization": f"Bearer {creds.token}"},
            timeout=30,
        )
        response.raise_for_status()
        return str(response.json().get("email", ""))
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("could not fetch the connected Gmail address: %s", exc)
        return ""


def service(creds: Credentials):
    """Build the Gmail API client. Module-level seam so tests stub it."""
    authed = AuthorizedHttp(creds, http=httplib2.Http(timeout=REQUEST_TIMEOUT_S))
    return build("gmail", "v1", http=authed, cache_discovery=False)


def _text_to_html(text: str) -> str:
    """Faithful minimal HTML twin of the plain-text part (no CSS, no links).

    Filters flag multipart messages whose parts diverge — generating the
    HTML from the exact text part makes divergence impossible."""
    paragraphs = [
        html.escape(p).replace("\n", "<br>")
        for p in re.split(r"\n{2,}", text.strip())
    ]
    body = "".join(f"<p>{p}</p>" for p in paragraphs if p)
    return f"<html><body>{body}</body></html>"


def build_mime(
    to: str,
    subject: str,
    text_body: str,
    from_name: str = "",
    from_addr: str = "",
    attachment: Path | None = None,
) -> EmailMessage:
    """Assemble the application e-mail (see module docstring for the shape).

    From is set only when the connected address is known — Gmail fills in
    (and enforces) the authenticated sender otherwise. Header values are
    whitespace-collapsed: they come from user-editable fields and must
    never smuggle line breaks into the header block.
    """
    message = EmailMessage()
    message["To"] = normalize_address(to)
    message["Subject"] = " ".join(subject.split())
    if from_addr:
        message["From"] = Address(
            display_name=" ".join(from_name.split()), addr_spec=from_addr.strip()
        )
    message.set_content(text_body)
    message.add_alternative(_text_to_html(text_body), subtype="html")
    if attachment is not None:
        filename = attachment.name
        if not filename.isascii():
            filename = f"{safe_filename(attachment.stem)}{attachment.suffix}"
        message.add_attachment(
            attachment.read_bytes(),
            maintype="application",
            subtype="pdf",
            filename=filename,
        )
    return message


def send_message(message: EmailMessage) -> tuple[str, str]:
    """Send one MIME message as the connected user.

    Returns (gmail_message_id, gmail_thread_id). Raises GmailNotConnected /
    GmailError with user-readable messages."""
    creds = load_credentials()
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
    try:
        response = (
            service(creds)
            .users()
            .messages()
            .send(userId="me", body={"raw": raw})
            .execute()
        )
    except HttpError as exc:
        # Gmail answered: the message was definitively not accepted.
        raise GmailRefused(f"Gmail refused the send: {exc.reason}") from exc
    except (httplib2.HttpLib2Error, GoogleAuthError, OSError) as exc:
        # A timeout or reset can also hit AFTER Gmail accepted the message,
        # with only the response lost — the send is ambiguous, not failed.
        raise GmailUncertain(f"could not reach Gmail: {exc}") from exc
    return str(response.get("id", "")), str(response.get("threadId", ""))
