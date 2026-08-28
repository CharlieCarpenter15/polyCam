# Join-clicker DOM tests

`test_clicker.js` exercises the JavaScript that `app/join_flows.py` injects into
the meeting page. It builds fake Teams / Meet pre-join screens with `jsdom` and
asserts that the clicker:

* takes the "Continue on this browser" step before "Join now"
* fills a guest name box, but never a *meeting code* box
* ignores hidden, disabled and `aria-disabled` controls
* finds buttons inside open shadow roots
* prefers an exact text match over a longer "contains" match
* survives a cross-origin iframe
* recognises an in-call page by its leave/hang-up control

These tests are optional (Node and `jsdom` are not installed on the appliance).
Run them on a development machine:

```bash
cd tests/js
npm install jsdom
python3 emit_scripts.py     # writes the current clicker JS next to the test
node test_clicker.js
```
