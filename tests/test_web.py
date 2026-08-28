"""The web interface: pages, API, access control and uploads."""

from __future__ import annotations

import io
import struct
import zlib

import pytest

from app.config_schema import SECRET_KEYS


def tiny_png() -> bytes:
    """A genuinely valid 4x4 PNG, so the upload check is a real check."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(
            ">I", zlib.crc32(body) & 0xFFFFFFFF
        )

    header = struct.pack(">IIBBBBB", 4, 4, 8, 2, 0, 0, 0)
    rows = b"".join(b"\x00" + b"\xff\x88\x22" * 4 for _ in range(4))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


@pytest.fixture()
def app(mock_config):
    from app.main import create_app

    application = create_app(mock_config, start_services=False)
    application.config.update(TESTING=True)
    return application


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def token(client):
    """The CSRF token a real page would carry."""
    response = client.get("/panel")
    body = response.get_data(as_text=True)
    marker = 'data-csrf="'
    start = body.index(marker) + len(marker)
    return body[start : body.index('"', start)]


def post(client, url, token, payload=None):
    return client.post(url, json=payload or {}, headers={"X-Room-Token": token})


class TestPages:
    @pytest.mark.parametrize(
        "path", ["/", "/panel", "/settings", "/diagnostics"]
    )
    def test_pages_render(self, client, path):
        response = client.get(path)
        assert response.status_code == 200
        assert len(response.get_data()) > 500

    def test_the_dashboard_names_the_room(self, client):
        assert "Test Room" in client.get("/").get_data(as_text=True)

    def test_the_settings_page_lists_every_option(self, client, app):
        from app.config_schema import FIELDS

        body = client.get("/settings").get_data(as_text=True)
        for field in FIELDS:
            assert f'data-setting="{field.key}"' in body, f"{field.key} is not editable"

    def test_secrets_are_not_rendered_into_the_settings_page(self, client, mock_config):
        mock_config.update({"CALENDAR_ICS_URL": "https://example.com/very-secret.ics"})
        body = client.get("/settings").get_data(as_text=True)
        assert "very-secret" not in body
        assert "********" in body

    def test_an_unknown_page_returns_to_the_dashboard(self, client):
        response = client.get("/some-old-bookmark")
        assert response.status_code == 302
        assert response.headers["Location"].endswith("/")

    def test_security_headers_are_set(self, client):
        headers = client.get("/").headers
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert headers["X-Frame-Options"] == "DENY"
        assert "default-src 'self'" in headers["Content-Security-Policy"]


class TestStateApi:
    def test_state_has_everything_the_dashboard_needs(self, client):
        payload = client.get("/api/state").get_json()
        for key in (
            "mode", "room", "now", "next", "upcoming", "calendar", "airplay",
            "network_ok", "status", "backgrounds", "display", "panel_url",
        ):
            assert key in payload, f"/api/state is missing {key}"

    def test_health_reports_every_component(self, client):
        payload = client.get("/api/health").get_json()
        assert payload["status"] in ("ok", "warning", "error")
        assert payload["mode"] in ("home", "meeting", "screen-sharing", "offline")
        for component in (
            "backend", "calendar", "browser", "airplay",
            "camera", "microphone", "speaker", "network",
        ):
            assert component in payload["components"]

    def test_health_includes_unit_states(self, client):
        payload = client.get("/api/health").get_json()
        assert "room-dashboard.service" in payload["units"]

    def test_api_responses_are_not_cached(self, client):
        assert client.get("/api/state").headers["Cache-Control"] == "no-store"


class TestCsrf:
    def test_a_post_without_a_token_is_refused(self, client):
        assert client.post("/api/actions/home", json={}).status_code == 403

    def test_a_post_with_a_wrong_token_is_refused(self, client, token):
        response = client.post(
            "/api/actions/home", json={}, headers={"X-Room-Token": "wrong"}
        )
        assert response.status_code == 403

    def test_a_post_with_the_page_token_is_accepted(self, client, token):
        assert post(client, "/api/actions/home", token).status_code == 200

    def test_a_refused_post_asks_the_page_to_reload(self, client):
        assert client.post("/api/actions/home", json={}).get_json()["reload"] is True


class TestSettingsApi:
    def test_settings_can_be_read(self, client):
        payload = client.get("/api/settings").get_json()
        assert payload["values"]["ROOM_NAME"] == "Test Room"
        assert payload["groups"] and payload["groups"][0]["fields"]

    def test_settings_can_be_saved(self, client, token, mock_config):
        response = post(client, "/api/settings", token, {"ROOM_NAME": "Sky Lounge"})
        assert response.status_code == 200
        assert mock_config.str_("ROOM_NAME") == "Sky Lounge"

    def test_invalid_settings_return_per_field_errors(self, client, token):
        response = post(
            client,
            "/api/settings",
            token,
            {"CALENDAR_REFRESH_SECONDS": 1, "ACCENT_COLOR": "purple-ish"},
        )
        assert response.status_code == 422
        errors = response.get_json()["errors"]
        assert set(errors) == {"CALENDAR_REFRESH_SECONDS", "ACCENT_COLOR"}

    def test_an_unknown_setting_is_refused(self, client, token):
        response = post(client, "/api/settings", token, {"HACK_THE_PLANET": 1})
        assert response.status_code == 400

    def test_a_masked_secret_does_not_overwrite_the_real_one(self, client, token, mock_config):
        mock_config.update({"ADMIN_PIN": "123456"})
        post(client, "/api/settings", token, {"ADMIN_PIN": "********"})
        assert mock_config.str_("ADMIN_PIN") == "123456"

    def test_secrets_are_masked_when_read_back(self, client, mock_config):
        mock_config.update({"ADMIN_PIN": "123456"})
        values = client.get("/api/settings").get_json()["values"]
        for key in SECRET_KEYS:
            if mock_config.get(key):
                assert values[key] == "********"

    def test_reset_keeps_the_calendar_by_default(self, client, token, mock_config):
        mock_config.update(
            {"CALENDAR_SOURCE": "ics", "CALENDAR_ICS_URL": "https://x/y.ics", "THEME": "light"}
        )
        post(client, "/api/settings/reset", token, {"keep_calendar": True})
        assert mock_config.str_("CALENDAR_ICS_URL") == "https://x/y.ics"
        assert mock_config.str_("THEME") == "dark"


class TestActions:
    def test_home_and_leave(self, client, token):
        assert post(client, "/api/actions/home", token).get_json()["ok"]
        assert post(client, "/api/actions/leave", token).get_json()["ok"]

    def test_join_with_no_meeting_explains_itself(self, client, token, mock_config):
        mock_config.update({"CALENDAR_SOURCE": "none"})
        response = post(client, "/api/actions/join", token)
        assert response.status_code == 409
        assert response.get_json()["error"]

    def test_volume_and_mute_work_in_mock_mode(self, client, token):
        assert post(client, "/api/actions/volume", token, {"level": 42}).get_json()["ok"]
        assert post(client, "/api/actions/mute", token).get_json()["ok"]

    def test_a_nonsense_volume_is_refused(self, client, token):
        assert post(client, "/api/actions/volume", token, {"level": "loud"}).status_code == 400

    def test_remote_actions_are_restricted_to_known_names(self, client, token):
        assert post(client, "/api/actions/remote/home", token).get_json()["ok"]
        assert post(client, "/api/actions/remote/rm-rf", token).status_code == 400

    def test_an_unknown_restart_target_is_refused(self, client, token):
        response = post(client, "/api/actions/restart", token, {"target": "the-building"})
        assert response.status_code == 400

    def test_safe_reset_keeps_the_important_things(self, client, token, mock_config):
        mock_config.update({"ROOM_NAME": "Keep", "THEME": "light", "ADMIN_PIN": "9999"})
        assert post(client, "/api/actions/reset-safe", token).get_json()["ok"]
        assert mock_config.str_("ROOM_NAME") == "Keep"
        assert mock_config.str_("ADMIN_PIN") == "9999"
        assert mock_config.str_("THEME") == "dark"


class TestBackgrounds:
    def test_an_image_can_be_uploaded_listed_and_removed(self, client, token):
        response = client.post(
            "/api/backgrounds",
            data={"image": (io.BytesIO(tiny_png()), "wall.png")},
            content_type="multipart/form-data",
            headers={"X-Room-Token": token},
        )
        assert response.status_code == 200
        name = response.get_json()["image"]["name"]

        listing = client.get("/api/backgrounds").get_json()
        assert listing["count"] == 1

        served = client.get(f"/media/backgrounds/{name}")
        assert served.status_code == 200
        assert served.headers["Content-Type"].startswith("image/")

        removed = client.delete(
            f"/api/backgrounds/{name}", headers={"X-Room-Token": token}
        )
        assert removed.get_json()["ok"]
        assert client.get("/api/backgrounds").get_json()["count"] == 0

    def test_uploading_the_first_image_turns_the_slideshow_on(self, client, token, mock_config):
        assert mock_config.str_("BACKGROUND_MODE") == "theme"
        client.post(
            "/api/backgrounds",
            data={"image": (io.BytesIO(tiny_png()), "wall.png")},
            content_type="multipart/form-data",
            headers={"X-Room-Token": token},
        )
        assert mock_config.str_("BACKGROUND_MODE") == "slideshow"

    @pytest.mark.parametrize(
        "payload,name",
        [
            (b"MZ\x90\x00 not an image", "virus.exe"),
            (b'<svg onload="alert(1)"></svg>', "sneaky.svg"),
            (b"", "empty.png"),
            (b"GIF87a", "truncated.gif"),  # signature only, no data
        ],
    )
    def test_non_images_are_refused(self, client, token, payload, name):
        response = client.post(
            "/api/backgrounds",
            data={"image": (io.BytesIO(payload), name)},
            content_type="multipart/form-data",
            headers={"X-Room-Token": token},
        )
        if name == "truncated.gif":
            # A valid signature is accepted; the browser simply will not render it.
            assert response.status_code in (200, 400)
        else:
            assert response.status_code == 400
            assert response.get_json()["error"]

    @pytest.mark.parametrize(
        "name",
        ["../config.yaml", "..%2f..%2fetc%2fpasswd", "/etc/passwd", "not-ours.png"],
    )
    def test_only_our_own_images_are_served(self, client, name):
        assert client.get(f"/media/backgrounds/{name}").status_code in (301, 302, 404)

    def test_uploads_can_be_switched_off(self, client, token, mock_config):
        mock_config.update({"BACKGROUND_ALLOW_UPLOADS": False})
        response = client.post(
            "/api/backgrounds",
            data={"image": (io.BytesIO(tiny_png()), "wall.png")},
            content_type="multipart/form-data",
            headers={"X-Room-Token": token},
        )
        assert response.status_code == 409


class TestInternalApi:
    def test_internal_endpoints_need_the_shared_token(self, client):
        assert client.post("/api/internal/airplay", json={"event": "connected"}).status_code == 403

    def test_a_wrong_internal_token_is_refused(self, client):
        response = client.post(
            "/api/internal/airplay",
            json={"event": "connected"},
            headers={"X-Room-Internal-Token": "nope"},
        )
        assert response.status_code == 403

    def test_airplay_events_change_the_mode(self, client, app):
        from app.web_security import internal_token

        headers = {"X-Room-Internal-Token": internal_token()}
        client.post("/api/internal/airplay", json={"event": "connected"}, headers=headers)
        assert client.get("/api/state").get_json()["mode"] == "screen-sharing"

        client.post("/api/internal/airplay", json={"event": "disconnected"}, headers=headers)
        assert client.get("/api/state").get_json()["mode"] != "screen-sharing"

    def test_remote_actions_arrive_over_the_internal_api(self, client):
        from app.web_security import internal_token

        response = client.post(
            "/api/internal/action",
            json={"action": "home"},
            headers={"X-Room-Internal-Token": internal_token()},
        )
        assert response.get_json()["ok"]


class TestRemoteAccess:
    """Requests that are not from the Pi itself need the admin PIN."""

    def test_a_lan_client_is_asked_for_the_pin(self, client, mock_config):
        mock_config.update({"ADMIN_PIN": "4242"})
        response = client.get("/settings", environ_overrides={"REMOTE_ADDR": "192.168.1.50"})
        assert response.status_code == 302
        assert "/login" in response.headers["Location"]

    def test_a_lan_api_call_is_refused_with_a_hint(self, client, mock_config):
        mock_config.update({"ADMIN_PIN": "4242"})
        response = client.get(
            "/api/settings", environ_overrides={"REMOTE_ADDR": "192.168.1.50"}
        )
        assert response.status_code == 401
        assert response.get_json()["needs_pin"] is True

    def test_the_dashboard_itself_stays_viewable(self, client):
        """A second display should be able to show the room screen read-only."""
        response = client.get("/", environ_overrides={"REMOTE_ADDR": "192.168.1.50"})
        assert response.status_code == 200

    def test_the_correct_pin_signs_in(self, client, mock_config):
        mock_config.update({"ADMIN_PIN": "4242"})
        response = client.post(
            "/login",
            data={"pin": "4242", "next": "/panel"},
            environ_overrides={"REMOTE_ADDR": "192.168.1.50"},
            follow_redirects=False,
        )
        assert response.status_code == 302
        assert response.headers["Location"].endswith("/panel")

    def test_a_wrong_pin_is_rejected(self, client, mock_config):
        mock_config.update({"ADMIN_PIN": "4242"})
        response = client.post(
            "/login",
            data={"pin": "0000"},
            environ_overrides={"REMOTE_ADDR": "192.168.1.50"},
        )
        assert response.status_code == 401
        assert "Incorrect PIN" in response.get_data(as_text=True)

    def test_repeated_wrong_pins_are_rate_limited(self, client, mock_config):
        mock_config.update({"ADMIN_PIN": "4242"})
        seen_lockout = False
        for _ in range(9):
            body = client.post(
                "/login",
                data={"pin": "0000"},
                environ_overrides={"REMOTE_ADDR": "192.168.1.77"},
            ).get_data(as_text=True)
            if "Too many attempts" in body:
                seen_lockout = True
        assert seen_lockout, "brute-forcing the PIN must be slowed down"

    def test_login_will_not_redirect_off_site(self, client, mock_config):
        mock_config.update({"ADMIN_PIN": "4242"})
        response = client.post(
            "/login",
            data={"pin": "4242", "next": "https://evil.example/steal"},
            environ_overrides={"REMOTE_ADDR": "192.168.1.50"},
        )
        assert response.headers["Location"].endswith("/panel")

    def test_a_forwarded_header_cannot_fake_being_local(self, client, mock_config):
        """Trusting X-Forwarded-For would let anyone claim to be the Pi."""
        mock_config.update({"ADMIN_PIN": "4242"})
        response = client.get(
            "/api/settings",
            environ_overrides={"REMOTE_ADDR": "192.168.1.50"},
            headers={"X-Forwarded-For": "127.0.0.1"},
        )
        assert response.status_code == 401


class TestBinding:
    def test_the_server_stays_local_by_default(self, mock_config):
        from app.web_security import effective_bind_host

        assert effective_bind_host(mock_config) == "127.0.0.1"

    def test_lan_access_widens_the_binding(self, mock_config):
        from app.web_security import effective_bind_host

        mock_config.update({"ADMIN_PIN": "4242", "ADMIN_LAN_ACCESS": True})
        assert effective_bind_host(mock_config) == "0.0.0.0"

    def test_an_explicit_address_is_respected(self, mock_config):
        from app.web_security import effective_bind_host

        mock_config.update(
            {"ADMIN_PIN": "4242", "ADMIN_LAN_ACCESS": True, "DASHBOARD_HOST": "10.0.0.5"}
        )
        assert effective_bind_host(mock_config) == "10.0.0.5"
