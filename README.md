# Meeting-room appliance for Raspberry Pi

Turns a Raspberry Pi 5, a TV and a Poly USB conference bar into a dedicated
meeting-room system. It boots straight into a room dashboard, shows the room's
calendar, opens Teams / Google Meet / Zoom meetings by itself, accepts AirPlay
screen sharing, uses the Poly bar for camera, microphone and speaker, and
repairs itself when something goes wrong.

Everything is configured from a web page — on your phone, over the room's
network. No keyboard, no YAML, no SSH.

```
┌──────────────────────────────────────────────────────────────┐
│  MEETING ROOM                                 ● Available    │
│  Level 3 · 8 seats                                           │
│                                                              │
│                        10:42 AM                              │
│                    Thursday, 28 August                       │
│                                                              │
│  ┌────────────────────────────────┐ ┌──────────────────────┐ │
│  │ NEXT MEETING                   │ │ UPCOMING             │ │
│  │ Engineering Daily              │ │ 13:00 Supplier Call  │ │
│  │ 11:00 – 11:30                  │ │       Google Meet    │ │
│  │ ▣ Microsoft Teams              │ │ 15:30 Product Review │ │
│  │ Starts in 18 min               │ │       Teams          │ │
│  │ ┌────────────────────────────┐ │ │                      │ │
│  │ │           JOIN             │ │ │                      │ │
│  │ └────────────────────────────┘ │ │                      │ │
│  └────────────────────────────────┘ └──────────────────────┘ │
│                                                              │
│  ⧉ Screen Mirroring → Meeting Room     ● Network ● Calendar   │
│                                        ● Camera  ● Mic       │
└──────────────────────────────────────────────────────────────┘
```

---

## Contents

- [Install it](#install-it)
- [Set the room up from your phone](#set-the-room-up-from-your-phone)
- [First-time account sign-in](#first-time-account-sign-in)
- [Using the room](#using-the-room)
- [The control panel](#the-control-panel)
- [The room controller (scan the code)](#the-room-controller-scan-the-code)
- [Background slideshow](#background-slideshow)
- [Meeting minutes (experimental)](#meeting-minutes-experimental)
- [Keeping the software up to date](#keeping-the-software-up-to-date)
- [Poly conference bar](#poly-conference-bar)
- [Poly remote / controller](#poly-remote--controller)
- [AirPlay screen sharing](#airplay-screen-sharing)
- [Screen sharing from Windows](#screen-sharing-from-windows)
- [Troubleshooting](#troubleshooting)
- [What is deliberately best-effort](#what-is-deliberately-best-effort)
- [Architecture](#architecture)
- [Configuration](#configuration)
- [Security](#security)
- [Developing without a Raspberry Pi](#developing-without-a-raspberry-pi)
- [Command reference](#command-reference)

---

## Install it

**What you need**

| | |
| --- | --- |
| Raspberry Pi 5 | 4 GB or 8 GB. Read the note below — the choice matters more than it looks. |
| Raspberry Pi OS (64-bit) | The **Desktop** image, not Lite — a graphical session is required. |
| TV | Connected over HDMI. |
| Poly USB conference bar | Or any USB conference device; nothing is hard-coded to a model. |
| Network | Ethernet preferred, Wi-Fi supported. |
| Keyboard & mouse | For the install only. Unplug them afterwards. |

### Which Pi, honestly

The two halves of this appliance have very different appetites:

| | Dashboard, calendar, AirPlay, one-touch join | Being the video endpoint in a call |
| --- | --- | --- |
| **Mini-PC / NUC (x86, 4+ cores, 8 GB)** | Trivial | Comfortable |
| **Pi 5 (4/8 GB)** | Comfortable | Works |
| **Pi 4 (4/8 GB)** | Comfortable | Usable, runs warm |
| **Pi 3 (1 GB)** | Fine | **Not realistically** |

**It is not only for a Pi, and it tunes itself.** At startup the room measures
the machine — cores, memory, whether it is a Raspberry Pi at all — and picks a
profile. On a mini-PC or a NUC that means **high**: Chromium is told to use the
GPU properly (rasterisation, hardware video decode, no background throttling of
a window that is in a call), and the join automation stops padding its timings
for hardware that does not need the padding.

| Profile | Picked for | What changes |
| --- | --- | --- |
| `high` | Not a Pi, 4+ cores, 8 GB+ | GPU rasterisation, zero-copy, hardware video decode, no renderer backgrounding; join settles in ~3 s instead of 8, gives up after 60 s; dashboard polls every 3.5 s |
| `balanced` | Pi 4, Pi 5, a modest PC | The shipped defaults, unchanged |
| `low` | Pi 3, or under 2 GB | Fewer renderer processes, no smooth scrolling; join waits four times as long and keeps trying for five minutes |

```bash
./scripts/roomctl performance              # what it decided, and why
./scripts/roomctl performance high         # override the guess
./scripts/roomctl performance auto         # back to measuring
```

A profile only ever supplies **defaults**. Anything you have set yourself —
`JOIN_SETTLE_SECONDS`, `AUTO_JOIN_TIMEOUT_SECONDS` — still wins, so the room
never quietly argues with a value you typed. `roomctl status` and
`GET /api/health` both report the machine it found and the profile it is
running.

A Pi 3 has 1 GB of RAM and no hardware video encode. Chromium plus a live
Google Meet or Teams call needs more memory than that, and the outgoing camera
stream has to be encoded in software on a 1.2 GHz A53. It will swap, and the
call will be poor no matter how the software is tuned.

If a Pi 3 is what you have, it still makes a good **room dashboard and
launcher** — calendar on the TV, AirPlay sharing, and a JOIN button people
press before taking audio on their own laptop. Retune it with:

```bash
./scripts/roomctl slow-device on
```

That gives the meeting page far longer to load before the automation touches
it, widens the join window to five minutes, and stops the dashboard doing
avoidable work. It improves *joining*. It cannot make the call itself smooth.

**Install**

```bash
git clone -b main https://github.com/CharlieCarpenter15/polyCam.git room-appliance
cd room-appliance
./install.sh
```

Run it as the normal desktop user (usually `pi`) — **not** with `sudo`. It asks
for sudo only where it genuinely needs it, and tells you when.

It takes 5–15 minutes. It will:

1. install the system packages it needs
2. create a Python virtual environment in `.venv`
3. install UxPlay for AirPlay (from apt if available, otherwise it offers to build it)
4. write `config/config.yaml` with a generated admin PIN
5. install five systemd **user** services and enable them at boot
6. add exactly one sudo rule — permission to reboot, as a last-resort repair
7. start everything and print the address to open on your phone

When it finishes you will see something like:

```
  Open this on your phone:
     http://192.168.1.50:8080/panel
     PIN: 481920
```

The TV shows the same address and PIN on its "Finish setting up this room"
screen until a calendar is configured, so you do not have to write it down —
and it disappears from the screen as soon as setup is done.

Then finish setup from your phone, and reboot:

```bash
sudo reboot
```

After the reboot the TV shows the dashboard on its own. Nothing needs to be
launched by hand, ever again.

<details>
<summary>Installer options</summary>

```bash
./install.sh --room "Boardroom"              # set the room name
./install.sh --calendar "https://…/room.ics" # set the calendar straight away
./install.sh --pin 123456                    # choose the PIN yourself
./install.sh --no-lan-admin                  # keep settings on the Pi only
./install.sh --no-uxplay                     # skip AirPlay
./install.sh --no-apt                        # skip apt (re-running an install)
./install.sh --unattended                    # never prompt
```

Re-running `./install.sh` is safe. It upgrades in place and never overwrites an
existing `config/config.yaml`.
</details>

---

## Set the room up from your phone

Open the address the installer printed and enter the PIN. You get a touch
control panel:

| Section | What it does |
| --- | --- |
| **Status** | What the room is doing, and whether anything needs attention |
| **Join next meeting** | One tap, from anywhere in the room |
| **Today's meetings** | Every meeting with its own Join button |
| **Sound** | Speaker volume, microphone mute, camera toggle |
| **Background** | Upload photos, set the slideshow speed and dimming |
| **If something looks wrong** | Four numbered repair buttons, then reboot |

There is exactly one thing you must set: the **room calendar**.

**Settings → Calendar → Calendar ICS/iCal URL**

<details>
<summary>Where to find the ICS link — Microsoft 365 / Outlook</summary>

1. Sign in as the room account (or as an admin with access to the room mailbox).
2. Outlook on the web → **Settings** → **Calendar** → **Shared calendars**.
3. Under **Publish a calendar**, pick the room calendar.
4. Choose **Can view all details** and select **Publish**.
5. Copy the **ICS** link (not the HTML one).

The link is a secret — anyone with it can read the calendar. The appliance
stores it with `0600` permissions and never writes it to a log.
</details>

<details>
<summary>Where to find the ICS link — Google Calendar / Google Workspace</summary>

1. Google Calendar → hover the room calendar → **⋮** → **Settings and sharing**.
2. Scroll to **Integrate calendar**.
3. Copy **Secret address in iCal format**.

If you use a Google Workspace resource calendar, an administrator may need to
allow external sharing before the secret address works.
</details>

<details>
<summary>Trying it out before you have a calendar link</summary>

Set **Settings → Calendar → Calendar source** to `mock`. The dashboard fills
with invented meetings so you can see how everything looks and test the Join
button. Switch it back to `ics` when you have the real link.
</details>

Everything else already has a sensible default. Change the room name, the theme
and the AirPlay name if you like, and you are done.

---

## First-time account sign-in

The room joins meetings as a **room account**, not as an employee. This is a
one-time job that needs a keyboard and mouse plugged into the Pi.

Chromium's profile lives in `var/chromium-profile`, so these sign-ins survive
reboots and Chromium restarts. You will not have to do it again.

**1. Get to a normal browser window**

The kiosk is fullscreen with no address bar, so stop it first:

```bash
systemctl --user stop room-kiosk
```

**2. Open Chromium using the room's own profile**

```bash
chromium-browser --user-data-dir="$HOME/room-appliance/var/chromium-profile" \
                 --password-store=basic &
```

(Adjust the path if you cloned somewhere else. Use `chromium` instead of
`chromium-browser` on releases where that is the name.)

`--password-store=basic` matters: without it Chromium tries to use the desktop
keyring and pops an *"Enter password to unlock your login keyring"* dialog,
which is confusing and has nothing to do with the room accounts. The kiosk
already passes this flag; the manual launch needs it too.

**3. Sign in to what the room needs**

- **Microsoft Teams** — <https://teams.microsoft.com>, sign in as the room
  account, and tick *Keep me signed in*. Dismiss the "get the app" prompts;
  always choose **Use the web app**.
- **Google Meet** — <https://meet.google.com>, sign in as the room's Google
  account, and allow it to stay signed in.
- **Zoom** — <https://zoom.us/signin> only if your rooms have a Zoom account.
  Zoom in a browser is the least reliable of the three (see
  [what is best-effort](#what-is-deliberately-best-effort)).

**4. Grant camera and microphone once**

Open <https://teams.microsoft.com> (or Meet), start a test meeting and allow
camera and microphone access when asked. Choose **Remember this decision** if
offered. The appliance also pre-grants these permissions over the DevTools
protocol before each meeting, but doing it by hand once is more reliable.

**5. Put the kiosk back**

```bash
systemctl --user start room-kiosk
```

Then unplug the keyboard and mouse.

> **Why a dedicated profile?** The room must never use a person's browser
> profile: their mail, history and saved passwords would be one click away on a
> screen in a shared room. The appliance always uses its own profile and never
> touches yours.

---

## Using the room

**A meeting starts.** About a minute before the scheduled start (configurable)
the TV opens the meeting page and tries to press through the join buttons. If a
person is in the room they see the meeting come up on its own.

**Joining by hand.** The big **JOIN** button on the dashboard always works —
on a touch TV, on the phone control panel, or with the green button on a Poly
remote. Use it whenever automatic joining did not get all the way in.

**A meeting ends.** Two minutes after the scheduled end time (configurable) the
TV returns to the dashboard by itself. There is also a hard limit — by default
four hours — so the room can never be stuck on a stale meeting screen even if
the calendar says something strange.

**From a phone.** Scan the small code in the bottom-right corner of the TV and
the room's buttons open on the phone — join, leave, mute, camera, volume. See
[The room controller](#the-room-controller-scan-the-code).

**Sharing a screen.** Mac or iPhone → Control Centre → Screen Mirroring →
*Meeting Room*. The dashboard steps aside; when mirroring stops it comes back.

**Nothing scheduled.** The dashboard shows the time, the date, *Available*, and
how to share a screen.

---

## The control panel

`http://<pi-address>:8080/panel`

Designed for a phone held in one hand. Three pages:

| Page | For |
| --- | --- |
| **Control** (`/panel`) | Day-to-day: join, volume, background, repairs |
| **Settings** (`/settings`) | Every option, grouped, with help text |
| **Checks** (`/diagnostics`) | What the Pi can see, plus live logs |

**Turning network access on or off**

Network access requires an admin PIN — the appliance refuses to open the
Settings page to the network without one.

```bash
./scripts/roomctl lan-admin on 481920   # allow, with this PIN
./scripts/roomctl lan-admin off         # this Pi only
./scripts/roomctl panel                 # print the address and PIN status
```

The dashboard also shows the control-panel address in its bottom corner. Turn
that off once the room is set up: **Settings → System & access → Show the
control-panel address on the TV**.

---

## The room controller (scan the code)

The control panel above is for whoever looks after the room. The **controller**
is for whoever is *in* it: a phone-sized page with the room's buttons on it,
opened by pointing a camera at the small QR code in the bottom-right corner of
the TV. No app, no PIN, nothing to remember.

```
                                            ┌──────────┐
                                            │ ▙▚▘▟▘▚▙▘ │   Scan to control
                                            │ ▘▙▚▟▚▘▙▚ │   this room
                                            └──────────┘
```

**What it does**

| | |
| --- | --- |
| Join / Leave | The one big button, which reads the room: join the meeting that is due, or leave the one that is running |
| Microphone | Mute and unmute, showing which it currently is |
| Camera | On and off in the meeting |
| Volume | Up, down, and a slider |
| Show dashboard | Put the room screen back on the TV |
| Today's meetings | Tap any one of them to join that meeting instead |

It shows what the room is doing in plain English — nothing scheduled, starting
in four minutes, in a meeting, someone is sharing their screen, the room is
offline — and it says what to do next in each case. Pressing a button on the
physical Poly remote shows up on the phone too, and on the TV, so two people
are never fighting a room that appears not to respond.

**Turning it on**

The QR code appears once phones on the room's network can actually reach the
Pi. That is one switch:

**Settings → Room controller → Let phones on the room network open the
controller** — or from a terminal:

```bash
./scripts/roomctl set CONTROLLER_LAN_ACCESS true
./scripts/roomctl restart backend
./scripts/roomctl qr                  # the address behind the QR code
```

If **Allow settings from other computers on the network** is already on, the
controller is reachable and the code appears without changing anything.

**What a scanned phone can do**

By default: everything. The appliance is a Raspberry Pi on a wall with no
keyboard, and the point of the code is that a phone is the keyboard — including
for first-run setup, which happens before a PIN exists to ask for. Scanning it
signs that phone in to the room: the room buttons, Settings, the background
slideshow, restarts, updates, diagnostics and logs.

The trade is "whoever can see this screen can run this room", which is close to
what standing in the room already means — and the code is shown on the room's
own screen and served nowhere else, so it cannot be fetched over the network.
Three settings tighten it where that is not the right trade:

| Setting | |
| --- | --- |
| **A scanned phone can do everything** (off) | Back to the room buttons alone — join, leave, mute, camera, volume — with everything else asking for the PIN |
| **Ask for the admin PIN on the controller too** | For a room in a public space: scanning grants nothing on its own |
| **New code** (control panel → Room controller) | Issues a fresh code and signs out every phone that ever scanned the old one |

Turn the whole thing off with **Settings → Room controller → Phone
controller**, or hide just the code with **Show the QR code on the TV**.

---

## Background slideshow

By default the dashboard uses a built-in gradient. To use your own photos and
video:

1. Control panel → **Background**
2. **+ Add images or video** — JPEG, PNG, GIF or WebP up to 12 MB each, MP4,
   WebM or MOV up to 200 MB each, 60 files in total
3. Adjust **Seconds per image**, **Darken for readability** and **Shuffle**

**Videos play in full.** *Seconds per image* is what it says — it applies to
stills. A video is never cut off part way: it plays to its end and only then
does the slideshow move on. Sound is off unless you turn on **Play sound with
background videos**, and a clip the TV cannot decode (an iPhone HEVC recording,
usually) is skipped rather than left as a black screen. For a wall loop, H.264
MP4 is the safe choice on a Raspberry Pi.

Uploading your first image switches the background to slideshow mode
automatically. Images crossfade, and the darkening layer keeps the clock and
meeting text readable over a bright photo — raise it if the text is hard to
read from across the room.

Uploads are checked by their actual file contents, not their filename, so a
renamed file cannot slip something else onto the screen. Images are stored in
`var/backgrounds`.

---

## Meeting minutes (experimental)

The appliance can record each meeting, work out who said what, ask Claude for a
summary and email it to the people who were there.

**It is off by default and every part of it is a separate switch.** With it off,
no thread starts, no microphone or camera is opened and nothing is written to
disk — the room screen, the calendar and the meeting joining behave exactly as
they do without it.

The idea it is built on is that the room microphone and the speaker's own output
are recorded as **two separate tracks**. Which track a voice arrives on decides
whether the speaker was in the room or on the call, and that is a fact rather
than a guess. On top of that: the meeting window's own participant list and
captions name the remote speakers, and enrolled faces and voices name the people
in the room. Where nothing knows, the line stays unattributed rather than being
given to somebody — a confidently wrong name is far worse than a blank one.

Turning it on is a switch in Settings and one command for the parts that need
downloading:

```bash
./scripts/install-minutes.sh          # or ./scripts/install.sh --with-minutes
```

Enrolling colleagues, what each part needs, and an honest account of what works
well and what does not, are all in
**[docs/meeting-minutes.md](docs/meeting-minutes.md)**. Read it first: recording
a room full of people is a legal and social decision as much as a technical one,
and some of the recognition is genuinely experimental.

The short version of the honest part: recording is reliable; naming remote
speakers works well where live captions are on and is patchy otherwise;
recognising faces works close to the camera and fails down a long table;
recognising voices on a far-field microphone is the weakest link and should be
treated as a suggestion you correct rather than an answer you trust.

---

## Keeping the software up to date

The room updates itself when it boots. `room-update.service` runs before the
dashboard and the kiosk start, pulls the branch this Pi was installed from, and
gets out of the way.

It is deliberately timid, because a room that will not start is much worse than
a room running last month's code:

**The remote branch wins.** The checkout is *reset* to it, not merged with it:
anything edited on the Pi is discarded, and a checkout that has drifted is
straightened out rather than skipped. A room should run the code everyone else
can see, not a local variant nobody remembers making. Edit on your laptop, push,
and the room picks it up.

**The room's own state is not code, and is never touched.** Everything git
ignores survives every update:

| Kept | |
| --- | --- |
| `config/config.yaml` | The room's calendar, name, PIN, every setting |
| `.env` | Environment overrides |
| `var/` | Calendar cache, pairing code, background images and videos, and the Chromium profile with its signed-in room accounts |
| `.venv/` | The virtualenv |

| Situation | What happens |
| --- | --- |
| No network yet | Waits up to a minute, retries the fetch three times, then gives up and starts on the current version |
| Files edited on the Pi | Discarded — the count is logged, so `journalctl` can answer "where did my edit go?" |
| The branch has diverged | Reset onto the remote branch |
| `requirements.txt` changed | The virtualenv is updated too |
| A systemd unit changed | Units are reinstalled and reloaded |
| Anything at all fails | Logged, exit 0, the room starts anyway |

**Update now, without rebooting**

```bash
./scripts/roomctl update          # pull, then restart the room
```

or from a phone: control panel → **If something looks wrong** is for repairs;
the update lives with the restarts as **Check for a software update**.

**Watch what it did**

```bash
journalctl --user -u room-update.service -n 50
```

**Turning it off**

**Settings → Reliability & recovery → Update the room software when it boots**,
or:

```bash
./scripts/roomctl set AUTO_UPDATE_ON_BOOT false
```

Set **Branch to update from** (`AUTO_UPDATE_BRANCH`) to pin a room to one
branch; empty means "whichever branch this Pi is on".

Worth being clear about the trade: with this on, whoever can push to that
branch can change what the room runs. That is the point of it, and it is fine
for a repository you control — but it is the reason the setting exists.

---

## Poly conference bar

Plug the bar into a USB port. At startup the appliance finds it and makes it the
system's default camera, microphone and speaker, then keeps checking every 20
seconds so a re-plugged cable recovers on its own.

No model is hard-coded. Detection matches a configurable word list against
`lsusb` and PipeWire device names — by default `poly`, `plantronics`,
`polycom`, `studio`, `hp inc`.

**If the bar is not detected**

```bash
./scripts/detect-poly.sh
```

This prints the USB device, the camera nodes, every microphone and speaker,
which are currently the system defaults, and any missing tools. If your bar
appears in the "all connected USB devices" list but is not matched, add a word
from its name to **Settings → Poly conference bar → USB name matches**.

The same information is on **Checks** in the web UI.

<details>
<summary>Doing it by hand</summary>

```bash
lsusb                              # is it there at all?
v4l2-ctl --list-devices            # camera nodes
v4l2-ctl -d /dev/video0 --list-formats
pactl list short sources           # microphones
pactl list short sinks             # speakers
pactl get-default-source
pactl get-default-sink
wpctl status                       # PipeWire's own view

# Force a choice, then let the appliance keep it:
pactl set-default-sink   alsa_output.usb-Poly_Studio-00.analog-stereo
pactl set-default-source alsa_input.usb-Poly_Studio-00.mono-fallback
```

Over SSH, PipeWire belongs to the desktop session, so prefix commands with
`XDG_RUNTIME_DIR=/run/user/$(id -u)`.
</details>

**Sound through the TV instead of the bar:** set **Settings → Poly conference
bar → Speaker** to `hdmi`.

---

## Poly remote / controller

Optional. A Poly remote appears to Linux as an ordinary HID input device, so its
buttons arrive as standard `KEY_*` codes. Because models differ, every mapping
is configurable and nothing is assumed.

**1. Find out what your remote sends**

Either in the web UI — **Checks → Discover buttons**, then press a button — or
in a terminal:

```bash
./scripts/diagnose-remote.sh          # guided: pick a device, press buttons
./scripts/diagnose-remote.sh --list   # just list input devices
./scripts/diagnose-remote.sh --all    # watch every device at once
```

Look for lines like `EV_KEY  KEY_ENTER  value 1` — `value 1` means pressed.

**2. Map the buttons**

**Settings → Poly remote / controller**, then turn **Enable remote /
controller** on.

| Setting | Default | Does |
| --- | --- | --- |
| Answer / green | `KEY_ENTER` | Join the current or next meeting |
| Hang-up / red | `KEY_ESC` | Leave the meeting, return to the dashboard |
| Mute | `KEY_MUTE` | Toggle the microphone |
| Volume up / down | `KEY_VOLUMEUP` / `KEY_VOLUMEDOWN` | Speaker volume |
| Camera | *(unset)* | Toggle the camera in the meeting |
| Home | `KEY_HOME` | Force the dashboard back on screen |

Reading `/dev/input` requires membership of the `input` group. The installer adds
it; it takes effect after a reboot. If the remote sees nothing, check:

```bash
id -nG | tr ' ' '\n' | grep input
```

---

## AirPlay screen sharing

Works out of the box after installation.

**Mac / iPhone / iPad** → Control Centre → Screen Mirroring → **Meeting Room**

The dashboard steps aside while mirroring and comes back when it stops. UxPlay
runs with low-latency settings (`-vsync no`) because a shared laptop screen
needs to feel responsive more than it needs perfectly paced frames.

| Setting | Effect |
| --- | --- |
| AirPlay name | What appears in Screen Mirroring. Defaults to the room name. |
| AirPlay PIN | Optional numeric code a guest must type before sharing. |
| Allow sharing during a meeting | On by default; the call keeps running underneath. |

**If the room does not appear in Screen Mirroring**

1. The device must be on the same network as the Pi. AirPlay discovery uses
   mDNS, which most guest and "client isolation" Wi-Fi networks block.
2. Check the receiver is running: `./scripts/roomctl status`
3. Check discovery is running: `systemctl is-active avahi-daemon`
4. Restart it: control panel → **2 · Restart AirPlay**

---

## Screen sharing from Windows

**AirPlay only supports Apple devices.** Miracast on Raspberry Pi is
experimental and unreliable, and this appliance deliberately does not attempt
it — a flaky Miracast stack is not worth risking the dashboard, the calendar and
AirPlay for.

Windows users have two good options:

1. **Share through the meeting** — Teams or Meet content sharing. Better anyway:
   remote participants see the content too, which mirroring to the TV never
   achieves.
2. **A dedicated casting dongle** on a second HDMI input, if in-room wireless
   sharing from Windows is a hard requirement.

The architecture leaves room for a Windows method later: screen sharing is a
separate service that reports its state to the backend over a small internal
API, so another receiver can be added the same way UxPlay was, without touching
the dashboard or the calendar.

---

## Troubleshooting

Start here: **control panel → "If something looks wrong"**. Four numbered
buttons, safe to press in order. Each takes a few seconds and the room comes
back on its own.

Or from a terminal:

```bash
./scripts/roomctl status        # what is running, what is broken
./scripts/roomctl doctor        # full hardware check
./scripts/roomctl logs -f       # follow the logs
./scripts/roomctl restart all   # restart everything
```

### The TV is white, blank, or grey

Start here — it tells you which of two very different problems you have:

```bash
./scripts/roomctl screen
```

It reports whether the backend is answering, whether Chromium is running, and
**which page Chromium actually has open**. That last part is the whole answer:

* *"The page never loaded"* — Chromium started before the backend was ready.
  `./scripts/roomctl restart browser`.
* *"The correct page IS loaded"* — the page is fine and Chromium is failing to
  draw it. This is a compositor mismatch, common on a Pi 5 because Raspberry Pi
  OS Bookworm runs labwc (Wayland). Work down these, checking the TV after each:

  ```bash
  ./scripts/roomctl set CHROMIUM_RENDER_MODE wayland  && ./scripts/roomctl restart browser
  ./scripts/roomctl set CHROMIUM_RENDER_MODE x11      && ./scripts/roomctl restart browser
  ./scripts/roomctl set CHROMIUM_RENDER_MODE software && ./scripts/roomctl restart browser
  ```

  `software` has no GPU acceleration but always draws something, so it is the
  one to reach for if you just need the room working today.

### The TV is blank or shows a desktop

```bash
systemctl --user status room-kiosk
journalctl --user -u room-kiosk -n 50
```

The most common cause is that the graphical session was not ready yet — the
kiosk waits up to 90 seconds and retries by itself. Check the Pi is set to boot
to **Desktop with autologin** (`sudo raspi-config` → System Options → Boot /
Auto Login).

### I have lost the admin PIN

While the room is still unconfigured it is on the TV, on the "Finish setting up
this room" screen. Otherwise read it back on the Pi:

```bash
./scripts/roomctl get ADMIN_PIN
```

Note that `./scripts/roomctl config` deliberately masks it as `********`; `get`
returns the real value. To set a new one:

```bash
./scripts/roomctl pin 246813
```

### The TV shows a browser "Sign in" box wanting a username and password

That is Chromium's own HTTP Basic Authentication dialog, showing
`http://127.0.0.1:8080`. **This appliance never asks for a username** — its own
sign-in is a numeric PIN on a page it draws itself. A browser only shows that
dialog when a server answers `401` with a `WWW-Authenticate: Basic` header, so
something *else* is listening on the dashboard port.

```bash
./scripts/roomctl screen
```

It will report **"wrong program on port 8080"** and name the process. Then
either stop that program, or move the room out of its way:

```bash
sudo ss -tlnp | grep :8080     # what owns the port
./scripts/roomctl port auto     # move the room out of its way
```

`port auto` finds a free port, pins it, and restarts the dashboard **and** the
TV display (the kiosk builds its URL from the port, so it has to restart too).
It deliberately picks from a short fixed list rather than a random high port:
the control-panel address is something people bookmark on a phone, so it needs
to stay put between reboots.

### "backend NOT answering on port 8080"

```bash
./scripts/roomctl screen
```

It now tells the two causes apart, because they need opposite fixes:

**"Nothing is listening"** — the room software is not running. Its own log says
why:

```bash
./scripts/roomctl logs backend | tail -40
```

**"Something else is using port 8080"** — a port clash. The room restarts,
fails to bind, and tries again forever. Either stop whatever owns the port or
move the room:

```bash
sudo ss -tlnp | grep :8080     # what owns it
pgrep -af app.main             # is it a second copy of this appliance?
./scripts/roomctl set DASHBOARD_PORT 8090
./scripts/roomctl restart backend
```

A stray `./scripts/dev-run.sh` left running is the usual culprit. The journal
also names this directly now — look for `web.port_already_in_use`.

### The dashboard shows "No calendar connected yet"

The calendar link is missing or wrong. Settings → Calendar. To test the link:

```bash
./scripts/roomctl calendar "https://…/room.ics"
```

That fetches it immediately and tells you how many meetings it found, or exactly
what went wrong.

### "Showing saved meetings"

The calendar could not be reached, so the last meetings that *were* retrieved
are still on screen. This is intended. The appliance keeps retrying with a
backoff and clears the message when the feed returns. Check the ICS link has not
expired — published Outlook links can be reset by an administrator.

### Meetings appear but every JOIN button says "No meeting link"

Check how many of your meetings actually carry a link the room can open:

```bash
./scripts/roomctl calendar "https://…/room.ics"
```

If it reports **0 of N**, the calendar is being read fine but the join links
are not in the feed. On Microsoft 365 the usual cause is that the room mailbox
strips the meeting body — which is exactly where the Teams link lives. Resource
mailboxes do this by default. An Exchange admin can stop it:

```powershell
Set-CalendarProcessing -Identity "boardroom@yourcompany.com" `
  -DeleteComments $false -DeleteSubject $false -AddOrganizerToSubject $false
```

That only affects meetings booked *after* the change, so test with a new
booking rather than an existing one.

### One meeting says "No meeting link"

The event has no Teams / Meet / Zoom link the appliance recognises in its URL,
location or description. **Checks → Meeting join automation** shows what was
found. Organisers sometimes paste a link as an image or an attachment, which
cannot be detected.

### Joining is unreliable and the Pi is a 3 or older

Almost certainly timing. The defaults assume a Pi 5: the automation waits 6–8
seconds for the page, starts pressing buttons, and gives up after 90 seconds.
On a Pi 3 the Meet pre-join screen can take 30–60 seconds to appear, so it
presses at nothing and then runs out of budget.

```bash
./scripts/roomctl slow-device on
```

Then watch a real join happen:

```bash
./scripts/roomctl logs -f
```

`meeting.join_automation_failed` with `clicks=none` means it never found a
button at all — push the wait up further:

```bash
./scripts/roomctl set JOIN_SETTLE_SECONDS 45
```

Also check memory, because on a Pi 3 that is the real ceiling:

```bash
./scripts/roomctl status | grep -i memory
```

Below roughly 150 MB free, Chromium is swapping and no amount of tuning helps.
See [Which Pi, honestly](#which-pi-honestly).

### A meeting opens but the room never gets in

Expected some of the time — see
[what is best-effort](#what-is-deliberately-best-effort). Press **JOIN** on the
dashboard, the phone panel, or the remote's green button.

If it fails every time, look at **Checks → Meeting join automation**. It lists
the buttons the automation pressed and where it stopped. If a provider has
renamed a button, add the new text to **Settings → Meeting joining → Buttons
the auto-join may press** — no code change and no update needed.

If Teams keeps asking to open the desktop app, the room account is probably not
signed in: redo [first-time account sign-in](#first-time-account-sign-in).

### No sound, or the far end cannot hear the room

```bash
./scripts/detect-poly.sh
```

Check the bar is matched as camera, microphone *and* speaker. Then check the
microphone is not muted (control panel → Sound) and the volume is up. If
Chromium grabbed the wrong device before the bar was ready, restart the display:
control panel → **1 · Restart the TV display**.

### The room is on a meeting screen it should have left

It will leave by itself — at the scheduled end plus the grace period, or at the
hard limit at the latest. To force it now: control panel → **Show dashboard**,
or the remote's red button.

### Everything is broken

```bash
./scripts/roomctl restart all
sudo reboot
```

If a bad settings change is the cause, put everything back to defaults while
keeping the calendar link, room name and PIN:

control panel → **Reset settings to safe defaults**, or

```bash
./scripts/roomctl reset
```

The previous configuration is kept as `config/config.yaml.bak`, so a reset can
be undone by hand.

### Reading the logs

Everything goes to the journal as structured, greppable lines:

```bash
journalctl --user -u room-dashboard -f              # the backend
journalctl --user -u room-kiosk -n 100              # Chromium
journalctl --user -u room-airplay -n 100            # AirPlay
journalctl --user -u room-watchdog -n 50            # the watchdog
./scripts/roomctl logs -f                           # all of them together
```

```
INFO    calendar.refreshed events=7 source=ics
INFO    meeting.upcoming_detected provider=teams manual=false starts=2026-08-28T11:00
INFO    meeting.opening_teams provider=teams title="Engineering Daily"
INFO    meeting.join_automation_attempted provider=teams button="join now" pass_number=2
INFO    meeting.join_automation_succeeded provider=teams passes=3 clicks="join now"
INFO    room.returning_to_dashboard reason="meeting ended"
WARNING network.unavailable hosts=1.1.1.1,8.8.8.8
INFO    network.restored
```

Set **Settings → System & access → Log level** to `DEBUG` while investigating.
Meeting URLs and calendar tokens are never logged — only the provider's host
name.

---

## What is deliberately best-effort

**Automatic joining cannot be guaranteed, and this is not a bug to be fixed.**

Teams, Google Meet and Zoom are web applications owned by other companies. They
change their markup, their button labels and their sign-in flows without notice
and without a compatibility promise. Any automation against them will break
eventually.

What this appliance does about that:

| Choice | Why |
| --- | --- |
| Matches on **visible button text**, not CSS selectors | "Join now" outlives `.css-1x2y3z` by years |
| Button texts live in **configuration**, editable from Settings | A rename is fixed in the UI in seconds, with no update |
| Per-provider quirks are in one small file (`app/join_flows.py`) | Adding or fixing a provider is a local, obvious edit |
| Every automated step is **optional** | Failure leaves the room on the provider's own pre-join screen |
| The manual **JOIN** button is always available | On the TV, the phone, and the remote |
| Failures are logged with what was pressed and where it stopped | So the fix is a config change, not an investigation |

**The safe fallback is the pre-join screen**, which is exactly where a person
pressing Join by hand would have arrived. The room is never left somewhere
useless.

Known specifics:

- **Teams** shows an app-or-browser chooser first. Signed in as the room
  account, joining is usually reliable. As a guest it asks for a name, which the
  automation fills in with the room name.
- **Google Meet** needs the room's Google account signed in. Otherwise the room
  can only "Ask to join" and a host must admit it — there is nothing any
  automation can do about that.
- **Zoom** is the least reliable. It repeatedly pushes the desktop client, which
  does not exist for Raspberry Pi OS. The appliance asks for the web client, but
  expect to press Join by hand.

Also best-effort, and for the same reason: the in-meeting mute and camera
buttons on the remote press the *meeting page's* controls. Microphone mute at
the operating-system level always works.

---

## Architecture

Five small services. Each does one job, each is restarted independently, and
none needs another to be healthy in order to keep working.

```
                    ┌───────────────────────────────────────┐
   TV (HDMI) ◄──────┤ room-kiosk        Chromium, fullscreen │
                    └──────────────┬────────────────────────┘
                                   │ HTTP (localhost) ▲ DevTools protocol
                                   ▼                  │
                    ┌───────────────────────────────────────┐
   Phone / laptop ──┤ room-dashboard    Flask + state machine│
   (PIN, LAN)       │                                        │
                    │  calendar_service   refresh + cache     │
                    │  meeting_service    which mode, and why │
                    │  browser_service    drives Chromium     │
                    │  poly_service       camera/mic/speaker  │
                    │  health_service     watch and repair    │
                    └───┬──────────────┬─────────────────┬───┘
                        │              ▲                 │
              PipeWire / │      internal│API       systemctl│--user
                  pactl  ▼              │                 ▼
              ┌──────────────┐  ┌───────────────┐  ┌──────────────┐
              │ Poly USB bar │  │ room-airplay  │  │ room-remote  │
              │ cam/mic/spkr │  │ UxPlay + supv │  │ evdev → HTTP │
              └──────────────┘  └───────────────┘  └──────────────┘

              ┌──────────────────────────────────────────────┐
              │ room-watchdog.timer   every minute, outside   │
              │ everything: is the room actually answering?   │
              └──────────────────────────────────────────────┘
```

**Why the services are split this way**

- **The kiosk is independent of the backend.** If the backend crashes, the TV
  keeps showing the page, which reconnects by itself. If Chromium crashes,
  systemd restarts it without disturbing anything else.
- **AirPlay is independent of both.** People can still share a screen while the
  room software is restarting.
- **The remote is a separate process** because reading `/dev/input` needs the
  `input` group, and the web backend should not have it. A misbehaving HID
  device also cannot take the dashboard down.
- **The watchdog runs outside all of it.** systemd restarts a process that
  *exits*; only an external check catches one that is running but wedged.

**Why systemd *user* services.** Chromium needs the graphical session, PipeWire
is per-user, and UxPlay needs both. As user units all three simply work — and
usefully, restarting them needs no privileges at all. The single sudo rule the
installer adds is permission to reboot, nothing more.

### The four modes

The state machine picks exactly one, in this order of precedence:

| Mode | When | The TV shows |
| --- | --- | --- |
| `screen-sharing` | Someone is mirroring | Their screen |
| `meeting` | A meeting page is open | The meeting |
| `offline` | No network | The dashboard, saying so |
| `home` | Otherwise | The dashboard |

### Recovery behaviour

| Goes wrong | What happens |
| --- | --- |
| Chromium crashes | systemd restarts it; it reopens on the dashboard |
| Chromium hangs | Health check notices after ~4 probes and restarts it |
| Chromium drifts to an unexpected page | Navigated back to the dashboard |
| UxPlay crashes | The supervisor restarts it and reports the restart |
| Backend crashes | systemd restarts it; the TV reconnects by itself |
| Backend wedges | The watchdog restarts it from outside |
| Internet disappears | Dashboard stays up, shows offline, reconnects on its own |
| Calendar fails | Last retrieved meetings stay on screen, marked saved; retried with backoff |
| Calendar unreachable at boot | Meetings restored from the on-disk cache |
| `config.yaml` is corrupt | Falls back to `config.yaml.bak`, then to defaults; the room always starts |
| A setting is out of range | Reset to its default, reported on the dashboard |
| A meeting screen gets stuck | Left at the scheduled end + grace, or at the hard limit |
| Nothing works for ~10 minutes | The watchdog reboots the Pi (at most once an hour) |

Every one of these is covered by a test in `tests/`.

### Project layout

```
room-appliance/
├── install.sh                  wrapper for scripts/install.sh
├── app/
│   ├── main.py                 Flask app, routes, entry point
│   ├── config_schema.py        every option — the single source of truth
│   ├── config.py               layering, validation, atomic save, recovery
│   ├── calendar_service.py     background refresh + last-known-good cache
│   ├── providers/              ics.py · mock.py · null.py  (add graph/google here)
│   ├── meeting_links.py        find the join link, name the provider
│   ├── meeting_service.py      the state machine: which mode, and why
│   ├── browser_service.py      drives Chromium, corrects drift
│   ├── join_flows.py           per-provider join automation (edit this one)
│   ├── cdp.py                  minimal Chrome DevTools Protocol client
│   ├── poly_service.py         detect and select camera / mic / speaker
│   ├── airplay_service.py      mirroring state from the UxPlay supervisor
│   ├── remote_service.py       evdev buttons → room actions
│   ├── remote_runner.py        the separate process for room-remote.service
│   ├── health_service.py       health reporting and self-repair
│   ├── background_service.py   slideshow images (validated uploads)
│   ├── system_service.py       every shell-out, in one place, with timeouts
│   ├── web_security.py         PIN auth, CSRF, internal tokens, binding
│   ├── logging_setup.py        structured logs with secret redaction
│   ├── templates/              index · panel · settings · diagnostics · login
│   └── static/                 styles.css · admin.css · app.js · panel.js · …
├── scripts/
│   ├── install.sh              the installer
│   ├── uninstall.sh            remove services (--purge for data too)
│   ├── roomctl                 do anything from a terminal
│   ├── start-kiosk.sh          launch Chromium properly
│   ├── start-airplay.sh        UxPlay + event supervisor
│   ├── detect-poly.sh          conference-bar diagnostics
│   ├── diagnose-remote.sh      discover remote key codes
│   ├── watchdog.sh             the external health check
│   ├── dev-run.sh              run on a laptop, no hardware
│   └── lib-room.sh             shared shell helpers
├── systemd/                    the five units and the watchdog timer
├── config/
│   ├── config.example.yaml     generated: every option with its default
│   └── config.yaml             yours (created by the installer, 0600)
├── docs/configuration.md       generated: the full options reference
└── tests/                      433 tests, including shell and JS checks
```

### Stack

Python 3 · Flask · icalendar + recurring-ical-events · Chrome DevTools Protocol
over websocket · UxPlay · PipeWire/PulseAudio utilities · evdev · systemd ·
HTML, CSS and vanilla JavaScript.

No React, no Node at runtime, no Docker, no build step. An engineer sitting in
front of the Pi can read every file, change one, and restart one service.

### Adding a calendar back end

The ICS feed is a starting point, not a commitment. To add Microsoft Graph or
the Google Calendar API:

1. Write `app/providers/graph.py` with a class extending `CalendarProvider`.
   It needs one method — `fetch(window_start, window_end) -> list[Meeting]` —
   and should raise `CalendarFetchError` for expected failures.
2. Register it in `PROVIDER_FACTORIES` in `app/providers/__init__.py`.
3. Add `"graph"` to the `CALENDAR_SOURCE` choices in `app/config_schema.py`,
   along with any new options (client id, tenant, and so on).

Nothing else changes. The refresh loop, the disk cache, the outage handling, the
Settings UI and the dashboard all come along for free, because they only ever
see `Meeting` objects.

---

## Configuration

Three ways to change any setting, all equivalent:

```bash
# 1. The Settings page                    http://<pi>:8080/settings
# 2. The terminal
./scripts/roomctl set ROOM_NAME "Boardroom"
./scripts/roomctl get CALENDAR_REFRESH_SECONDS
./scripts/roomctl config                  # everything, secrets masked

# 3. The file, then restart
nano config/config.yaml
./scripts/roomctl restart backend
```

**[Full reference: docs/configuration.md](docs/configuration.md)** — all 77
options with defaults, generated from the schema so it cannot go stale.

The most useful ones:

| Option | Default | |
| --- | --- | --- |
| `ROOM_NAME` | `Meeting Room` | Shown on the TV |
| `CALENDAR_ICS_URL` | *(empty)* | The room calendar. The one thing you must set |
| `CALENDAR_REFRESH_SECONDS` | `30` | How often meetings are re-fetched |
| `AUTO_OPEN_MEETING` | `true` | Open meetings without being asked |
| `AUTO_OPEN_MINUTES` | `1` | How early |
| `AUTO_CLICK_JOIN` | `true` | Try to press Join too |
| `RETURN_HOME_MINUTES` | `2` | Grace period after a meeting ends |
| `AIRPLAY_NAME` | *(room name)* | What appears in Screen Mirroring |
| `MICROPHONE_DEVICE` / `SPEAKER_DEVICE` / `CAMERA_DEVICE` | `auto` | `auto` finds the Poly bar |
| `POLY_ANSWER_KEY` etc. | `KEY_ENTER` etc. | Remote button mapping |
| `BACKGROUND_MODE` | `theme` | `theme`, `slideshow` or `solid` |
| `CHROMIUM_RENDER_MODE` | `auto` | Fix for a blank screen: `auto`, `wayland`, `x11`, `software` |
| `DASHBOARD_PORT` | `8080` | |
| `ADMIN_LAN_ACCESS` / `ADMIN_PIN` | `false` / *(empty)* | Access from a phone |
| `LOG_LEVEL` | `INFO` | |

**Layering.** Defaults → `config/config.yaml` → environment variables (`.env`
or the real environment). An environment variable wins, and the Settings page
shows such an option as read-only so the two can never disagree.

**Nothing in the application needs editing to configure a room.** If you find
yourself changing Python to change behaviour, that is a gap — please say so.

---

## Security

The appliance sits on an office network in a shared room, so:

- **The dashboard listens on `127.0.0.1` by default.** Network access is opt-in
  (`ADMIN_LAN_ACCESS`) and the configuration layer *refuses* to enable it
  without an admin PIN — including if you edit the file by hand.
- **The Pi itself is trusted; nothing else is.** Requests from `127.0.0.1` are
  the kiosk and local scripts. Everything else needs the PIN. `X-Forwarded-For`
  is deliberately ignored, so a remote client cannot claim to be local.
- **The QR code on the TV signs a phone in to that room.** This is a deliberate
  trade, not an oversight: the appliance has no keyboard, and first-run setup
  has to be possible before a PIN exists. The threat model is the honest one for
  a meeting room — whoever can see the screen can already walk over and press
  the buttons on it. Two settings narrow it: `CONTROLLER_FULL_ACCESS` off leaves
  a scanned phone with the room buttons only (join, leave, mute, camera, volume
  — the API behind them accepts that fixed list and refuses everything else),
  and `CONTROLLER_REQUIRE_PIN` withdraws even that.
- **The pairing code never leaves the Pi.** It lives in `var/controller-token`
  (mode `0600`), and both the code and the QR image are served only to
  `127.0.0.1` and to signed-in administrators — the dashboard is readable from
  the LAN, so putting either in that payload would hand the room to anyone who
  loaded the page without ever looking at it. Wrong codes are rate-limited on
  the same counter as the PIN, and **New code** on the control panel invalidates
  every phone paired so far.
- **PIN attempts are rate-limited** (6 tries, then a two-minute pause) and
  compared in constant time.
- **Every state-changing request needs a page token** (`X-Room-Token`), which
  only a page served by this app can know. A form on another website cannot set
  a custom header, so cross-site request forgery is closed without a framework.
- **Chromium's debugging port is bound to `127.0.0.1`** and never exposed.
- **No credentials in source.** Secrets live in `config/config.yaml` (mode
  `0600`) or the environment. `config.yaml` is in `.gitignore`.
- **Secrets never reach the logs.** Calendar tokens, meeting URLs, PINs and
  passcodes are redacted at the logging layer — including the short Google Meet
  code, which looks harmless but *is* the key to the meeting.
- **Meeting URLs never reach the browser.** The dashboard joins by meeting id,
  so a link cannot appear in page source or in a screenshot.
- **The admin PIN is shown on the TV only during first-run setup**, and only to
  the kiosk: `/api/state` includes it solely for requests from `127.0.0.1`, so a
  LAN client that can view the dashboard cannot read it. It stops being sent the
  moment a calendar is configured.
- **Uploads are validated by content**, not filename, and are served by exact
  match against the directory listing, so no crafted name can escape it.
- **A tight sudo rule.** The room account may reboot. That is all:
  `/etc/sudoers.d/room-appliance`. Restarting services needs no privileges
  because they are user units.
- **Content-Security-Policy, `X-Frame-Options: DENY`, `nosniff`** and
  `SameSite=Strict` cookies on every response.

**Filesystem permissions**

| Path | Mode | |
| --- | --- | --- |
| `config/config.yaml` | `0600` | Calendar URL, PINs |
| `config/config.yaml.bak` | `0600` | Previous configuration |
| `var/` | `0700` | Everything below it |
| `var/calendar-cache.json` | `0600` | Contains meeting URLs |
| `var/flask-secret-key` | `0600` | Session signing key |
| `var/internal-token` | `0600` | Shared secret for helper scripts |
| `var/controller-token` | `0600` | Pairing code behind the QR on the TV |
| `var/chromium-profile/` | `0700` | Room account sessions and cookies |
| `var/backgrounds/` | `0755` | Images, served to the dashboard |

To check them:

```bash
ls -la config/ var/
stat -c '%a %n' config/config.yaml var/calendar-cache.json
```

**If you publish the dashboard beyond the room's own network**, put it behind a
reverse proxy with TLS and your own authentication. It is designed for a trusted
LAN, not the internet.

---

## Developing without a Raspberry Pi

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
./scripts/dev-run.sh
```

Then open <http://127.0.0.1:8080/> (dashboard), `/panel`, `/settings`.

Development mode simulates the Poly bar and the AirPlay receiver, uses a mock
calendar, and does not start Chromium. Overrides are applied in memory only —
your `config.yaml` is never modified.

```bash
./scripts/dev-run.sh --port 5000
./scripts/dev-run.sh --real-calendar    # use the configured ICS feed
```

To see the screen-sharing view, use the control panel's AirPlay simulation or:

```bash
curl -X POST http://127.0.0.1:8080/api/actions/airplay-simulate \
  -H 'Content-Type: application/json' -H "X-Room-Token: $(
    curl -s -c /tmp/c http://127.0.0.1:8080/panel |
    grep -o 'data-csrf="[^"]*"' | head -1 | sed 's/.*="//;s/"//')" \
  -b /tmp/c -d '{"sharing": true}'
```

### Tests

```bash
.venv/bin/python -m pytest tests/ -q
```

433 tests covering configuration layering and recovery, ICS parsing with real
calendar quirks, link extraction including Outlook SafeLinks, the state machine
(especially "never stuck"), the API, access control, uploads, logging redaction,
and the hardware layers with no hardware attached.

The suite also checks the project itself: every shell script through
`bash -n` and `shellcheck -x --severity=style`, every systemd unit for a valid
start command and `StartLimitIntervalSec=0`, every JavaScript file through
`node --check`, and the generated documentation against the schema.

The injected join-automation JavaScript has its own DOM tests:

```bash
cd tests/js && npm install jsdom && python3 emit_scripts.py && node test_clicker.js
```

These build fake Teams and Meet pre-join screens and assert that the clicker
takes the right step in the right order, ignores hidden and disabled controls,
reaches into shadow DOM, and never types into a meeting-code box.

### After changing a setting's definition

`config/config.example.yaml` and `docs/configuration.md` are generated:

```bash
.venv/bin/python scripts/gen-config-docs.py
```

A test fails if you forget.

---

## Command reference

```bash
./scripts/roomctl status              how is the room?
./scripts/roomctl screen              why is the TV blank / white / wrong?
./scripts/roomctl performance         what machine is this, and how hard is it pushed?
./scripts/roomctl performance high    override the guess (auto|high|balanced|low)
./scripts/roomctl slow-device on      retune for a Pi 3 / older hardware
./scripts/roomctl port                show the dashboard port
./scripts/roomctl port auto           move to a free port (or: port 9123)
./scripts/roomctl doctor              full hardware check
./scripts/roomctl panel               the control-panel address and PIN status

./scripts/roomctl restart [what]      browser | airplay | remote | backend | all
./scripts/roomctl start|stop [what]
./scripts/roomctl logs [unit] [-f]

./scripts/roomctl config              every setting, secrets masked
./scripts/roomctl get KEY
./scripts/roomctl set KEY VALUE
./scripts/roomctl calendar <ics-url>  set it and test it immediately
./scripts/roomctl pin <digits>
./scripts/roomctl lan-admin on|off

./scripts/roomctl minutes             meeting minutes: what is it doing?
./scripts/roomctl minutes list [n]    the last n meetings recorded
./scripts/roomctl minutes show [id]   one meeting (--transcript | --summary)
./scripts/roomctl minutes process <id>  write that meeting up again
./scripts/roomctl minutes delete <id>   remove one for good (--yes)
./scripts/roomctl minutes sweep       apply the retention policy now
./scripts/roomctl minutes people      who the room can recognise

./scripts/roomctl reset               defaults, keeping calendar and PIN
./scripts/roomctl reboot
./scripts/roomctl enable|disable      the whole appliance at boot

./scripts/detect-poly.sh              conference-bar diagnostics
./scripts/diagnose-remote.sh          discover remote key codes
./scripts/install.sh                  re-run safely to upgrade
./scripts/uninstall.sh [--purge]
```

### HTTP API

Localhost needs no authentication; anything else needs the PIN, and every
`POST`/`DELETE` needs the `X-Room-Token` header from the page.

| | |
| --- | --- |
| `GET /api/state` | Everything the dashboard renders |
| `GET /api/health` | Component status, mode, host facts, unit states |
| `GET /api/settings` · `POST /api/settings` | Read and write configuration |
| `POST /api/actions/join` | `{}` for the next meeting, or `{"meeting_id": …}` |
| `POST /api/actions/leave` · `/home` · `/retry-join` | |
| `POST /api/actions/volume` · `/mute` | |
| `POST /api/actions/restart` | `{"target": "browser｜airplay｜backend｜all"}` |
| `POST /api/actions/reboot` · `/reset-safe` | |
| `GET/POST /api/backgrounds` · `DELETE /api/backgrounds/<name>` | Slideshow images |
| `GET /api/diagnostics` · `GET /api/logs` | |
| `GET /api/controller/state` | What the phone controller renders |
| `POST /api/controller/action` | `{"action": "join｜leave｜home｜mute｜camera｜volume_up｜volume_down｜volume_set"}` |
| `POST /api/actions/controller-code` | Issue a new pairing code (admin) |
| `GET /qr/controller.svg` | The pairing code as an image (the Pi and admins only) |
| `GET /c/<code>` | Where the QR points: pairs the phone, opens the controller |

---

## Licence

MIT. See [LICENSE](LICENSE).
