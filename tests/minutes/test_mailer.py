"""Sending the minutes out: who it goes to, how it looks, and what breaks.

No test here opens a socket. ``smtplib.SMTP`` and ``smtplib.SMTP_SSL`` are
replaced by a stand-in that records what it was asked to do, which is how the
things that matter — the recipients being in Bcc, the timeout being set, the
right security handshake for each mode — can be asserted without a mail server.
"""

from __future__ import annotations

import smtplib
import types

import pytest

from app.minutes import mailer
from app.minutes.transcript import Participant, Segment, SessionMeta, Transcript

FROM = "room@example.com"


# -- building blocks -----------------------------------------------------


def make_transcript(*, segments=None, participants=None, invited=None):
    meta = SessionMeta(
        session_id="20260828-090000-abcdef12",
        started_at="2026-08-28T09:00:00+00:00",
        ended_at="2026-08-28T09:12:00+00:00",
        title="Engineering Daily",
        provider="teams",
        room="Boardroom",
        invited=["dana@example.com"] if invited is None else invited,
    )
    if segments is None:
        segments = [
            Segment(0.0, 6.0, "We need to decide on the supplier.", speaker="Alice"),
            Segment(6.0, 12.0, "Quotes by Thursday.", track="far-end", speaker="Carol"),
        ]
    if participants is None:
        participants = [
            Participant("Alice", "alice@example.com", where="room", source="face"),
            Participant("Carol", "carol@example.com", where="remote", source="roster"),
        ]
    return Transcript(meta=meta, segments=segments, participants=participants)


def assert_plain_english(message: str) -> None:
    """An error an office administrator can read and act on."""
    assert message, "an empty error explains nothing to anybody"
    assert message[0].isupper(), message
    assert message.rstrip().endswith("."), message
    lowered = message.lower()
    for jargon in ("traceback", "smtplib", "errno", "<class", "b'"):
        assert jargon not in lowered, f"{jargon!r} leaked into: {message}"


@pytest.fixture()
def smtp(monkeypatch):
    """Replace both SMTP classes with a recorder.

    Set ``record.fail_at`` and ``record.error`` to make a step blow up; read
    ``record.session`` afterwards to see what the server was asked to do.
    """
    record = types.SimpleNamespace(sessions=[], fail_at=None, error=None)

    class FakeSMTP:
        kind = "plain"

        def __init__(self, host, port, timeout=None):
            self.host = host
            self.port = port
            self.timeout = timeout
            self.started_tls = False
            self.login_args = None
            self.sent = []
            self.quit_called = False
            record.sessions.append(self)
            self._maybe_fail("connect")

        def starttls(self, *args, **kwargs):
            self.started_tls = True
            self._maybe_fail("starttls")

        def login(self, username, password):
            self.login_args = (username, password)
            self._maybe_fail("login")

        def send_message(self, message, from_addr=None, to_addrs=None):
            self.sent.append((message, from_addr, to_addrs))
            self._maybe_fail("send")

        def quit(self):
            self.quit_called = True
            self._maybe_fail("quit")

        def _maybe_fail(self, step):
            if record.fail_at == step and record.error is not None:
                raise record.error

    class FakeSMTPSSL(FakeSMTP):
        kind = "ssl"

    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTPSSL)
    return record


def only_session(record):
    assert len(record.sessions) == 1, f"expected one connection, got {len(record.sessions)}"
    return record.sessions[0]


def sent_message(record):
    session = only_session(record)
    assert len(session.sent) == 1
    return session.sent[0]


@pytest.fixture()
def mail_config(config):
    """A working outgoing mail configuration."""
    changed, errors = config.update(
        {
            "ROOM_NAME": "Test Room",
            "MINUTES_EMAIL_ENABLED": True,
            "MINUTES_SMTP_HOST": "smtp.example.com",
            "MINUTES_SMTP_PORT": 587,
            "MINUTES_SMTP_SECURITY": "starttls",
            "MINUTES_SMTP_USERNAME": FROM,
            "MINUTES_SMTP_PASSWORD": "an-app-password",
            "MINUTES_EMAIL_FROM": FROM,
            "MINUTES_EMAIL_TO_ATTENDEES": True,
            "MINUTES_EMAIL_ALWAYS_TO": ["charlie@example.com"],
        }
    )
    assert not errors, errors
    return config


def a_summary(text="Overview\nThe team chose a supplier."):
    from app.minutes.summarize import Summary

    return Summary(text=text, model="claude-opus-5", ok=True)


# -- what is usable ------------------------------------------------------


class TestAvailability:
    def test_switched_off_says_so(self, config):
        ok, reason = mailer.available(config)
        assert ok is False
        assert "switched off" in reason
        assert_plain_english(reason)

    def test_no_server_names_the_settings_page(self, config):
        config.update({"MINUTES_EMAIL_ENABLED": True})
        ok, reason = mailer.available(config)
        assert ok is False
        assert "mail server" in reason
        assert_plain_english(reason)

    def test_no_from_address_is_reported(self, config):
        config.update(
            {"MINUTES_EMAIL_ENABLED": True, "MINUTES_SMTP_HOST": "smtp.example.com"}
        )
        ok, reason = mailer.available(config)
        assert ok is False
        assert "send the summary from" in reason
        assert_plain_english(reason)

    def test_the_smtp_username_stands_in_for_the_from_address(self, config):
        config.update(
            {
                "MINUTES_EMAIL_ENABLED": True,
                "MINUTES_SMTP_HOST": "smtp.example.com",
                "MINUTES_SMTP_USERNAME": FROM,
            }
        )
        assert mailer.available(config) == (True, "")

    def test_a_full_configuration_is_usable(self, mail_config):
        assert mailer.available(mail_config) == (True, "")


# -- who gets it ---------------------------------------------------------


class TestRecipients:
    def test_attendees_and_the_fixed_list(self, mail_config):
        who = mailer.recipients_for(make_transcript(), mail_config)
        assert who == [
            "alice@example.com",
            "carol@example.com",
            "dana@example.com",
            "charlie@example.com",
        ]

    def test_the_fixed_list_only(self, mail_config):
        mail_config.update({"MINUTES_EMAIL_TO_ATTENDEES": False})
        who = mailer.recipients_for(make_transcript(), mail_config)
        assert who == ["charlie@example.com"]

    def test_addresses_are_deduplicated_ignoring_case(self, mail_config):
        mail_config.update({"MINUTES_EMAIL_ALWAYS_TO": ["ALICE@Example.com"]})
        who = mailer.recipients_for(make_transcript(), mail_config)
        assert who == ["alice@example.com", "carol@example.com", "dana@example.com"]

    def test_things_that_are_not_addresses_are_dropped(self, mail_config):
        transcript = make_transcript(
            participants=[Participant("Nobody", "not-an-address", where="room")],
            invited=[""],
        )
        assert mailer.recipients_for(transcript, mail_config) == ["charlie@example.com"]

    def test_nobody_at_all(self, mail_config):
        mail_config.update({"MINUTES_EMAIL_ALWAYS_TO": []})
        transcript = make_transcript(participants=[], invited=[])
        assert mailer.recipients_for(transcript, mail_config) == []


# -- what arrives --------------------------------------------------------


class TestTheMessage:
    def test_a_summary_is_sent(self, mail_config, smtp):
        delivery = mailer.send_summary(mail_config, make_transcript(), a_summary())
        assert delivery.ok is True
        assert delivery.error == ""
        assert delivery.sent_to == [
            "alice@example.com",
            "carol@example.com",
            "dana@example.com",
            "charlie@example.com",
        ]
        assert delivery.to_dict()["ok"] is True

    def test_everybody_is_hidden_in_bcc(self, mail_config, smtp):
        mailer.send_summary(mail_config, make_transcript(), a_summary())
        message, from_addr, to_addrs = sent_message(smtp)

        # The privacy decision: some of these addresses come from face and
        # voice recognition, so nobody is shown the list they are on.
        assert message["To"] == FROM
        assert message["Cc"] is None
        bcc = message["Bcc"]
        for address in ("alice@example.com", "carol@example.com", "charlie@example.com"):
            assert address in bcc
        # The envelope carries the real recipients; smtplib strips the Bcc
        # header from the copy it actually transmits.
        assert to_addrs == mailer.recipients_for(make_transcript(), mail_config)
        assert from_addr == FROM
        assert FROM not in to_addrs, "the room does not need its own summary"

    def test_the_subject_says_room_meeting_and_day(self, mail_config, smtp):
        mailer.send_summary(mail_config, make_transcript(), a_summary())
        message, _, _ = sent_message(smtp)
        subject = message["Subject"]
        assert "Boardroom" in subject
        assert "Engineering Daily" in subject
        assert "28 August 2026" in subject

    def test_the_body_carries_the_summary_and_a_footer(self, mail_config, smtp):
        mailer.send_summary(mail_config, make_transcript(), a_summary())
        message, _, _ = sent_message(smtp)
        body = message.get_body(("plain",)).get_content()
        assert "Overview\nThe team chose a supplier." in body
        assert "automatically from a recording made in Boardroom" in body
        assert "In the room: Alice." in body
        assert "Joined remotely: Carol." in body
        assert "speaker labels are a best guess" in body

    def test_vacation_responders_are_told_to_stay_out_of_it(self, mail_config, smtp):
        mailer.send_summary(mail_config, make_transcript(), a_summary())
        message, _, _ = sent_message(smtp)
        assert message["Auto-Submitted"] == "auto-generated"

    def test_no_transcript_is_attached_by_default(self, mail_config, smtp):
        mailer.send_summary(mail_config, make_transcript(), a_summary())
        message, _, _ = sent_message(smtp)
        assert list(message.iter_attachments()) == []

    def test_the_transcript_is_attached_when_asked_for(self, mail_config, smtp):
        mail_config.update({"MINUTES_EMAIL_ATTACH_TRANSCRIPT": True})
        mailer.send_summary(mail_config, make_transcript(), a_summary())
        message, _, _ = sent_message(smtp)

        attachments = list(message.iter_attachments())
        assert len(attachments) == 1
        attachment = attachments[0]
        assert attachment.get_filename() == "transcript.txt"
        assert attachment.get_content_type() == "text/plain"
        assert attachment.get_content_charset() == "utf-8"
        assert "We need to decide on the supplier." in attachment.get_content()

    def test_a_plain_string_summary_is_accepted(self, mail_config, smtp):
        delivery = mailer.send_summary(mail_config, make_transcript(), "Just text.")
        assert delivery.ok is True
        message, _, _ = sent_message(smtp)
        assert "Just text." in message.get_body(("plain",)).get_content()

    def test_a_meeting_nobody_was_recognised_in(self, mail_config, smtp):
        transcript = make_transcript(participants=[], invited=[])
        delivery = mailer.send_summary(mail_config, transcript, a_summary())
        assert delivery.ok is True
        assert delivery.sent_to == ["charlie@example.com"]
        body = sent_message(smtp)[0].get_body(("plain",)).get_content()
        assert "Nobody present was identified by name." in body


# -- how it connects -----------------------------------------------------


class TestTheConnection:
    def test_starttls(self, mail_config, smtp):
        mailer.send_summary(mail_config, make_transcript(), a_summary())
        session = only_session(smtp)
        assert session.kind == "plain"
        assert session.started_tls is True
        assert session.host == "smtp.example.com"
        assert session.port == 587

    def test_implicit_ssl(self, mail_config, smtp):
        mail_config.update({"MINUTES_SMTP_SECURITY": "ssl", "MINUTES_SMTP_PORT": 465})
        mailer.send_summary(mail_config, make_transcript(), a_summary())
        session = only_session(smtp)
        assert session.kind == "ssl"
        assert session.started_tls is False
        assert session.port == 465

    def test_no_security_at_all(self, mail_config, smtp):
        mail_config.update({"MINUTES_SMTP_SECURITY": "none", "MINUTES_SMTP_PORT": 25})
        mailer.send_summary(mail_config, make_transcript(), a_summary())
        session = only_session(smtp)
        assert session.kind == "plain"
        assert session.started_tls is False

    def test_every_connection_has_a_timeout(self, mail_config, smtp):
        mailer.send_summary(mail_config, make_transcript(), a_summary())
        session = only_session(smtp)
        assert session.timeout == mailer.SMTP_TIMEOUT_SECONDS
        assert 0 < session.timeout <= 60

    def test_it_logs_in_when_there_is_an_account(self, mail_config, smtp):
        mailer.send_summary(mail_config, make_transcript(), a_summary())
        assert only_session(smtp).login_args == (FROM, "an-app-password")

    def test_it_does_not_log_in_to_an_open_relay(self, mail_config, smtp):
        mail_config.update({"MINUTES_SMTP_USERNAME": "", "MINUTES_EMAIL_FROM": FROM})
        mailer.send_summary(mail_config, make_transcript(), a_summary())
        assert only_session(smtp).login_args is None

    def test_the_connection_is_always_closed(self, mail_config, smtp):
        mailer.send_summary(mail_config, make_transcript(), a_summary())
        assert only_session(smtp).quit_called is True

    def test_a_rude_goodbye_does_not_undo_a_delivery(self, mail_config, smtp):
        smtp.fail_at = "quit"
        smtp.error = smtplib.SMTPServerDisconnected("no QUIT for you")
        delivery = mailer.send_summary(mail_config, make_transcript(), a_summary())
        assert delivery.ok is True, "the message was accepted before QUIT"


# -- when it does not work ----------------------------------------------


class TestFailures:
    def test_nothing_is_sent_when_the_feature_is_off(self, config, smtp):
        delivery = mailer.send_summary(config, make_transcript(), a_summary())
        assert delivery.ok is False
        assert "switched off" in delivery.error
        assert smtp.sessions == []

    def test_nobody_to_send_to(self, mail_config, smtp):
        mail_config.update({"MINUTES_EMAIL_ALWAYS_TO": []})
        transcript = make_transcript(participants=[], invited=[])
        delivery = mailer.send_summary(mail_config, transcript, a_summary())
        assert delivery.ok is False
        assert delivery.sent_to == []
        assert "email address" in delivery.error
        assert smtp.sessions == [], "no connection is opened with nobody to send to"
        assert_plain_english(delivery.error)

    def test_an_empty_summary_is_not_sent(self, mail_config, smtp):
        delivery = mailer.send_summary(mail_config, make_transcript(), a_summary(""))
        assert delivery.ok is False
        assert "no summary" in delivery.error
        assert smtp.sessions == []
        assert_plain_english(delivery.error)

    def test_a_failed_summary_is_not_sent(self, mail_config, smtp):
        from app.minutes.summarize import Summary

        failed = Summary(ok=False, error="Claude could not be reached.")
        delivery = mailer.send_summary(mail_config, make_transcript(), failed)
        assert delivery.ok is False
        assert smtp.sessions == []

    def test_a_meeting_with_no_transcript_still_sends_its_summary(
        self, mail_config, smtp
    ):
        mail_config.update({"MINUTES_EMAIL_ATTACH_TRANSCRIPT": True})
        transcript = make_transcript(segments=[])
        delivery = mailer.send_summary(mail_config, transcript, a_summary())
        assert delivery.ok is True
        attachments = list(sent_message(smtp)[0].iter_attachments())
        assert len(attachments) == 1
        assert attachments[0].get_content().strip() == ""

    @pytest.mark.parametrize(
        "step, error, expected",
        [
            (
                "login",
                smtplib.SMTPAuthenticationError(535, b"5.7.8 Username and Password not accepted"),
                "app password",
            ),
            (
                "send",
                smtplib.SMTPRecipientsRefused({"alice@example.com": (550, b"No such user")}),
                "refused every address",
            ),
            (
                "send",
                smtplib.SMTPSenderRefused(550, b"Not authorised", FROM),
                "refused to accept mail from",
            ),
            (
                "send",
                smtplib.SMTPServerDisconnected("Connection unexpectedly closed"),
                "closed the connection",
            ),
            (
                "send",
                smtplib.SMTPDataError(554, b"Message rejected"),
                "would not accept the summary",
            ),
            ("connect", TimeoutError("timed out"), "did not answer within 20 seconds"),
            ("connect", ConnectionRefusedError(111, "Connection refused"), "could not be reached"),
            ("send", ValueError("something nobody predicted"), "went wrong"),
        ],
    )
    def test_every_smtp_failure_becomes_a_sentence(
        self, mail_config, smtp, step, error, expected
    ):
        smtp.fail_at = step
        smtp.error = error
        delivery = mailer.send_summary(mail_config, make_transcript(), a_summary())

        assert delivery.ok is False
        assert delivery.sent_to == []
        assert expected in delivery.error
        assert_plain_english(delivery.error)
        assert str(error) not in delivery.error, "the raw response is not for a person"
        # However far it got, whatever was opened is let go of again. A
        # connection that never opened has nothing to close.
        if step != "connect":
            assert only_session(smtp).quit_called is True


# -- the test button -----------------------------------------------------


class TestTestMessage:
    def test_it_sends(self, mail_config, smtp):
        delivery = mailer.send_test(mail_config, "charlie@example.com")
        assert delivery.ok is True
        assert delivery.sent_to == ["charlie@example.com"]

        message, from_addr, to_addrs = sent_message(smtp)
        # One deliberate recipient who typed their own address: no list to hide.
        assert message["To"] == "charlie@example.com"
        assert message["Bcc"] is None
        assert to_addrs == ["charlie@example.com"]
        assert from_addr == FROM
        assert "Test Room" in message["Subject"]
        assert "test message" in message.get_content()

    def test_a_typo_is_caught_before_connecting(self, mail_config, smtp):
        delivery = mailer.send_test(mail_config, "charlie-at-example.com")
        assert delivery.ok is False
        assert smtp.sessions == []
        assert_plain_english(delivery.error)

    def test_an_unconfigured_room_says_what_is_missing(self, config, smtp):
        delivery = mailer.send_test(config, "charlie@example.com")
        assert delivery.ok is False
        assert "switched off" in delivery.error
        assert smtp.sessions == []

    def test_a_rejected_password_is_explained(self, mail_config, smtp):
        smtp.fail_at = "login"
        smtp.error = smtplib.SMTPAuthenticationError(535, b"5.7.8 rejected")
        delivery = mailer.send_test(mail_config, "charlie@example.com")
        assert delivery.ok is False
        assert "app password" in delivery.error
