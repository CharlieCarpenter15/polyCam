# Join-clicker DOM tests

`test_clicker.js` exercises the JavaScript that `app/join_flows.py` injects into
the meeting page. It builds fake Teams / Meet / Zoom pre-join screens with
`jsdom` and asserts that the clicker:

* takes the "Continue on this browser" step before "Join now"
* fills a guest name box **through the input's native value setter**, so a
  React page (Teams, Meet) keeps the value instead of reverting it, and reports
  `filled_name` only when the value really stuck
* fills a `contenteditable` name box too (Zoom and Webex use one in places)
* fills the name on one pass and presses Join on the *next* — pressing both in
  one pass either hits a still-disabled button or bounces back to the pre-join
  screen
* never types into a meeting-code, passcode, password, email, PIN or id box,
  and never overwrites a name someone has already typed
* honours the repeat guard: the same button is not pressed twice on the same
  page, and moving to a new URL releases it
* stops clicking altogether once the page says the room is waiting to be let in
* ignores hidden, disabled and `aria-disabled` controls
* finds buttons inside open shadow roots
* prefers an exact text match over a longer "contains" match
* survives a cross-origin iframe
* mutes, and only mutes, on the `JOIN_MUTE_ON_ENTRY` pass — "Mute" is a
  substring of "Unmute", so the deny list is what keeps it one-way
* recognises an in-call page by its leave/hang-up control, and does *not*
  mistake a lobby screen for one

These tests are optional (Node and `jsdom` are not installed on the appliance).
Run them on a development machine:

```bash
cd tests/js
npm install jsdom
python3 emit_scripts.py     # writes the current clicker JS next to the test
node test_clicker.js
```

`emit_scripts.py` writes `clicker_teams.js`, `clicker_meet.js`,
`clicker_guarded.js` (armed with a repeat guard), `clicker_mute.js` and
`incall.js`. All of them are generated, and all are ignored by git.

---

# Meeting-window DOM tests

`test_roster.js` exercises the JavaScript that `app/minutes/roster.py` injects
into a live meeting to read who is on the call and, where live captions are on,
what each of them said. It builds fake Teams / Meet / Zoom meeting stages with
`jsdom` and asserts that each probe:

* returns the same shape whatever it finds, and `{ok: false, reason}` rather
  than throwing when it finds nothing
* never turns a diagnostic string into a speaker's name — an unknown speaker
  stays unknown, because a confidently wrong name puts words in somebody's mouth
* never presses anything, with the single exception of the captions control
* reads names from open shadow roots and same-origin iframes, and survives a
  cross-origin one
* holds an interim caption back until the sentence has stopped changing, so the
  transcript is not full of half-sentences
* installs its in-page observer only once, keeps its state across a re-install,
  and tears itself down when the page moves on
* refuses to install in a frame that has no meeting in it, leaving nothing
  behind — which is what lets the caller try the next frame instead of settling
  in the page shell and watching an empty room for the whole meeting
* presses the participants control when asked to, exactly once, and never
  presses "Hide participants", "Invite people" or anything about leaving

Run them on a development machine:

```bash
cd tests/js
npm install jsdom
python3 emit_roster.py      # writes the current roster JS next to the test
node test_roster.js
```

`emit_roster.py` writes `clicker_roster_*.js`, which the existing `.gitignore`
rule already covers.
