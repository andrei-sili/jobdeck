import json
import stat
from email.message import EmailMessage
from types import SimpleNamespace

import httplib2
import pytest
from googleapiclient.errors import HttpError

from jobdeck import config, gmail


# -- MIME construction ---------------------------------------------------------
def _mime(**over):
    values = dict(
        to="hr@firma.de",
        subject="Bewerbung als Python Entwickler, K-17 – Max Muster",
        text_body="Sehr geehrte Frau Weber,\n\nanbei meine Bewerbung.\n\n"
                  "Mit freundlichen Grüßen\nMax Muster",
    )
    values.update(over)
    return gmail.build_mime(**values)


def _pdf(tmp_path, name="Bewerbung_Max_Muster_Firma.pdf"):
    path = tmp_path / name
    path.write_bytes(b"%PDF-1.4 fake")
    return path


def test_build_mime_shape_is_mixed_with_alternative_and_pdf(tmp_path):
    message = _mime(attachment=_pdf(tmp_path))
    assert message.get_content_type() == "multipart/mixed"
    parts = list(message.iter_parts())
    assert parts[0].get_content_type() == "multipart/alternative"
    alternatives = [p.get_content_type() for p in parts[0].iter_parts()]
    assert alternatives == ["text/plain", "text/html"]
    assert parts[1].get_content_type() == "application/pdf"
    assert parts[1].get_filename() == "Bewerbung_Max_Muster_Firma.pdf"
    assert parts[1].get_content() == b"%PDF-1.4 fake"


def test_build_mime_without_attachment_is_alternative_only():
    message = _mime()
    assert message.get_content_type() == "multipart/alternative"


def test_build_mime_text_and_html_parts_carry_the_same_content(tmp_path):
    message = _mime(attachment=_pdf(tmp_path))
    alternative = next(message.iter_parts())
    text, html_part = list(alternative.iter_parts())
    assert text.get_content().strip() == (
        "Sehr geehrte Frau Weber,\n\nanbei meine Bewerbung.\n\n"
        "Mit freundlichen Grüßen\nMax Muster"
    )
    rendered = html_part.get_content()
    assert "Sehr geehrte Frau Weber,</p>" in rendered
    assert "Mit freundlichen Grüßen<br>Max Muster" in rendered


def test_build_mime_html_part_escapes_markup():
    message = _mime(text_body="Hallo <script>alert(1)</script>\n\nGruß & Dank")
    text, html_part = list(message.iter_parts())
    rendered = html_part.get_content()
    assert "<script>" not in rendered
    assert "&lt;script&gt;" in rendered
    assert "Gruß &amp; Dank" in rendered


def test_build_mime_leaves_message_id_and_date_to_gmail():
    message = _mime()
    assert message["Message-ID"] is None
    assert message["Date"] is None


def test_build_mime_from_header_only_when_address_known():
    assert _mime()["From"] is None
    message = _mime(from_name="Max Muster", from_addr="max@gmail.com")
    assert str(message["From"]) == "Max Muster <max@gmail.com>"


def test_build_mime_subject_with_umlauts_roundtrips():
    subject = "Bewerbung als Anwendungsentwickler für Prüfsysteme – Max Müller"
    assert _mime(subject=subject)["Subject"] == subject


def test_build_mime_collapses_header_newlines():
    message = _mime(
        to=" hr@firma.de ",
        subject="Bewerbung als Dev\nX-Evil: 1",
        from_name="Max\nMuster",
        from_addr="max@gmail.com",
    )
    assert message["To"] == "hr@firma.de"
    assert message["Subject"] == "Bewerbung als Dev X-Evil: 1"
    assert "X-Evil" not in message.keys()


def test_build_mime_transliterates_non_ascii_attachment_name(tmp_path):
    message = _mime(attachment=_pdf(tmp_path, "Bewerbung_Müller_&_Söhne.pdf"))
    attachment = list(message.iter_parts())[1]
    assert attachment.get_filename() == "Bewerbung_Mueller_Soehne.pdf"
    assert attachment.get_filename().isascii()


# -- address plausibility ------------------------------------------------------
@pytest.mark.parametrize("addr,ok", [
    ("hr@firma.de", True),
    ("  hr@firma.de  ", True),
    ("bewerbung@sub.firma-gruppe.de", True),
    ("", False),
    ("hr@firma", False),
    ("hr firma.de", False),
    ("hr@@firma.de", False),
    ("hr@firma.de\nBcc: evil@x.de", False),
])
def test_is_plausible_address(addr, ok):
    assert gmail.is_plausible_address(addr) is ok


# -- credentials ---------------------------------------------------------------
def _token_payload(**over):
    # Deliberately unlike Google's real token shapes (ya29. / 1//): a public
    # repo must not train its own secret scan to ignore those prefixes.
    payload = {
        "token": "test-access-token",
        "refresh_token": "test-refresh-token",
        "client_id": "id.apps.googleusercontent.com",
        "client_secret": "secret",
        "token_uri": "https://oauth2.googleapis.com/token",
        "scopes": gmail.SCOPES,
        "expiry": "2099-01-01T00:00:00Z",
    }
    payload.update(over)
    return payload


def test_is_connected_reflects_token_file(data_dir):
    assert gmail.is_connected() is False
    config.TOKEN_PATH.write_text("{}", encoding="utf-8")
    assert gmail.is_connected() is True


def test_load_credentials_without_token_raises(data_dir):
    with pytest.raises(gmail.GmailNotConnected, match="Connect Gmail"):
        gmail.load_credentials()


def test_load_credentials_unreadable_token_raises(data_dir):
    config.TOKEN_PATH.write_text("not json", encoding="utf-8")
    with pytest.raises(gmail.GmailNotConnected, match="unreadable"):
        gmail.load_credentials()


def test_load_credentials_missing_send_scope_raises(data_dir):
    payload = _token_payload(scopes=["openid"])
    config.TOKEN_PATH.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(gmail.GmailNotConnected, match="send permission"):
        gmail.load_credentials()


def test_load_credentials_valid_token_needs_no_refresh(data_dir):
    config.TOKEN_PATH.write_text(json.dumps(_token_payload()), encoding="utf-8")
    creds = gmail.load_credentials()
    assert creds.token == "test-access-token"


def test_load_credentials_revoked_refresh_removes_dead_token(data_dir, monkeypatch):
    from google.auth.exceptions import RefreshError

    payload = _token_payload(expiry="2020-01-01T00:00:00Z")
    config.TOKEN_PATH.write_text(json.dumps(payload), encoding="utf-8")

    def refuse(self, request):
        raise RefreshError("invalid_grant")

    monkeypatch.setattr(
        "google.oauth2.credentials.Credentials.refresh", refuse
    )
    with pytest.raises(gmail.GmailNotConnected, match="revoked"):
        gmail.load_credentials()
    assert not config.TOKEN_PATH.exists()


def test_save_token_is_owner_only(data_dir):
    creds = SimpleNamespace(to_json=lambda: '{"token": "t"}')
    gmail._save_token(creds)
    mode = stat.S_IMODE(config.TOKEN_PATH.stat().st_mode)
    assert mode == 0o600


def test_connect_without_client_secret_raises(data_dir):
    with pytest.raises(gmail.GmailNotConnected, match="OAuth client file"):
        gmail.connect()


def test_successful_refresh_is_persisted(data_dir, monkeypatch):
    payload = _token_payload(expiry="2020-01-01T00:00:00Z")
    config.TOKEN_PATH.write_text(json.dumps(payload), encoding="utf-8")

    def refresh(self, request):
        self.token = "refreshed-access-token"
        self.expiry = None  # google-auth treats None as "never expires"

    monkeypatch.setattr("google.oauth2.credentials.Credentials.refresh", refresh)
    creds = gmail.load_credentials()
    assert creds.token == "refreshed-access-token"
    # the next send must not have to refresh again
    assert json.loads(config.TOKEN_PATH.read_text())["token"] \
        == "refreshed-access-token"


def test_refresh_does_not_resurrect_a_disconnected_token(data_dir, monkeypatch):
    """Disconnect during an in-flight refresh must win — otherwise the app
    keeps sending on an authorization the user believes is severed."""
    payload = _token_payload(expiry="2020-01-01T00:00:00Z")
    config.TOKEN_PATH.write_text(json.dumps(payload), encoding="utf-8")

    def refresh(self, request):
        self.token = "refreshed-access-token"
        self.expiry = None
        config.TOKEN_PATH.unlink()  # the user clicks Disconnect right now

    monkeypatch.setattr("google.oauth2.credentials.Credentials.refresh", refresh)
    gmail.load_credentials()
    assert not config.TOKEN_PATH.exists()
    assert gmail.is_connected() is False


def test_disconnect_revokes_at_google_then_removes_the_token(
    data_dir, monkeypatch
):
    config.TOKEN_PATH.write_text(json.dumps(_token_payload()), encoding="utf-8")
    posted = {}

    def fake_post(url, data, timeout):
        posted["url"] = url
        posted["token"] = data["token"]
        return SimpleNamespace(status_code=200)

    monkeypatch.setattr(gmail.httpx, "post", fake_post)
    gmail.disconnect()
    assert posted["url"] == gmail.REVOKE_ENDPOINT
    assert posted["token"] == "test-refresh-token"  # revoking it kills access
    assert not config.TOKEN_PATH.exists()


def test_disconnect_removes_the_token_even_if_revoking_fails(
    data_dir, monkeypatch
):
    import httpx

    config.TOKEN_PATH.write_text(json.dumps(_token_payload()), encoding="utf-8")

    def failing_post(url, data, timeout):
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(gmail.httpx, "post", failing_post)
    gmail.disconnect()
    assert not config.TOKEN_PATH.exists()


def test_disconnect_without_a_token_is_a_noop(data_dir):
    gmail.disconnect()
    assert not config.TOKEN_PATH.exists()


# -- address normalisation -----------------------------------------------------
def test_normalize_address_idna_encodes_umlaut_domains():
    assert gmail.normalize_address("bewerbung@müller.de") \
        == "bewerbung@xn--mller-kva.de"
    assert gmail.normalize_address("hr@firma.de") == "hr@firma.de"


def test_umlaut_domain_yields_a_valid_ascii_to_header(tmp_path):
    message = _mime(to="bewerbung@müller.de")
    assert message["To"] == "bewerbung@xn--mller-kva.de"
    # encoded-words are illegal inside an addr-spec: the To line must be plain
    to_line = next(line for line in message.as_bytes().split(b"\n")
                   if line.startswith(b"To:"))
    assert to_line == b"To: bewerbung@xn--mller-kva.de"


def test_normalize_address_rejects_a_non_ascii_local_part():
    with pytest.raises(ValueError, match="local part"):
        gmail.normalize_address("bewerbungü@firma.de")
    assert gmail.is_plausible_address("bewerbungü@firma.de") is False


# -- sending -------------------------------------------------------------------
class StubService:
    """Mimics service.users().messages().send(...).execute()."""

    def __init__(self, response=None, error=None):
        self.response = response or {"id": "m-1", "threadId": "t-1"}
        self.error = error
        self.sent_bodies = []

    def users(self):
        return self

    def messages(self):
        return self

    def send(self, userId, body):
        assert userId == "me"
        self.sent_bodies.append(body)
        return self

    def execute(self):
        if self.error is not None:
            raise self.error
        return self.response


@pytest.fixture()
def connected(data_dir, monkeypatch):
    """A valid saved token plus a stubbed API service."""
    config.TOKEN_PATH.write_text(json.dumps(_token_payload()), encoding="utf-8")
    stub = StubService()
    monkeypatch.setattr(gmail, "service", lambda creds: stub)
    return stub


def test_send_message_returns_gmail_ids(connected):
    message_id, thread_id = gmail.send_message(_mime())
    assert (message_id, thread_id) == ("m-1", "t-1")
    assert len(connected.sent_bodies) == 1
    assert set(connected.sent_bodies[0]) == {"raw"}


def test_send_message_raw_is_urlsafe_base64(connected):
    gmail.send_message(_mime())
    raw = connected.sent_bodies[0]["raw"]
    assert raw.isascii() and "+" not in raw and "/" not in raw


def test_send_message_requires_connection(data_dir):
    with pytest.raises(gmail.GmailNotConnected):
        gmail.send_message(_mime())


def test_send_message_wraps_api_errors(connected):
    connected.error = HttpError(
        resp=httplib2.Response({"status": "403", "reason": "quotaExceeded"}),
        content=b'{"error": {"message": "quota"}}',
    )
    with pytest.raises(gmail.GmailError, match="Gmail refused"):
        gmail.send_message(_mime())


def test_send_message_wraps_network_errors(connected):
    connected.error = TimeoutError("timed out")
    with pytest.raises(gmail.GmailError, match="could not reach Gmail"):
        gmail.send_message(_mime())


def test_stub_send_never_wrote_a_real_token_outside_tmp(data_dir):
    # Guard for the fixture itself: the redirected paths stay inside tmp.
    assert str(config.TOKEN_PATH).startswith(str(data_dir))
    assert str(config.CLIENT_SECRET_PATH).startswith(str(data_dir))


def test_mime_roundtrip_through_bytes_parses_back():
    import email
    from email import policy

    message = _mime()
    parsed = email.message_from_bytes(message.as_bytes(), policy=policy.default)
    assert isinstance(parsed, EmailMessage) or hasattr(parsed, "iter_parts")
    assert parsed["Subject"] == message["Subject"]
