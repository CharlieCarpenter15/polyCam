"""Putting the minutes in people's inboxes without telling on anybody.

The awkward part of emailing a meeting summary is not SMTP, it is the address
list. Some of those addresses came from a calendar invitation, but others came
from the appliance recognising a face or a voice — so the recipient list is
itself a record of who this room believes was in it. Putting that list in the
``To:`` header would hand every recipient a copy of it, which is a great deal
more than anyone agreed to when they walked into a meeting room. Everybody is
therefore addressed by ``Bcc``, with ``To`` pointing back at the room's own
address, and the envelope naming only the real recipients.

Beyond that the rules are the ones any appliance service follows here: the
standard library only, so switching the feature on never needs a package; a
timeout on every connection, because a hung SMTP session would otherwise wedge
the worker thread that processes the meeting; and a failure is a sentence
somebody can act on rather than an exception nobody sees.
"""

from __future__ import annotations

import logging
import smtplib
from dataclasses import asdict, dataclass, field
from datetime import datetime
from email.message import EmailMessage
from typing import Any

from ..config import ConfigManager
from ..logging_setup import get_logger, log_event
from .transcript import Transcript

log = get_logger("minutes.email")

#: The three ways a provider will take a connection.
SECURITY_STARTTLS = "starttls"
SECURITY_SSL = "ssl"
SECURITY_NONE = "none"

#: Every socket operation is bounded. A mail server that accepts a connection
#: and then says nothing is a normal enough failure on an office network, and
#: without this it would hold the meeting-processing thread until the appliance
#: was restarted.
SMTP_TIMEOUT_SECONDS = 20.0

#: What the transcript is called when it is attached.
TRANSCRIPT_FILENAME = "transcript.txt"


def _s(value: Any) -> str:
    return str(value or "").strip()


@dataclass
class Delivery:
    """What happened when the summary was sent out."""

    ok: bool = False
    sent_to: list[str] = field(default_factory=list)
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def available(config: ConfigManager) -> tuple[bool, str]:
    """``(can we send mail, and if not why not)``."""
    if not config.bool_("MINUTES_EMAIL_ENABLED"):
        return False, "Emailing the summary is switched off."
    if not config.str_("MINUTES_SMTP_HOST"):
        return False, (
            "No outgoing mail server has been set, so the summary cannot be "
            "sent. Add your provider's SMTP server on the Settings page."
        )
    if not _from_address(config):
        return False, (
            "There is no address to send the summary from. Fill in either "
            "“Send from” or the SMTP username on the Settings page."
        )
    return True, ""


def recipients_for(transcript: Transcript, config: ConfigManager) -> list[str]:
    """Everybody who should get this summary, in a sensible order.

    Deduplicated case-insensitively — an address from the calendar and the same
    address typed into the always-send list are one person — but kept in the
    form they were written in, because that is how they are shown back to
    whoever configured them.
    """
    candidates: list[str] = []
    if config.bool_("MINUTES_EMAIL_TO_ATTENDEES"):
        candidates.extend(transcript.recipients())
    candidates.extend(config.list_("MINUTES_EMAIL_ALWAYS_TO"))

    out: list[str] = []
    seen: set[str] = set()
    for address in candidates:
        clean = _s(address)
        if "@" not in clean:
            continue
        key = clean.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(clean)
    return out


def send_summary(
    config: ConfigManager, transcript: Transcript, summary: Any
) -> Delivery:
    """Email one meeting's minutes. Never raises."""
    usable, reason = available(config)
    if not usable:
        return Delivery(error=reason)

    # A Summary is what the caller has; a plain string is what a test or a
    # future caller may have. Both are readable, so both are accepted.
    text = getattr(summary, "text", None)
    if text is None:
        text = summary
    text = str(text or "").strip()
    if not text:
        return Delivery(
            error="There is no summary to send for this meeting, so nothing "
            "was emailed."
        )

    recipients = recipients_for(transcript, config)
    if not recipients:
        return Delivery(
            error="Nobody in this meeting had an email address, so the summary "
            "was not sent. Add an address under “Always also send to”, or "
            "enrol the people this room should recognise."
        )

    sender = _from_address(config)
    message = EmailMessage()
    message["Subject"] = _subject(transcript, config)
    message["From"] = sender
    # Everyone is addressed by Bcc: see the module docstring. ``To`` still has
    # to say something, and the room's own address is the honest answer — the
    # summary really was sent by this room, to itself, about its own meeting.
    message["To"] = sender
    message["Bcc"] = ", ".join(recipients)
    # Stops the summary being answered by every out-of-office responder in the
    # building, which on a large invitation list is a small avalanche.
    message["Auto-Submitted"] = "auto-generated"
    message.set_content(f"{text}\n\n{_footer(transcript, config)}")

    if config.bool_("MINUTES_EMAIL_ATTACH_TRANSCRIPT"):
        message.add_attachment(
            transcript.render_text(),
            subtype="plain",
            charset="utf-8",
            filename=TRANSCRIPT_FILENAME,
        )

    ok, error = _deliver(config, message, recipients)
    if not ok:
        return Delivery(error=error)

    # The addresses themselves are never logged: the journal is read over a
    # shoulder far more often than this mailbox is, and a count answers the
    # only question anyone asks of it.
    log_event(
        log,
        logging.INFO,
        "minutes.summary_emailed",
        session=transcript.session_id,
        recipients=len(recipients),
        attached=config.bool_("MINUTES_EMAIL_ATTACH_TRANSCRIPT"),
    )
    return Delivery(ok=True, sent_to=recipients)


def send_test(config: ConfigManager, to: str) -> Delivery:
    """Send a test message, for the “send a test email” button. Never raises."""
    usable, reason = available(config)
    if not usable:
        return Delivery(error=reason)

    address = _s(to)
    if "@" not in address:
        return Delivery(error="That does not look like an email address.")

    sender = _from_address(config)
    room = config.str_("ROOM_NAME") or "This meeting room"
    message = EmailMessage()
    message["Subject"] = f"{room} — test message"
    message["From"] = sender
    # One deliberate recipient who typed their own address into the Settings
    # page, so there is no list to protect and the plain header is clearer.
    message["To"] = address
    message["Auto-Submitted"] = "auto-generated"
    message.set_content(
        f"This is a test message from {room}.\n"
        "\n"
        "If you are reading it, the appliance can reach your mail server and "
        "the meeting summaries it writes will arrive the same way.\n"
        "\n"
        "Nothing else was sent and no meeting was recorded to produce this."
    )

    ok, error = _deliver(config, message, [address])
    if not ok:
        return Delivery(error=error)
    log_event(log, logging.INFO, "minutes.test_email_sent")
    return Delivery(ok=True, sent_to=[address])


# -- the connection ------------------------------------------------------


def _deliver(
    config: ConfigManager, message: EmailMessage, recipients: list[str]
) -> tuple[bool, str]:
    """Hand one message to the mail server. ``(sent, why it was not)``."""
    host = config.str_("MINUTES_SMTP_HOST")
    port = config.int_("MINUTES_SMTP_PORT")
    security = config.str_("MINUTES_SMTP_SECURITY") or SECURITY_STARTTLS
    username = config.str_("MINUTES_SMTP_USERNAME")
    password = config.str_("MINUTES_SMTP_PASSWORD")
    sender = _from_address(config)

    server = None
    try:
        if security == SECURITY_SSL:
            server = smtplib.SMTP_SSL(host, port, timeout=SMTP_TIMEOUT_SECONDS)
        else:
            server = smtplib.SMTP(host, port, timeout=SMTP_TIMEOUT_SECONDS)
            if security == SECURITY_STARTTLS:
                server.starttls()
        # Only log in when there is an account to log in as: an internal relay
        # authenticates by address or by network, and offering it an empty
        # username is an error rather than a no-op.
        if username:
            server.login(username, password)
        # The envelope is given explicitly so that it carries the real
        # recipients and nothing else — in particular not the room's own
        # address, which appears in ``To`` only to keep the Bcc list private.
        server.send_message(message, from_addr=sender, to_addrs=recipients)
    except smtplib.SMTPAuthenticationError:
        return False, (
            "The mail server rejected the username and password. With Gmail or "
            "Microsoft 365 this has to be an app password created for this "
            "appliance, not the account's own password."
        )
    except smtplib.SMTPRecipientsRefused:
        return False, (
            "The mail server refused every address the summary was addressed "
            "to. Check the addresses, and whether your provider allows this "
            "account to send to people outside your organisation."
        )
    except smtplib.SMTPSenderRefused:
        return False, (
            f"The mail server refused to accept mail from “{sender}”. Most "
            "providers only allow an account to send as its own address."
        )
    except smtplib.SMTPServerDisconnected:
        return False, (
            "The mail server closed the connection part-way through. That is "
            "usually the wrong security setting for the port — “starttls” with "
            "port 587, or “ssl” with port 465."
        )
    except smtplib.SMTPException:
        return False, (
            "The mail server would not accept the summary. The journal has the "
            "exact response it gave."
        )
    except TimeoutError:
        return False, (
            f"The mail server did not answer within {int(SMTP_TIMEOUT_SECONDS)} "
            "seconds. Check the server name and port on the Settings page."
        )
    except OSError:
        return False, (
            f"The mail server “{host}” could not be reached. Check the server "
            "name and port, and that this room is on the network."
        )
    except Exception:
        # A bug here must not take down the thread that is processing the
        # meeting, so it is logged with its traceback and reported as a
        # sentence like every other failure.
        log.exception("minutes.email_crashed")
        return False, (
            "Something went wrong while sending the summary. The journal has "
            "the details."
        )
    finally:
        # Quitting is a courtesy, and a server that is rude about it has still
        # accepted the message — so a failure here must not turn a delivered
        # summary into a reported failure.
        if server is not None:
            try:
                server.quit()
            except Exception:
                pass
    return True, ""


# -- what it looks like when it arrives ----------------------------------


def _from_address(config: ConfigManager) -> str:
    """The address the summary comes from, falling back to the SMTP account."""
    return config.str_("MINUTES_EMAIL_FROM") or config.str_("MINUTES_SMTP_USERNAME")


def _subject(transcript: Transcript, config: ConfigManager) -> str:
    """Room, meeting and date — what someone scans an inbox for."""
    room = transcript.meta.room or config.str_("ROOM_NAME") or "Meeting room"
    title = transcript.meta.title or "Meeting"
    when = _meeting_date(transcript, config)
    parts = [room, title, when]
    return " — ".join(part for part in parts if part)


def _meeting_date(transcript: Transcript, config: ConfigManager) -> str:
    """The day the meeting happened, in the room's own timezone."""
    started = _s(transcript.meta.started_at)
    if not started:
        return ""
    try:
        moment = datetime.fromisoformat(started)
    except ValueError:
        # An unparseable timestamp is still better than no date at all, and
        # the first ten characters of anything ISO-shaped are the date.
        return started[:10]
    if moment.tzinfo is not None:
        try:
            moment = moment.astimezone(config.tz())
        except (ValueError, OSError):
            pass
    # Written out rather than with %d so the day has no leading zero, which is
    # how a date is written in an email subject line.
    return f"{moment.day} {moment:%B %Y}"


def _footer(transcript: Transcript, config: ConfigManager) -> str:
    """Where this email came from, and how much to trust it.

    Every recipient is told that a recording was made, in which room, and that
    the names attached to what was said are a machine's best guess. Someone who
    was only invited and never came needs that first sentence, and someone
    reading an action point with their name on it needs the last one.
    """
    room = transcript.meta.room or config.str_("ROOM_NAME") or "a meeting room"
    facts = transcript.summary_context()
    in_room = [n for n in facts.get("in_room") or [] if _s(n)]
    remote = [n for n in facts.get("remote") or [] if _s(n)]

    who: list[str] = []
    if in_room:
        who.append(f"In the room: {', '.join(in_room)}.")
    if remote:
        who.append(f"Joined remotely: {', '.join(remote)}.")
    if not who:
        who.append("Nobody present was identified by name.")

    return (
        "--\n"
        f"This summary was written automatically from a recording made in "
        f"{room}. {' '.join(who)} The transcript it was written from is "
        "machine-generated, so words are misheard and the speaker labels are a "
        "best guess: check anything important, and anything with your name "
        "against it, before acting on it."
    )
