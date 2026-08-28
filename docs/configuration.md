# Configuration reference

<!-- GENERATED FILE — run scripts/gen-config-docs.py to refresh. -->

Every option can be changed from **Settings** in a browser, from
`./scripts/roomctl set KEY VALUE`, or by editing `config/config.yaml` and
restarting (`./scripts/roomctl restart backend`).

An environment variable of the same name — in `.env` or the real
environment — overrides both, and the Settings page shows such an option as
read-only so the two can never disagree.

There are 87 options. All have working defaults; a fresh install
needs only a calendar link.

## Room

| Option | Default | What it does |
| --- | --- | --- |
| `ROOM_NAME` | `Meeting Room` | Room name. Shown in large type at the top of the dashboard. |
| `ROOM_SUBTITLE` | empty | Room subtitle. Optional second line, e.g. a floor or capacity: “Level 3 · 8 seats”. |
| `TIMEZONE` | empty | Time zone. IANA time zone name. Leave empty to use the Raspberry Pi's own time zone. |
| `TIME_FORMAT_24H` | `false` | 24-hour clock. Off shows 10:42 AM, on shows 10:42. |

## Calendar

| Option | Default | What it does |
| --- | --- | --- |
| `CALENDAR_SOURCE` | `ics` | Calendar source. “ics” uses the ICS/iCal URL below. “mock” invents meetings so you can test the screen without a real calendar. “none” disables the calendar entirely. One of: ics, mock, none. |
| `CALENDAR_ICS_URL` | empty | Calendar ICS/iCal URL. The room calendar's secret iCal address. Outlook: Calendar → Share → Publish a calendar → ICS link. Google: Calendar settings → Secret address in iCal format. A local file path also works. **Secret — never logged.** |
| `CALENDAR_REFRESH_SECONDS` | `30` | Refresh interval (seconds). How often the calendar is re-fetched. 30 seconds is a good default. (min 10, max 3600) |
| `CALENDAR_LOOKAHEAD_HOURS` | `24` | Look-ahead window (hours). How far into the future meetings are loaded. (min 1, max 168) _Advanced._ |
| `CALENDAR_UPCOMING_COUNT` | `4` | Upcoming meetings to show. How many meetings are listed under “Upcoming” (3–5 fits a TV nicely). (min 1, max 8) |
| `CALENDAR_TIMEOUT_SECONDS` | `20` | Fetch timeout (seconds). (min 3, max 120) _Advanced._ |
| `CALENDAR_SHOW_TITLES` | `true` | Show meeting titles. Turn off for privacy: the dashboard then shows “Busy” instead of the subject. |
| `CALENDAR_IGNORE_ALL_DAY` | `true` | Ignore all-day events. All-day entries (out-of-office, holidays) do not mark the room busy. _Advanced._ |
| `CALENDAR_IGNORE_DECLINED` | `true` | Ignore cancelled events. Skip events the organiser has cancelled. _Advanced._ |

## Meeting joining

| Option | Default | What it does |
| --- | --- | --- |
| `AUTO_OPEN_MEETING` | `true` | Open meetings automatically. Navigate the TV to the meeting shortly before it starts. |
| `AUTO_OPEN_MINUTES` | `1.0` | Open this many minutes early. 1 means the meeting page opens about one minute before the start time. (min 0, max 30) |
| `AUTO_CLICK_JOIN` | `true` | Try to press Join automatically. Best effort. Teams, Meet and Zoom change their web pages without notice, so if this fails the big JOIN button on the dashboard always works. |
| `JOIN_SETTLE_SECONDS` | `0.0` | Wait before pressing anything (seconds). 0 uses a sensible per-provider default (6–8 seconds). Raise it on slower hardware: pressing buttons on a page that has not finished drawing wastes the whole join attempt. A Raspberry Pi 3 may need 25–40. (min 0, max 120) _Advanced._ |
| `AUTO_JOIN_TIMEOUT_SECONDS` | `90` | Give up on auto-join after (seconds). (min 10, max 600) _Advanced._ |
| `JOIN_REPEAT_GUARD_SECONDS` | `25.0` | Do not press the same button twice within (seconds). A slow meeting page can look unchanged for several seconds after Join is pressed. Without this guard the room presses it again, and again, which looks like the meeting being opened several times over. (min 0, max 300) _Advanced._ |
| `RETURN_HOME_MINUTES` | `2.0` | Return to dashboard after (minutes). Grace period after a meeting's scheduled end before the TV goes back to the room screen. (min 0, max 120) |
| `MAX_MEETING_MINUTES` | `240` | Hard limit on a meeting screen (minutes). Safety net: the appliance never stays on a meeting page longer than this, even if the calendar goes strange. (min 10, max 1440) _Advanced._ |
| `JOIN_BUTTON_TEXTS` | `Continue on this browser, Use the web app instead, Continue in this browser … (+7)` | Buttons the auto-join may press. One per line, matched case-insensitively against on-screen button text. Update this list if a provider renames a button — no code change needed. _Advanced._ |
| `JOIN_DISPLAY_NAME` | empty | Guest display name. Name typed into “your name” boxes when the room joins as a guest. Leave empty to use the room name. _Advanced._ |
| `JOIN_MUTE_ON_ENTRY` | `false` | Join muted. Ask the meeting page to mute the room's microphone on entry. _Advanced._ |

## AirPlay screen sharing

| Option | Default | What it does |
| --- | --- | --- |
| `AIRPLAY_ENABLED` | `true` | Enable AirPlay receiver. Restarts: room-airplay. |
| `AIRPLAY_NAME` | empty | AirPlay name. What appears in Screen Mirroring on a Mac or iPhone. Leave empty to use the room name. Restarts: room-airplay. |
| `AIRPLAY_PIN` | empty | AirPlay PIN. Optional numeric PIN a guest must type to share their screen. Empty means anyone on the network can share. **Secret — never logged.** Restarts: room-airplay. |
| `AIRPLAY_EXTRA_ARGS` | empty | Extra UxPlay arguments. Appended to the uxplay command line for unusual setups. _Advanced._ Restarts: room-airplay. |
| `AIRPLAY_INTERRUPTS_MEETING` | `true` | Allow screen sharing during a meeting. On: mirroring covers the meeting on the TV (the call keeps running). Off: mirroring is refused during a meeting. Either way, sharing through Teams or Meet is better, because remote participants can see it too. _Advanced._ |

## Poly conference bar

| Option | Default | What it does |
| --- | --- | --- |
| `POLY_ENABLED` | `true` | Manage the conference bar. Detect the USB bar and make it the default camera, microphone and speaker. |
| `MICROPHONE_DEVICE` | `auto` | Microphone. “auto” picks the Poly bar automatically. Otherwise a PipeWire/PulseAudio source name — the Diagnostics page lists them. |
| `SPEAKER_DEVICE` | `auto` | Speaker. “auto” picks the Poly bar. Set to “hdmi” to play sound through the TV, or paste an exact sink name. |
| `CAMERA_DEVICE` | `auto` | Camera. “auto” picks the Poly bar, or give a device path such as /dev/video0. |
| `POLY_USB_MATCH` | `poly, plantronics, polycom … (+2)` | USB name matches. Case-insensitive words used to recognise the bar in lsusb / PipeWire output. One per line. Add your model's name here if detection fails. _Advanced._ |
| `POLY_STARTUP_VOLUME` | `65` | Startup volume (%). Applied to the conference bar when the appliance starts. 0 disables. (min 0, max 100) |
| `POLY_CHECK_SECONDS` | `20` | Device check interval (seconds). (min 5, max 600) _Advanced._ |

## Poly remote / controller

| Option | Default | What it does |
| --- | --- | --- |
| `POLY_REMOTE_ENABLED` | `false` | Enable remote / controller. Turn on if you have a Poly remote or controller. Use Diagnostics → Discover remote buttons to find the key names. Restarts: room-remote. |
| `POLY_REMOTE_DEVICE` | `auto` | Input device. “auto” watches every matching input device. Otherwise an /dev/input/eventN path. Restarts: room-remote. |
| `POLY_ANSWER_KEY` | `KEY_ENTER` | Answer / green button. Joins the current or next meeting. Restarts: room-remote. |
| `POLY_HANGUP_KEY` | `KEY_ESC` | Hang-up / red button. Leaves the meeting and returns to the dashboard. Restarts: room-remote. |
| `POLY_MUTE_KEY` | `KEY_MUTE` | Mute button. Toggles the microphone. Restarts: room-remote. |
| `POLY_VOLUME_UP_KEY` | `KEY_VOLUMEUP` | Volume up button. Restarts: room-remote. |
| `POLY_VOLUME_DOWN_KEY` | `KEY_VOLUMEDOWN` | Volume down button. Restarts: room-remote. |
| `POLY_CAMERA_KEY` | empty | Camera button. Optional. Toggles the camera in the meeting where supported. Restarts: room-remote. |
| `POLY_HOME_KEY` | `KEY_HOME` | Home button. Forces the TV back to the room dashboard. Restarts: room-remote. |
| `POLY_VOLUME_STEP` | `5` | Volume step (%). (min 1, max 25) _Advanced._ Restarts: room-remote. |

## Room controller (phone)

| Option | Default | What it does |
| --- | --- | --- |
| `CONTROLLER_ENABLED` | `true` | Phone controller. A big-button page for whoever is in the room: join, leave, mute, camera and volume. It is opened by scanning the code on the TV — no app to install and no PIN to remember. |
| `CONTROLLER_QR_ON_TV` | `true` | Show the QR code on the TV. A small code in the bottom-right corner of the room screen. Point a phone camera at it to open the controller for this room. |
| `CONTROLLER_LAN_ACCESS` | `false` | Let phones on the room network open the controller. Needed for the QR code to work when “Allow settings from other computers on the network” is off. It exposes the controller and the read-only dashboard only: settings, restarts and logs still need the admin PIN. Restarts: room-dashboard. |
| `CONTROLLER_REQUIRE_PIN` | `false` | Ask for the admin PIN on the controller too. Turn this on for a room in a public space, where being able to see the TV should not be enough to control it. _Advanced._ |

## Display & browser

| Option | Default | What it does |
| --- | --- | --- |
| `KIOSK_ENABLED` | `true` | Run the Chromium kiosk. Turn off only when developing on a normal computer. Restarts: room-kiosk. |
| `CHROMIUM_BINARY` | `auto` | Chromium binary. “auto” searches for chromium-browser, chromium then google-chrome. _Advanced._ Restarts: room-kiosk. |
| `CHROMIUM_DEBUG_PORT` | `9222` | Chromium debug port (localhost only). Used by the appliance to drive the browser. Never exposed off the Pi. (min 1024, max 65535) _Advanced._ Restarts: room-kiosk. |
| `CHROMIUM_RENDER_MODE` | `auto` | How Chromium draws to the TV. Fix for a blank or white screen. “auto” detects Wayland (the default on Raspberry Pi OS Bookworm). Try “wayland”, then “x11”, then “software” — software always renders but without GPU acceleration. One of: auto, wayland, x11, software. Restarts: room-kiosk. |
| `CHROMIUM_EXTRA_ARGS` | empty | Extra Chromium arguments. _Advanced._ Restarts: room-kiosk. |
| `HIDE_CURSOR` | `true` | Hide the mouse pointer. Restarts: room-kiosk. |
| `SCREEN_BLANKING` | `false` | Allow the screen to blank. Off keeps the dashboard visible at all times. Restarts: room-kiosk. |
| `THEME` | `dark` | Dashboard theme. One of: dark, light. |
| `ACCENT_COLOR` | `#3d8bfd` | Accent colour. Hex colour used for the JOIN button and highlights. _Advanced._ |
| `SHOW_SHARING_INSTRUCTIONS` | `true` | Show screen-sharing instructions.  |
| `SHOW_STATUS_INDICATORS` | `true` | Show system status indicators. The small camera / microphone / network dots along the bottom. |

## Background & slideshow

| Option | Default | What it does |
| --- | --- | --- |
| `BACKGROUND_MODE` | `theme` | Background. “theme” is the built-in gradient. “slideshow” rotates through the images you upload in the control panel. “solid” is a single colour. One of: theme, slideshow, solid. |
| `BACKGROUND_SLIDESHOW_SECONDS` | `45` | Seconds per image. How long each slideshow image stays on screen before it fades to the next. Videos ignore this and play to the end. (min 5, max 3600) |
| `BACKGROUND_VIDEO_SOUND` | `false` | Play sound with background videos. Off by default, and it should usually stay off: the wallpaper talking over a meeting is worse than a silent clip. |
| `BACKGROUND_SHUFFLE` | `true` | Shuffle images. Off plays them in filename order. |
| `BACKGROUND_DIM_PERCENT` | `55` | Darken images by (%). Keeps the time and meeting text readable over a bright photo. Raise it if the text is hard to read from across the room. (min 0, max 95) |
| `BACKGROUND_BLUR_PIXELS` | `0` | Blur images by (pixels). A little blur makes busy photos easier to read text over. (min 0, max 40) |
| `BACKGROUND_SOLID_COLOR` | `#0b1220` | Solid colour. Used when Background is set to “solid”. _Advanced._ |
| `BACKGROUND_ALLOW_UPLOADS` | `true` | Allow image uploads from the control panel. Turn off to freeze the slideshow contents. _Advanced._ |

## Reliability & recovery

| Option | Default | What it does |
| --- | --- | --- |
| `HEALTH_CHECK_SECONDS` | `15` | Health check interval (seconds). (min 5, max 300) _Advanced._ |
| `NETWORK_CHECK_HOSTS` | `1.1.1.1, 8.8.8.8` | Hosts used to test internet access. One per line. _Advanced._ |
| `AUTO_RECOVER_BROWSER` | `true` | Recover a stuck browser. If Chromium stops responding or drifts to an unexpected page, put it back on the dashboard. |
| `WATCHDOG_ENABLED` | `true` | External watchdog. A tiny timer outside the app checks the appliance every minute and restarts anything that has stopped answering. Restarts: room-watchdog. |
| `WATCHDOG_REBOOT_ENABLED` | `true` | Reboot as a last resort. If the appliance cannot be revived by restarting services, reboot the Pi. Rate-limited to once an hour. |
| `WATCHDOG_REBOOT_AFTER_FAILURES` | `10` | Failed checks before rebooting. With a 1-minute watchdog, 10 means roughly ten minutes of being completely unresponsive. (min 3, max 120) _Advanced._ |
| `AUTO_UPDATE_ON_BOOT` | `true` | Update the room software when it boots. Pulls the latest version from the repository this room was installed from, before the dashboard starts. A room that cannot reach the repository, or whose files have been edited on the Pi, simply keeps the version it has — updating never stops the room starting. |
| `AUTO_UPDATE_BRANCH` | empty | Branch to update from. Empty follows whichever branch the Pi is on. Only fast-forward updates are taken, so a branch that has diverged is left alone. _Advanced._ |
| `DAILY_RESTART_TIME` | empty | Daily quiet-hours restart. Optional HH:MM (24-hour) at which the room software restarts itself while nobody is around, e.g. 04:30. Empty disables it. |

## System & access

| Option | Default | What it does |
| --- | --- | --- |
| `DASHBOARD_PORT` | `8080` | Dashboard port. Change this if another program already uses the port. The TV and the control-panel address both follow it. (min 1, max 65535) Restarts: room-dashboard, room-kiosk. |
| `DASHBOARD_HOST` | `127.0.0.1` | Listen address. 127.0.0.1 keeps the dashboard on the Pi itself. Use the LAN admin switch below rather than editing this by hand. _Advanced._ Restarts: room-dashboard. |
| `ADMIN_LAN_ACCESS` | `false` | Allow settings from other computers on the network. Lets you open the Settings page from a laptop instead of plugging a keyboard into the Pi. Requires an admin PIN. Restarts: room-dashboard. |
| `ADMIN_PIN` | empty | Admin PIN. Digits only. Required before network access to the Settings page can be switched on. **Secret — never logged.** |
| `PANEL_ENABLED` | `true` | Enable the phone control panel. The simplified touch page at /panel used to join meetings, change the background and restart things from a phone. |
| `PANEL_SHOW_URL_ON_TV` | `true` | Show the control-panel address on the TV. Displays the http://…:8080/panel address in the corner of the dashboard so anyone can find it. Turn off once the room is set up. |
| `LOG_LEVEL` | `INFO` | Log level. DEBUG is noisy but useful while setting the room up. One of: DEBUG, INFO, WARNING, ERROR. |
| `LOG_FORMAT` | `text` | Log format. “text” is easy to read in journalctl; “json” suits log collectors. One of: text, json. _Advanced._ |
| `DEV_MODE` | `false` | Development mode. Runs with mock hardware so the dashboard can be built on a laptop. Never leave this on in a real room. _Advanced._ |

## Where the values come from

Lowest priority first — a later layer wins:

1. **Built-in defaults** (`app/config_schema.py`) — a complete, working set.
2. **`config/config.yaml`** — written by the installer and the Settings page.
3. **`.env` / environment variables** — for secrets and development.

A value that fails validation is replaced by its default and reported as a
warning on the dashboard, rather than stopping the appliance from starting.
If `config.yaml` itself is unreadable, the previous version
(`config.yaml.bak`) is used; if that fails too, the defaults are. The room
always comes up.
