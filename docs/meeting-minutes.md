# Meeting minutes

The appliance can record each meeting held in the room, work out who said what,
ask Claude for a summary and email it to the people who were there.

**It is off by default, and every part of it is a separate switch.** With
`MINUTES_ENABLED` off, no thread starts, no microphone or camera is opened and
nothing is written to disk. Nothing on this page changes how the room screen,
the calendar or the meeting joining behave.

This page is deliberately blunt about what works well, what works badly and what
does not work at all. Read it before you turn any of this on in a room where
real meetings happen.

---

## Before anything else: recording people has consequences

Recording a room full of people is a legal and social decision, not a technical
one. Whatever the law says where you are, the practical minimum is:

* **Tell the room.** `MINUTES_SHOW_RECORDING_NOTICE` is on by default and puts a
  notice on the TV. Leave it on. It is also worth a sign on the wall — the room
  screen is showing the meeting itself while a meeting is running, so the notice
  is mostly seen before and after.
* **Ask, for anything sensitive.** A transcript of a disciplinary meeting, a
  salary conversation or a customer's confidential material is a liability, not
  an asset. The switch is easy to turn off for a meeting; use it.
* **Delete the audio.** `MINUTES_KEEP_AUDIO_DAYS` is `0` by default, which
  deletes the recording the moment it has been transcribed. The transcript is
  the useful artefact; the audio is the sensitive one. There is very rarely a
  good reason to change this.
* **Keep it short.** `MINUTES_KEEP_DAYS` deletes transcripts and summaries after
  30 days by default.
* **Know where it goes.** With `MINUTES_SUMMARY_ENABLED` on, the transcript is
  sent to the Claude API. With `MINUTES_EMAIL_ENABLED` on, the summary is
  emailed out. Both are off by default, and both are choices about data leaving
  the room.

Face and voice profiles are biometric data. They are stored on the Pi only,
readable only by the appliance's own user, never sent anywhere, and deleting a
person deletes their vectors and photos in the same action.

---

## How it works

### Two microphones, one useful fact

While a meeting runs the appliance records **two separate audio tracks**:

| Track | What it is | Who is on it |
|---|---|---|
| `room.wav` | The conference bar's microphone | The people physically in the room |
| `farend.wav` | The monitor of the speaker output | Everyone dialled in |

Both are recorded through PulseAudio/PipeWire, which mixes rather than locks, so
recording them does not take the microphone or the speaker away from the meeting.

This split is the foundation of everything else. "Was this person in the room or
on the call" is not a guess — it is a fact about which file the audio landed in,
and it cannot be wrong. Every weaker signal is layered on top of it.

### Naming the far end: the meeting window already knows

Teams, Google Meet and Zoom all know exactly who is on the call. Where live
captions are switched on, Teams and Meet have already written down what each
remote person said with their name against it. The appliance reads that straight
out of the meeting page — which is both far more accurate than transcribing the
call audio on a Raspberry Pi and completely free.

`MINUTES_READ_CAPTIONS` (on by default) reads captions when a human has turned
them on. `MINUTES_TURN_ON_CAPTIONS` (off by default) will switch them on when
joining — off by default because captions appear on the TV for everyone, so it
is a visible change to the meeting and should be somebody's decision.

Where there are no captions, the appliance falls back to watching which
participant the meeting UI is highlighting as the active speaker.

### Naming the room: faces and voices

Everyone physically in the room is a single participant to Teams — "Meeting
Room". The meeting window cannot tell them apart, so the appliance has to.

* **Faces** (`MINUTES_IDENTIFY_FACES`) — between meetings, the appliance takes a
  few frames through the conference-bar camera and matches them against enrolled
  people. It cannot look during a meeting: the camera belongs to the meeting
  then, and a second program cannot stream from it. The roster is therefore
  whoever was seen shortly before the meeting started.
* **Voices** (`MINUTES_IDENTIFY_VOICES`) — each in-room speaking turn is matched
  against enrolled voice profiles.

There is one heuristic worth knowing: **when the camera saw exactly one person in
the room, every in-room line is attributed to them** — not because the voice was
recognised, but because there was nobody else there to say it. The transcript
records that the label came from the camera rather than from the voice.

Where nothing knows, the line stays as "Room speaker" rather than being given to
somebody. A confidently wrong name is much worse than a blank one: it puts words
in a named person's mouth.

### Writing it up

The transcript, the participant list and the summaries of the last few meetings
with the same title go to the Claude API, which returns an overview, the key
points, the decisions and the action points. Including the earlier summaries is
what stops a weekly meeting being written up as though nothing had ever been
agreed before; `MINUTES_SUMMARY_CONTEXT_MEETINGS` controls how many.

The prompt tells the model plainly that the transcript is machine-generated, that
speaker labels are guesses, and that it must not invent an attendee, a decision
or an action point. It also treats the transcript as material and never as
instructions, so somebody reading a message aloud in a meeting cannot steer the
summary.

The summary is emailed with every recipient in **Bcc** — the room should not
broadcast everyone's address to everyone else, particularly when some of those
addresses came from face recognition.

---

## What actually works

Honest expectations, from testing and from the published experience of projects
that do this full time.

| Part | How well it works |
|---|---|
| Recording both tracks | **Reliable.** This is the solid part. |
| Naming remote speakers from captions (Teams, Meet) | **Good** — around 75–85% of meetings, when captions are on. |
| Naming remote speakers from the active-speaker highlight | **Mixed.** Google Meet and Zoom around 70–80%. Microsoft Teams no longer exposes a usable speaking indicator, so on Teams this route is effectively dead and captions are the only option. |
| Transcribing the room track | **Good on a Pi 5, slow on a Pi 4, hopeless on a Pi 3.** |
| Recognising faces in the room | **Works close up, fails down the table.** Roughly 90% within 2 m, 40–60% at 3–4 m, near zero beyond 4 m — so perhaps 60–80% of a six-person room named correctly, and the far end of a boardroom table never. |
| Recognising voices in the room | **The weakest part**, though better than it used to be. With the recommended model, expect roughly 55–75% of individual segments and 70–85% of whole speaking turns to be attributed correctly, and 15–22% of speech to be people talking over each other and therefore unattributable at all. Treat it as a suggestion to correct, not an answer to trust. |
| The summary | **Good, and bounded by the transcript.** A summary of a poor transcript is a poor summary, and the prompt is written so that it says so rather than inventing a good one. |

Two things that will not work, and are not bugs:

* **A Raspberry Pi 3 cannot transcribe.** Local speech-to-text is refused on the
  `low` hardware tier with a message saying so. Recording and remote-speaker
  naming still work.
* **Selectors rot.** Reading names out of the Teams, Meet and Zoom pages depends
  on their HTML, and they ship changes every few weeks. Expect to need a fix
  every few months. When a provider changes, the appliance logs
  `minutes.speaker_signal_absent` once per meeting and the transcript quietly
  loses its remote names — it does not break the room, and it does not start
  guessing.

---

## Turning it on

### 1. The feature itself

Settings → **Meeting minutes**. Turn on **Record and write up meetings**. That
alone gets you recordings and, once an engine is installed, transcripts.

### 2. A speech-to-text engine

Nothing is bundled — the engines are large and most rooms want a different one.
`MINUTES_STT_ENGINE` is `auto`, which uses whichever it finds.

**whisper.cpp — recommended.** One binary and one model file. Punctuation and
capitals out of the box, which materially improves the summary.

```bash
# On the Pi, as the appliance's user
mkdir -p ~/room-appliance/var/minutes/models
cd ~/room-appliance/var/minutes/models
# Fetch a model: base.en on a Pi 5, tiny.en on a Pi 4
curl -LO https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.en.bin
# and put the whisper.cpp `whisper-cli` binary somewhere on PATH
```

**faster-whisper** — `pip install faster-whisper` in the appliance's virtualenv.
More accurate, 300–500 MB.

**vosk** — `pip install vosk` plus a model directory. Small and quick; no
punctuation.

The Settings page and `/minutes` both show which engines were found and, for
each one that was not, exactly what is missing.

### 3. Recognising people (optional)

```bash
pip install opencv-python-headless numpy
```

Note `opencv-python-headless` and not the `contrib` build: the detector and
recogniser used here are in OpenCV's core, and contrib is three times the size
for nothing this needs.

Then put the two model files in `var/minutes/models`: `face_detection_yunet`
(about 230 KB) and `face_recognition_sface` (about 37 MB), both from the OpenCV
Zoo at <https://github.com/opencv/opencv_zoo>. The appliance does not download
them itself — a room appliance that reaches out to the internet on its own is a
surprise — and the `/minutes` page names the exact files it is looking for and
whether it found them.

For voices:

```bash
pip install sherpa-onnx numpy webrtcvad-wheels
```

and put a speaker-embedding model — a `.onnx` file with `titanet`, `speaker` or
`ecapa` in its name — in `var/minutes/models`. TitaNet-small is the one to use:
about 40 MB, and in testing against twenty speakers in a simulated reverberant
room it identified the right person 92% of the time, against 38–48% for the
older alternatives. `sherpa-onnx` brings its own ONNX runtime, so that is the
whole dependency — no PyTorch and no compiler.

`webrtcvad-wheels` matters more than its size suggests. Without it the appliance
finds speech by loudness alone, which misses between a third and two thirds of
it in a real room — so a segment may be half of somebody else's sentence, and
the appliance will refuse to put a name to any of them. It will still tell
speakers apart. Note the `-wheels` suffix: plain `webrtcvad` has no ARM build
and would have to be compiled on the Pi.

### 4. The summary

```bash
pip install anthropic
```

Then in Settings: turn on **Write a summary with Claude**, paste an API key from
`console.anthropic.com`, and pick a model. `claude-opus-5` writes the best
summaries; `claude-sonnet-5` is cheaper and very close.

A typical hour-long meeting is roughly 10–15k tokens of transcript, so the cost
per meeting is small — but it is not zero, and it is per meeting.

### 5. The email

Turn on **Email the summary** and fill in your SMTP details. With Gmail or
Microsoft 365 you need an **app password**, not the account password. Use the
**Send a test email** button on the `/minutes` page before trusting it.

Recipients are the calendar invitation's attendee list plus anyone the appliance
recognised in the room, plus anything in `MINUTES_EMAIL_ALWAYS_TO`. Putting your
own address in that last one is a good idea while you are getting a feel for what
is going out.

---

## Enrolling people

Everything is on the **/minutes** page, admin only.

**Add a person** — a name and, so they can be emailed, an address.

**A photo** — upload one clear, front-on photo. One face per photo; a photo of
two people is refused, because it is usually a mistake.

**A voice** — press **Record a voice sample** and speak in the room for a few
seconds. The appliance records through its own far-field microphone, which is
the microphone that will have to recognise them later, so this is the better way
round rather than a workaround. (A phone browser cannot open a microphone over
plain HTTP on a LAN address, so it could not have done this anyway.)

**The best way** — after a meeting, open its transcript, find a line labelled
"Room speaker", and say who it was. The transcript is corrected and the voice is
added to that person's profile at the same time. It costs nobody anything and it
uses exactly the audio the appliance got wrong.

A profile keeps up to a dozen samples of each kind, oldest dropped first, so it
follows a person as they change. A sample the appliance adds on its own has to
already resemble the profile it is joining — otherwise one mislabelled speaker
turn teaches "Charlie" somebody else's voice and every later match quietly gets
worse. A person adding a sample by hand is trusted and skips that check.

---

## Where things are kept

```
var/minutes/
  people/people.json          profiles, including face and voice vectors
  people/photos/              reference photos
  models/                     speech and face models you installed
  sessions/<id>/
    meta.json                 the meeting, and how far it got
    room.wav, farend.wav      deleted once transcribed, by default
    roster.jsonl              who the meeting window said was speaking, over time
    captions.jsonl            the meeting's own captions, if any
    presence.json             who the camera saw before the meeting
    transcript.json           the attributed transcript
    summary.json              what Claude wrote
    delivery.json             who it was emailed to
```

Everything is owner-readable only. Deleting `var/minutes` removes every trace of
the feature.

---

## When it does not work

Start at **/minutes**. The status section lists every moving part and, for
anything unavailable, the reason and the command that fixes it.

| What you see | What it usually is |
|---|---|
| Nothing is recorded | `MINUTES_ENABLED` off, or the meeting was shorter than `MINUTES_MIN_MEETING_SECONDS` (two minutes by default), or the disk is more than 85% full — recording is refused rather than filling the card and taking the room down with it. |
| A transcript with no words | No speech-to-text engine found. The status section names what to install. |
| A transcript with no names on the remote side | Captions were off and the provider's active-speaker indicator could not be read. On Teams this is expected; turn captions on. |
| Everyone in the room is "Room speaker" | Face and voice recognition are off, or nobody is enrolled, or the camera saw more than one person and no voice matched. |
| The wrong name in the room | Raise `MINUTES_FACE_THRESHOLD` or `MINUTES_VOICE_THRESHOLD`, and add more samples for the people being confused. |
| A summary but no email | Check the SMTP settings with the test button. Gmail and 365 need an app password. |
| The far-end track is silent | The default speaker changed mid-meeting — usually the Poly bar being re-plugged. The recorder notices and reconnects, and says so in the session's notices. |
| `journalctl -u room-dashboard -f` | Every stage logs one structured event: `minutes.recording_started`, `minutes.recording_stopped`, `minutes.summary_written`, `minutes.summary_sent`. |
