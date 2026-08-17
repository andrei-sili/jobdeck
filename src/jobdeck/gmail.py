"""Gmail connection, sending and reading: OAuth tokens in, messages out.

Synchronous by design — callers on the event loop go through asyncio.to_thread,
matching the sqlite and Anthropic-wrapper conventions in this codebase.

Scope policy since Phase 3: gmail.send (sending) plus gmail.modify (reading
replies and applying the JobDeck labels), plus openid/email so the UI can show
which account is connected. gmail.modify is "restricted", and a consent run on
2026-08-05 proved the unverified personal-use OAuth app is granted it, with a
refresh token issued. Incremental authorization does not exist for installed
apps, so a token from before Phase 3 carries only the send scope: sending
keeps working on it, and every read path checks for the modify scope itself
and asks for a re-connect rather than failing obscurely.

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
MODIFY_SCOPE = "https://www.googleapis.com/auth/gmail.modify"
SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    SEND_SCOPE,
    MODIFY_SCOPE,
]

# What each scope buys, in the words the reconnect message uses.
_SCOPE_PURPOSE = {
    SEND_SCOPE: "send permission",
    MODIFY_SCOPE: "read permission for replies (gmail.modify)",
}
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


class GmailHistoryExpired(GmailError):
    """The stored history checkpoint is too old for an incremental sync.

    Documented Gmail behavior (a historyId is typically valid for at least a
    week); the caller must fall back to a full sync and re-baseline."""


def is_connected() -> bool:
    """Cheap gate/UI check; load_credentials() is the real validation."""
    return config.TOKEN_PATH.exists()


def has_scope(scope: str) -> bool:
    """Whether the SAVED authorization carries a scope — no network, no
    refresh. A pre-Phase-3 token has send only; the consent screen also lets
    the user untick a scope, so presence of the file proves nothing about
    either capability."""
    if not config.TOKEN_PATH.exists():
        return False
    try:
        creds = Credentials.from_authorized_user_file(str(config.TOKEN_PATH))
    except (OSError, ValueError):
        return False
    return scope in (creds.scopes or [])


def can_read() -> bool:
    """Whether reply reading may even be attempted — the UI/service gate."""
    return has_scope(MODIFY_SCOPE)


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


def load_credentials(required_scope: str = SEND_SCOPE) -> Credentials:
    """Load, validate and (if needed) refresh the saved authorization.

    `required_scope` is what the CALLER is about to do: the send path keeps
    working on a pre-Phase-3 token, and the reply reader asks for the modify
    scope and gets a message naming exactly what a re-connect would add.
    Raises GmailNotConnected with a user-actionable message when the token
    is missing, unreadable, lacks that scope, or was revoked (Google revokes
    Gmail-scoped tokens on password changes, among other causes).
    """
    if not config.TOKEN_PATH.exists():
        raise GmailNotConnected("Gmail is not connected — use Connect Gmail in Settings")
    try:
        creds = Credentials.from_authorized_user_file(str(config.TOKEN_PATH))
    except ValueError as exc:
        raise GmailNotConnected(
            f"the saved Gmail authorization is unreadable — reconnect in Settings ({exc})"
        ) from exc
    if required_scope not in (creds.scopes or []):
        purpose = _SCOPE_PURPOSE.get(required_scope, required_scope)
        raise GmailNotConnected(
            f"the saved Gmail authorization is missing the {purpose} — "
            f"reconnect in Settings"
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
        # oauthlib aborts the flow with a Warning when Google's token answer
        # lists the granted scopes in a different order (or expanded form)
        # than the request — routine with more than one Gmail scope. Relaxing
        # accepts the answer as issued; what was actually granted is then
        # enforced where it matters, per call, by load_credentials.
        os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")
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


# --------------------------------------------------------------------------
# Reading (Phase 3) — every function here requires the modify scope.
#
# Reads carry none of sending's ambiguity: nothing leaves the account, so a
# lost response costs nothing and the next scheduled pass simply asks again.
# One error class (GmailError) therefore covers transport and API failures
# alike, with GmailHistoryExpired split out because the caller must react to
# it differently (full re-sync) rather than merely retry later.
# --------------------------------------------------------------------------

# The headers the ingestion pass reads. Fetched by name so the metadata
# answer stays small; Authentication-Results is Gmail's OWN verdict on the
# sender (SPF/DKIM), which is what lets the receipt path refuse a spoofed
# confirmation, and the Auto-Submitted family is what tells an
# out-of-office machine answer from a human reply.
METADATA_HEADERS = (
    "From", "To", "Subject", "Date", "Message-ID",
    "Auto-Submitted", "Precedence", "List-Unsubscribe",
    "X-Autoreply", "X-Auto-Response-Suppress", "Authentication-Results",
)


def _read_service():
    return service(load_credentials(MODIFY_SCOPE))


def _execute(request):
    """Run one API request under the read error posture."""
    try:
        return request.execute()
    except HttpError as exc:
        raise GmailError(f"Gmail refused the request: {exc.reason}") from exc
    except (httplib2.HttpLib2Error, GoogleAuthError, OSError) as exc:
        raise GmailError(f"could not reach Gmail: {exc}") from exc


def profile_history_id() -> str:
    """The mailbox's current history checkpoint (users.getProfile, 1 unit)."""
    response = _execute(_read_service().users().getProfile(userId="me"))
    return str(response.get("historyId", ""))


def list_new_message_ids(query: str, max_results: int) -> list[str]:
    """Message ids matching a Gmail search, newest first, bounded.

    The full-sync path: q= is the whole Gmail search grammar, and the bound
    is over ALL pages — a first run against a busy mailbox must not walk
    years of archive in one pass."""
    svc = _read_service()
    ids: list[str] = []
    page_token = None
    while len(ids) < max_results:
        response = _execute(svc.users().messages().list(
            userId="me", q=query, pageToken=page_token,
            maxResults=min(100, max_results - len(ids)),
        ))
        ids.extend(str(m["id"]) for m in response.get("messages", []))
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    return ids[:max_results]


def history_added_messages(
    start_history_id: str, max_results: int
) -> tuple[list[str], str]:
    """Inbox arrivals since a checkpoint, plus the new checkpoint.

    The incremental path (users.history.list, 2 units): only messageAdded
    records for INBOX, so archive re-labelling can never look like new mail.
    Raises GmailHistoryExpired on the documented 404 — the checkpoint is
    typically valid for at least a week, and the caller then re-baselines
    with a full sync."""
    svc = _read_service()
    ids: list[str] = []
    seen: set[str] = set()
    latest = ""
    truncated = False
    page_token = None
    while True:
        try:
            response = svc.users().history().list(
                userId="me", startHistoryId=start_history_id,
                historyTypes="messageAdded", labelId="INBOX",
                pageToken=page_token,
            ).execute()
        except HttpError as exc:
            if exc.resp.status == 404:
                raise GmailHistoryExpired(
                    "the Gmail history checkpoint expired — full sync needed"
                ) from exc
            raise GmailError(f"Gmail refused the request: {exc.reason}") from exc
        except (httplib2.HttpLib2Error, GoogleAuthError, OSError) as exc:
            raise GmailError(f"could not reach Gmail: {exc}") from exc
        latest = str(response.get("historyId", latest))
        for record in response.get("history", []):
            for added in record.get("messagesAdded", []):
                message_id = str(added.get("message", {}).get("id", ""))
                # The sync guide warns the same message may appear in
                # several records; the bound below must count messages,
                # not records, or a chatty history bursts it.
                if message_id and message_id not in seen:
                    seen.add(message_id)
                    ids.append(message_id)
        page_token = response.get("nextPageToken")
        if len(ids) >= max_results:
            # More history than this pass may carry. `historyId` is the
            # MAILBOX's current position, not the point this listing
            # reached, so storing it would step over everything past the
            # bound — permanently, since nothing lists it again. An empty
            # checkpoint says "not drained" and the caller keeps the old one.
            truncated = bool(page_token) or len(ids) > max_results
            break
        if not page_token:
            break
    return ids[:max_results], ("" if truncated else latest)


def get_message_metadata(message_id: str) -> dict:
    """One message's envelope: headers, snippet, size — never the body.

    This is what the match cascade runs on, so the bodies of mail that
    belongs to nobody are never even fetched. Headers land lower-cased,
    first occurrence wins — for Authentication-Results the topmost header
    is the one Gmail itself stamped."""
    response = _execute(_read_service().users().messages().get(
        userId="me", id=message_id, format="metadata",
        metadataHeaders=list(METADATA_HEADERS),
    ))
    headers: dict[str, str] = {}
    for header in response.get("payload", {}).get("headers", []):
        name = str(header.get("name", "")).lower()
        if name and name not in headers:
            headers[name] = str(header.get("value", ""))
    return {
        "id": str(response.get("id", "")),
        "thread_id": str(response.get("threadId", "")),
        "snippet": str(response.get("snippet", "")),
        "internal_date_ms": int(response.get("internalDate", 0) or 0),
        "size_estimate": int(response.get("sizeEstimate", 0) or 0),
        "label_ids": [str(label) for label in response.get("labelIds", [])],
        "headers": headers,
    }


def get_message_raw(message_id: str) -> bytes:
    """The full RFC-822 message, for mail the cascade has already matched.

    format=raw + the email stdlib is the one decoding path that gets
    charset and transfer encoding right by construction; the parsed
    payload's per-part bytes have no documented charset contract."""
    response = _execute(_read_service().users().messages().get(
        userId="me", id=message_id, format="raw",
    ))
    raw = str(response.get("raw", ""))
    return base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))


def ensure_labels(names: list[str]) -> dict[str, str]:
    """Name → id for these labels, creating what is missing.

    Order matters to Gmail's UI only in that a parent ('JobDeck') should
    exist before its children — the caller passes it first. Creating a name
    that appeared meanwhile answers 409; that is success, re-read the id."""
    svc = _read_service()
    def _existing() -> dict[str, str]:
        response = _execute(svc.users().labels().list(userId="me"))
        return {str(label["name"]): str(label["id"])
                for label in response.get("labels", [])}
    by_name = _existing()
    resolved: dict[str, str] = {}
    for name in names:
        if name in by_name:
            resolved[name] = by_name[name]
            continue
        try:
            created = svc.users().labels().create(
                userId="me",
                body={"name": name, "labelListVisibility": "labelShow",
                      "messageListVisibility": "show"},
            ).execute()
            resolved[name] = str(created["id"])
            by_name[name] = resolved[name]
        except HttpError as exc:
            if exc.resp.status == 409:
                by_name = _existing()
                if name in by_name:
                    resolved[name] = by_name[name]
                    continue
            raise GmailError(
                f"could not create the Gmail label {name!r}: {exc.reason}"
            ) from exc
        except (httplib2.HttpLib2Error, GoogleAuthError, OSError) as exc:
            raise GmailError(f"could not reach Gmail: {exc}") from exc
    return resolved


def set_labels(message_id: str, add: list[str], remove: list[str]) -> None:
    """Make one message carry exactly the labels it should (messages.modify).

    Add AND remove in one call, because a verdict that changes has to take
    its old label with it: a mail relabelled Einladung while still carrying
    Absagen is worse in his inbox than one carrying nothing, and Gmail's
    own filters would see both."""
    body: dict[str, list[str]] = {}
    if add:
        body["addLabelIds"] = add
    if remove:
        body["removeLabelIds"] = remove
    if not body:
        return
    _execute(_read_service().users().messages().modify(
        userId="me", id=message_id, body=body,
    ))
