"""Finding the join link in a calendar event.

These cases are drawn from the shapes real calendars actually produce: Teams
links buried in HTML, Outlook SafeLinks rewriting, Google Meet in LOCATION,
Zoom with a passcode, and events with no online link at all.
"""

from __future__ import annotations

import pytest

from app.meeting_links import (
    extract_meeting,
    find_urls,
    provider_for_url,
    provider_label,
    unwrap_url,
)

TEAMS_URL = (
    "https://teams.microsoft.com/l/meetup-join/"
    "19%3ameeting_ABCDEF%40thread.v2/0?context=%7b%22Tid%22%3a%22t%22%7d"
)


class TestProviderRecognition:
    @pytest.mark.parametrize(
        "url,expected",
        [
            (TEAMS_URL, "teams"),
            ("https://teams.live.com/meet/9391234567", "teams"),
            ("https://teams.microsoft.us/l/meetup-join/x/0", "teams"),
            ("https://meet.google.com/abc-defg-hij", "meet"),
            ("https://us02web.zoom.us/j/8412345678", "zoom"),
            ("https://acme.zoom.us/w/123456", "zoom"),
            ("https://acme.webex.com/acme/j.php?MTID=m1", "webex"),
            ("https://meet.jit.si/SomeRoom", "other"),
        ],
    )
    def test_known_providers(self, url, expected):
        provider = provider_for_url(url)
        assert provider is not None and provider.id == expected

    @pytest.mark.parametrize(
        "url",
        [
            "https://zoom.us/pricing",
            "https://example.com/agenda.pdf",
            "https://intranet.local/rooms/4",
            "",
            "not a url",
        ],
    )
    def test_non_meeting_urls(self, url):
        assert provider_for_url(url) is None

    def test_labels_are_human_readable(self):
        assert provider_label("teams") == "Microsoft Teams"
        assert provider_label("meet") == "Google Meet"
        assert provider_label("") == "Online meeting"


class TestExtraction:
    def test_teams_link_inside_html(self):
        url, provider = extract_meeting(
            description=f'<div>Click <a href="{TEAMS_URL}">Join the meeting now</a></div>'
        )
        assert provider.id == "teams"
        assert url == TEAMS_URL

    def test_google_meet_in_location(self):
        url, provider = extract_meeting(
            location="https://meet.google.com/abc-defg-hij", description="Catch-up"
        )
        assert provider.id == "meet"
        assert url == "https://meet.google.com/abc-defg-hij"

    def test_url_property_wins_over_the_body(self):
        url, provider = extract_meeting(
            url=TEAMS_URL, description="old link https://meet.google.com/aaa-bbbb-ccc"
        )
        assert provider.id == "teams"

    def test_trailing_punctuation_is_trimmed(self):
        url, _ = extract_meeting(description="Dial in at https://meet.google.com/xyz-pqrs-tuv.")
        assert url == "https://meet.google.com/xyz-pqrs-tuv"

    def test_angle_brackets_are_trimmed(self):
        url, _ = extract_meeting(description="Join <https://meet.google.com/qqq-wwww-eee>")
        assert url == "https://meet.google.com/qqq-wwww-eee"

    def test_html_entities_are_decoded(self):
        url, _ = extract_meeting(
            description="Join &lt;https://meet.google.com/aaa-bbbb-ccc&gt; today"
        )
        assert url == "https://meet.google.com/aaa-bbbb-ccc"

    def test_zoom_passcode_is_preserved(self):
        url, provider = extract_meeting(
            description="Join Zoom Meeting\nhttps://us02web.zoom.us/j/841?pwd=SECRET"
        )
        assert provider.id == "zoom"
        assert "pwd=SECRET" in url

    def test_event_with_no_link(self):
        url, provider = extract_meeting(description="Bring the agenda", location="Room 4")
        assert url is None and provider is None

    def test_automatable_provider_is_preferred(self):
        url, provider = extract_meeting(
            description=f"Backup: https://whereby.com/room and {TEAMS_URL}"
        )
        assert provider.id == "teams"

    def test_teams_dedicated_property(self):
        url, provider = extract_meeting(url=TEAMS_URL, location="Microsoft Teams Meeting")
        assert provider.id == "teams" and url == TEAMS_URL


#: The shape Google Calendar actually exports for a Meet event: one long
#: DESCRIPTION holding the Meet link, then a tel.meet dial-in link, then a
#: calendar.google.com link. Picking either of the last two would send the room
#: to a phone-numbers page or to Google Calendar instead of the meeting.
GOOGLE_EXPORT_DESCRIPTION = (
    "-::~:~::~:~:~:~:~:~:~:~:~:~:~:~::~:~::-\n"
    "Do not edit this section of the description.\n\n"
    "This event has a video call.\n"
    "Join: https://meet.google.com/qkj-mzrn-xub\n"
    "(SG) +65 3138 0345 PIN: 419283746#\n"
    "View more phone numbers: https://tel.meet/qkj-mzrn-xub?pin=8471028374619\n\n"
    "View your event at "
    "https://calendar.google.com/calendar/event?action=VIEW&eid=abc123\n"
    "-::~:~::~:~:~:~:~:~:~:~:~:~:~:~::~:~::-"
)


class TestGoogleCalendarExport:
    """The exact path a Google Workspace room calendar takes."""

    def test_the_meet_link_is_found_in_the_description(self):
        url, provider = extract_meeting(description=GOOGLE_EXPORT_DESCRIPTION)
        assert provider.id == "meet"
        assert url == "https://meet.google.com/qkj-mzrn-xub"

    def test_the_dial_in_link_is_not_chosen(self):
        url, _ = extract_meeting(description=GOOGLE_EXPORT_DESCRIPTION)
        assert "tel.meet" not in url, "a phone-numbers page is not the meeting"

    def test_the_calendar_link_is_not_chosen(self):
        url, _ = extract_meeting(description=GOOGLE_EXPORT_DESCRIPTION)
        assert "calendar.google.com" not in url

    def test_a_room_name_in_location_does_not_confuse_it(self):
        url, provider = extract_meeting(
            location="Boardroom (8)", description=GOOGLE_EXPORT_DESCRIPTION
        )
        assert provider.id == "meet" and "meet.google.com" in url

    def test_the_conference_property_is_honoured(self):
        """Google sets X-GOOGLE-CONFERENCE, which ics.py maps onto `url`."""
        url, provider = extract_meeting(
            url="https://meet.google.com/qkj-mzrn-xub",
            description=GOOGLE_EXPORT_DESCRIPTION,
        )
        assert provider.id == "meet" and url == "https://meet.google.com/qkj-mzrn-xub"


class TestSafeLinks:
    def test_outlook_safelinks_is_unwrapped(self):
        wrapped = (
            "https://eur03.safelinks.protection.outlook.com/?url="
            "https%3A%2F%2Fteams.microsoft.com%2Fl%2Fmeetup-join%2F19%253ameeting_Z%2F0"
            "&data=05%7C01&sdata=abc%3D&reserved=0"
        )
        assert unwrap_url(wrapped) == (
            "https://teams.microsoft.com/l/meetup-join/19%3ameeting_Z/0"
        )

    def test_encoding_inside_the_target_survives(self):
        """Double-decoding would corrupt a Teams id and its context parameter."""
        wrapped = (
            "https://eur03.safelinks.protection.outlook.com/?url="
            "https%3A%2F%2Fteams.microsoft.com%2Fl%2Fmeetup-join%2F19%253ameeting_Z%2F0"
            "%3Fcontext%3D%257b%2522Tid%2522%257d&data=x"
        )
        unwrapped = unwrap_url(wrapped)
        assert "19%3ameeting_Z" in unwrapped
        assert "context=%7b%22Tid%22%7d" in unwrapped

    def test_urldefense_v3_is_unwrapped(self):
        wrapped = (
            "https://urldefense.com/v3/__https://meet.google.com/def-ghij-klm__;!!ABC!xyz$"
        )
        assert unwrap_url(wrapped) == "https://meet.google.com/def-ghij-klm"

    def test_a_plain_url_is_left_alone(self):
        assert unwrap_url(TEAMS_URL) == TEAMS_URL

    def test_a_wrapper_with_no_target_is_left_alone(self):
        wrapped = "https://eur03.safelinks.protection.outlook.com/?data=only"
        assert unwrap_url(wrapped) == wrapped


class TestUrlScanning:
    def test_urls_are_deduplicated_in_order(self):
        found = find_urls(
            "https://a.example/one https://b.example/two https://a.example/one"
        )
        assert found == ["https://a.example/one", "https://b.example/two"]

    def test_href_targets_are_found_even_with_link_text(self):
        found = find_urls('<a href="https://meet.google.com/abc-defg-hij">Click here</a>')
        assert "https://meet.google.com/abc-defg-hij" in found

    def test_markup_is_not_mistaken_for_a_url(self):
        assert find_urls("<p>No links here</p>") == []

    def test_empty_input_is_safe(self):
        assert find_urls("", None, "   ") == []
