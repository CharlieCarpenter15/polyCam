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
