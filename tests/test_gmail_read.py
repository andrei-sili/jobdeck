"""The Gmail read surface (Phase 3): scopes, sync, metadata, labels.

A fake API object implements exactly the chained calls the module makes, so
every test pins the REQUEST SHAPE (params, formats, bounds) as well as the
answer handling — the wire is the contract here.
"""

import base64
import json

import httplib2
import pytest
from googleapiclient.errors import HttpError

from jobdeck import config, gmail


def _token_payload(**over):
    payload = {
        "token": "test-access-token",
        "refresh_token": "test-refresh-token",
        "client_id": "id.apps.googleusercontent.com",
        "client_secret": "test-client-secret",
        "token_uri": "https://oauth2.googleapis.com/token",
        "scopes": gmail.SCOPES,
        "expiry": "2099-01-01T00:00:00Z",
    }
    payload.update(over)
    return payload


def _http_error(status: int, reason: str = "boom") -> HttpError:
    return HttpError(
        resp=httplib2.Response({"status": str(status), "reason": reason}),
        content=b'{"error": {"message": "x"}}',
    )


class _Request:
    def __init__(self, answer):
        self._answer = answer

    def execute(self):
        if isinstance(self._answer, Exception):
            raise self._answer
        return self._answer


class FakeGmail:
    """Answers the exact call chains gmail.py's read functions make."""

    def __init__(self):
        self.profile = {"historyId": "h-100", "emailAddress": "x@example.com"}
        self.list_pages = [{"messages": [], "nextPageToken": None}]
        self.history_pages = [{"historyId": "h-100", "history": []}]
        self.message_store = {}
        self.label_store = []
        self.created = []
        self.create_error = None
        self.modified = []
        self.calls = []

    def users(self):
        return self

    def getProfile(self, userId):
        assert userId == "me"
        self.calls.append(("getProfile",))
        return _Request(self.profile)

    def messages(self):
        return _Messages(self)

    def history(self):
        return _History(self)

    def labels(self):
        return _Labels(self)


class _Messages:
    def __init__(self, fake):
        self.fake = fake

    def list(self, userId, q, pageToken, maxResults):
        assert userId == "me"
        self.fake.calls.append(("messages.list", q, pageToken, maxResults))
        index = 0 if pageToken is None else int(pageToken)
        page = dict(self.fake.list_pages[index])
        page = {"messages": page.get("messages", []),
                "nextPageToken": page.get("nextPageToken")}
        return _Request(page)

    def get(self, userId, id, format, metadataHeaders=None):
        assert userId == "me"
        self.fake.calls.append(("messages.get", id, format,
                                tuple(metadataHeaders or ())))
        answer = self.fake.message_store[id]
        if isinstance(answer, Exception):
            return _Request(answer)
        return _Request(answer[format])

    def modify(self, userId, id, body):
        assert userId == "me"
        self.fake.modified.append((id, body))
        return _Request({})


class _History:
    def __init__(self, fake):
        self.fake = fake

    def list(self, userId, startHistoryId, historyTypes, labelId, pageToken):
        assert userId == "me"
        assert historyTypes == "messageAdded"
        assert labelId == "INBOX"
        self.fake.calls.append(("history.list", startHistoryId, pageToken))
        index = 0 if pageToken is None else int(pageToken)
        answer = self.fake.history_pages[index]
        return _Request(answer)


class _Labels:
    def __init__(self, fake):
        self.fake = fake

    def list(self, userId):
        assert userId == "me"
        return _Request({"labels": list(self.fake.label_store)})

    def create(self, userId, body):
        assert userId == "me"
        if self.fake.create_error is not None:
            return _Request(self.fake.create_error)
        self.fake.created.append(body)
        created = {"id": f"Label_{len(self.fake.created)}",
                   "name": body["name"]}
        self.fake.label_store.append(created)
        return _Request(created)


@pytest.fixture()
def readable(data_dir, monkeypatch):
    """A saved token with both scopes plus the fake API."""
    config.TOKEN_PATH.write_text(json.dumps(_token_payload()), encoding="utf-8")
    fake = FakeGmail()
    monkeypatch.setattr(gmail, "service", lambda creds: fake)
    return fake


# -- scopes --------------------------------------------------------------------
def test_can_read_is_false_without_a_token(data_dir):
    assert gmail.can_read() is False


def test_can_read_is_false_on_a_pre_phase3_token(data_dir):
    config.TOKEN_PATH.write_text(
        json.dumps(_token_payload(scopes=["openid", gmail.SEND_SCOPE])),
        encoding="utf-8")
    assert gmail.can_read() is False
    assert gmail.has_scope(gmail.SEND_SCOPE) is True


def test_can_read_is_true_once_modify_was_granted(data_dir):
    config.TOKEN_PATH.write_text(json.dumps(_token_payload()), encoding="utf-8")
    assert gmail.can_read() is True


def test_sending_still_works_on_a_send_only_token(data_dir):
    """The scope split must never break the path that already worked: a token
    from before Phase 3 satisfies the send path's default requirement."""
    config.TOKEN_PATH.write_text(
        json.dumps(_token_payload(scopes=["openid", gmail.SEND_SCOPE])),
        encoding="utf-8")
    creds = gmail.load_credentials()
    assert gmail.SEND_SCOPE in creds.scopes


def test_reading_on_a_send_only_token_names_what_a_reconnect_adds(data_dir):
    config.TOKEN_PATH.write_text(
        json.dumps(_token_payload(scopes=["openid", gmail.SEND_SCOPE])),
        encoding="utf-8")
    with pytest.raises(gmail.GmailNotConnected, match="read permission"):
        gmail.load_credentials(gmail.MODIFY_SCOPE)


def test_an_unreadable_token_answers_cannot_read_rather_than_raising(data_dir):
    config.TOKEN_PATH.write_text("not json", encoding="utf-8")
    assert gmail.can_read() is False


# -- sync ----------------------------------------------------------------------
def test_profile_history_id(readable):
    assert gmail.profile_history_id() == "h-100"


def test_list_new_message_ids_walks_pages_and_bounds_the_total(readable):
    readable.list_pages = [
        {"messages": [{"id": "m-1"}, {"id": "m-2"}], "nextPageToken": "1"},
        {"messages": [{"id": "m-3"}, {"id": "m-4"}], "nextPageToken": None},
    ]
    assert gmail.list_new_message_ids("-from:me", 3) == ["m-1", "m-2", "m-3"]
    # the second page was asked for only the remainder
    assert ("messages.list", "-from:me", "1", 1) in readable.calls


def test_history_collects_added_messages_and_the_new_checkpoint(readable):
    readable.history_pages = [{
        "historyId": "h-200",
        "history": [
            {"messagesAdded": [{"message": {"id": "m-1", "threadId": "t-1"}}]},
            # the sync guide warns the same message may repeat across records
            {"messagesAdded": [{"message": {"id": "m-1", "threadId": "t-1"}},
                               {"message": {"id": "m-2", "threadId": "t-2"}}]},
        ],
    }]
    ids, checkpoint = gmail.history_added_messages("h-100", 50)
    assert ids == ["m-1", "m-2"]
    assert checkpoint == "h-200"


def test_an_expired_checkpoint_raises_its_own_error(readable):
    readable.history_pages = [_http_error(404, "notFound")]
    with pytest.raises(gmail.GmailHistoryExpired):
        gmail.history_added_messages("h-1", 50)


def test_other_history_failures_stay_plain_gmail_errors(readable):
    readable.history_pages = [_http_error(500, "backendError")]
    with pytest.raises(gmail.GmailError) as excinfo:
        gmail.history_added_messages("h-1", 50)
    assert not isinstance(excinfo.value, gmail.GmailHistoryExpired)


# -- metadata / raw ------------------------------------------------------------
def test_metadata_maps_fields_and_lowercases_headers(readable):
    readable.message_store["m-1"] = {"metadata": {
        "id": "m-1", "threadId": "t-9", "snippet": "Vielen Dank",
        "internalDate": "1755400000000", "sizeEstimate": 4321,
        "labelIds": ["INBOX", "UNREAD"],
        "payload": {"headers": [
            {"name": "From", "value": "HR <hr@firma.example>"},
            {"name": "Authentication-Results", "value": "mx.google.com; spf=pass"},
            # a second occurrence must NOT displace Gmail's own topmost one
            {"name": "Authentication-Results", "value": "forged"},
        ]},
    }}
    meta = gmail.get_message_metadata("m-1")
    assert meta["thread_id"] == "t-9"
    assert meta["internal_date_ms"] == 1755400000000
    assert meta["size_estimate"] == 4321
    assert meta["headers"]["from"] == "HR <hr@firma.example>"
    assert meta["headers"]["authentication-results"] == "mx.google.com; spf=pass"
    # the request asked for the named headers, not the whole envelope
    call = next(c for c in readable.calls if c[0] == "messages.get")
    assert call[2] == "metadata"
    assert "Authentication-Results" in call[3]


def test_raw_decodes_base64url_without_padding(readable):
    body = b"From: hr@firma.example\r\n\r\nSehr geehrter Herr ..."
    encoded = base64.urlsafe_b64encode(body).decode().rstrip("=")
    readable.message_store["m-1"] = {"raw": {"id": "m-1", "raw": encoded}}
    assert gmail.get_message_raw("m-1") == body


# -- labels --------------------------------------------------------------------
def test_ensure_labels_reuses_and_creates_in_order(readable):
    readable.label_store = [{"id": "Label_7", "name": "JobDeck"}]
    resolved = gmail.ensure_labels(["JobDeck", "JobDeck/Absagen"])
    assert resolved["JobDeck"] == "Label_7"
    assert resolved["JobDeck/Absagen"] == "Label_1"
    assert [b["name"] for b in readable.created] == ["JobDeck/Absagen"]
    assert readable.created[0]["labelListVisibility"] == "labelShow"


def test_ensure_labels_treats_409_as_already_exists(readable):
    """Gmail answers 409 for a name that exists — success, re-read the id."""
    readable.create_error = _http_error(409, "Label name exists or conflicts")
    # the label exists at Gmail, but only the re-list AFTER the 409 sees it —
    # the first list answers empty, forcing the create attempt
    listings = {"count": 0}
    original_list = _Labels.list

    def _list(self, userId):
        listings["count"] += 1
        if listings["count"] > 1:
            readable.label_store = [{"id": "Label_9", "name": "JobDeck"}]
        return original_list(self, userId)

    _Labels.list = _list
    try:
        resolved = gmail.ensure_labels(["JobDeck"])
    finally:
        _Labels.list = original_list
    assert resolved == {"JobDeck": "Label_9"}
    assert listings["count"] == 2  # the 409 branch really ran the re-list


def test_add_label_modifies_the_message(readable):
    gmail.add_label("m-1", "Label_3")
    assert readable.modified == [("m-1", {"addLabelIds": ["Label_3"]})]


def test_reading_requires_the_modify_scope(data_dir, monkeypatch):
    config.TOKEN_PATH.write_text(
        json.dumps(_token_payload(scopes=["openid", gmail.SEND_SCOPE])),
        encoding="utf-8")
    monkeypatch.setattr(gmail, "service",
                        lambda creds: pytest.fail("service built without scope"))
    with pytest.raises(gmail.GmailNotConnected):
        gmail.profile_history_id()


def test_a_truncated_history_listing_refuses_to_hand_back_a_checkpoint(readable):
    """`historyId` is the MAILBOX's current position, not the point this
    listing reached. Storing it after a bounded read would step over every
    message past the bound — permanently, since nothing lists them again."""
    readable.history_pages = [
        {"historyId": "h-900", "nextPageToken": "1",
         "history": [{"messagesAdded": [{"message": {"id": f"m-{i}"}}]}
                     for i in range(3)]},
        {"historyId": "h-900",
         "history": [{"messagesAdded": [{"message": {"id": f"m-{i}"}}]}
                     for i in range(3, 6)]},
    ]
    ids, checkpoint = gmail.history_added_messages("h-1", 2)
    assert ids == ["m-0", "m-1"]
    assert checkpoint == ""  # not drained — the caller keeps the old one


def test_a_complete_history_listing_does_hand_back_its_checkpoint(readable):
    readable.history_pages = [{
        "historyId": "h-900",
        "history": [{"messagesAdded": [{"message": {"id": "m-0"}}]}],
    }]
    ids, checkpoint = gmail.history_added_messages("h-1", 50)
    assert (ids, checkpoint) == (["m-0"], "h-900")
