const { JSDOM } = require('jsdom');
const fs = require('fs');
const clickerTeams   = fs.readFileSync(__dirname + '/clicker_teams.js', 'utf8');
const clickerMeet    = fs.readFileSync(__dirname + '/clicker_meet.js', 'utf8');
const clickerGuarded = fs.readFileSync(__dirname + '/clicker_guarded.js', 'utf8');
const clickerMute    = fs.readFileSync(__dirname + '/clicker_mute.js', 'utf8');
const inCall         = fs.readFileSync(__dirname + '/incall.js', 'utf8');

// Kept in step with GUARD_URL in emit_scripts.py.
const GUARD_URL = 'https://meet.google.com/guarded';

// jsdom has no layout engine, so give every element a plausible box.
const PATCH = `
  Element.prototype.getBoundingClientRect = function () {
    const hidden = this.hasAttribute('data-hidden');
    return hidden ? {width:0,height:0,top:0,left:0,bottom:0,right:0}
                  : {width:180,height:44,top:100,left:100,bottom:144,right:280};
  };
  Element.prototype.scrollIntoView = function(){};
  window.__clicks = [];
`;

// A page the clicker can be run against more than once — the room does two
// passes a couple of seconds apart, and that sequence is the thing under test.
function page(html, opts = {}) {
  const dom = new JSDOM(html, {
    runScripts: 'outside-only',
    url: opts.url || 'https://teams.microsoft.com/x',
  });
  const { window } = dom;
  window.eval(PATCH);
  window.eval(`
    var origClick = window.HTMLElement.prototype.click;
    window.HTMLElement.prototype.click = function () {
      window.__clicks.push((this.innerText || this.textContent || this.getAttribute('aria-label') || '').trim());
      if (origClick) { try { origClick.call(this); } catch (e) {} }
    };
  `);
  return {
    window,
    document: window.document,
    clicks: window.__clicks,
    run(script) {
      const out = window.eval(script);
      return out ? JSON.parse(out) : null;
    },
  };
}

function run(html, script, opts = {}) {
  const p = page(html, opts);
  return { result: p.run(script), clicks: p.clicks, window: p.window };
}

// Stand in for a framework that owns the input's value, the way React does:
// a plain `el.value = x` is swallowed, only the prototype's native setter is
// heard. This is the bug behind "each time it asks for the name of thing".
function frameworkOwnsValue(window, el, { reverts = false } = {}) {
  const native = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value');
  Object.defineProperty(el, 'value', {
    configurable: true,
    get() { return reverts ? '' : native.get.call(this); },
    set() { /* the framework drops it and re-renders the old value */ },
  });
  return native;
}

let failures = 0;
function check(name, cond, detail) {
  console.log(`${cond ? 'PASS' : 'FAIL'}  ${name}${detail ? '  -> ' + detail : ''}`);
  if (!cond) failures++;
}

// 1. Teams app-or-browser chooser: must take the browser option, not "Open Teams"
let t = run(`<body>
  <div><button>Open Microsoft Teams</button>
  <button>Download the Windows app</button>
  <a href="#" role="button">Continue on this browser</a></div></body>`, clickerTeams);
check('Teams: picks "Continue on this browser"', t.result.clicked === 'continue on this browser', JSON.stringify(t.result.clicked));

// 2. Teams pre-join: the name goes in on one pass, Join is pressed on the next.
{
  const p = page(`<body>
    <input type="text" aria-label="Type your name" />
    <button>Join now</button><button>Cancel</button></body>`);
  const first = p.run(clickerTeams);
  check('Teams: the first pass fills the name and presses nothing',
        first.filled_name === true && first.clicked === null && p.clicks.length === 0,
        JSON.stringify(first));
  check('Teams: the name really is in the box',
        p.document.querySelector('input').value === 'Meeting Room',
        p.document.querySelector('input').value);
  const second = p.run(clickerTeams);
  check('Teams: the next pass presses "Join now"',
        second.clicked === 'join now' && second.filled_name === false,
        JSON.stringify(second));
}

// 3. A React-style input: the plain assignment is ignored, the native setter is not.
{
  const p = page(`<body><input type="text" aria-label="Your name" /><button>Join now</button></body>`);
  const native = frameworkOwnsValue(p.window, p.document.querySelector('input'));
  const first = p.run(clickerTeams);
  check('React-style input: reported as filled', first.filled_name === true, JSON.stringify(first));
  check('React-style input: the value survived',
        native.get.call(p.document.querySelector('input')) === 'Meeting Room',
        native.get.call(p.document.querySelector('input')));
}

// 4. A page that really does revert the value must be reported honestly.
{
  const p = page(`<body><input type="text" aria-label="Your name" /><button>Join now</button></body>`);
  frameworkOwnsValue(p.window, p.document.querySelector('input'), { reverts: true });
  const first = p.run(clickerTeams);
  check('A reverted name box reports filled_name false', first.filled_name === false, JSON.stringify(first));
}

// 5. Zoom/Webex style contenteditable name box
{
  const p = page(`<body><div contenteditable="true" aria-label="Your name"></div>
                  <button>Join now</button></body>`, { url: 'https://zoom.us/wc/join/1' });
  const first = p.run(clickerMeet);
  check('contenteditable name box is filled', first.filled_name === true, JSON.stringify(first));
  check('contenteditable holds the room name',
        p.document.querySelector('div[contenteditable]').textContent === 'Meeting Room',
        p.document.querySelector('div[contenteditable]').textContent);
  check('contenteditable: no click in the filling pass', p.clicks.length === 0);
}

// 6. Hidden buttons must be ignored
t = run(`<body><button data-hidden>Join now</button><button>Ask to join</button></body>`, clickerMeet);
check('Hidden "Join now" skipped, uses "Ask to join"', t.result.clicked === 'ask to join', JSON.stringify(t.result.clicked));

// 7. Disabled and aria-disabled must be ignored
t = run(`<body><button disabled>Join now</button><button aria-disabled="true">Join meeting</button>
         <button>Ask to join</button></body>`, clickerMeet);
check('Disabled buttons skipped', t.result.clicked === 'ask to join', JSON.stringify(t.result.clicked));

// 8. Nothing matching -> no click, reports candidate count
t = run(`<body><button>Report a problem</button><button>Settings</button></body>`, clickerMeet);
check('No match -> clicked null', t.result.clicked === null, JSON.stringify(t.result));
check('No match -> no click fired', t.clicks.length === 0);

// 9. Shadow DOM traversal
{
  const p = page(`<body><div id="host"></div></body>`, { url: 'https://meet.google.com/x' });
  p.window.eval(`
    const host = document.getElementById('host');
    const sr = host.attachShadow({mode:'open'});
    sr.innerHTML = '<button>Join now</button>';
  `);
  const r = p.run(clickerMeet);
  check('Shadow DOM: finds button inside shadow root', r.clicked === 'join now', JSON.stringify(r.clicked));
}

// 10. Exact match beats a longer "contains" match
t = run(`<body><button>Join now with the mobile app instead</button><button>Join now</button></body>`, clickerMeet);
check('Exact text preferred over contains', t.clicks[0] === 'Join now', JSON.stringify(t.clicks));

// 11. Priority ordering: pre-join step before Join now
t = run(`<body><button>Join now</button><a role="button" href="#">Continue on this browser</a></body>`, clickerTeams);
check('Provider priority text wins', t.result.clicked === 'continue on this browser', JSON.stringify(t.result.clicked));

// 12. aria-label only button
t = run(`<body><button aria-label="Join now"><svg></svg></button></body>`, clickerMeet);
check('aria-label used when there is no text', t.result.clicked === 'join now', JSON.stringify(t.result.clicked));

// 13. Fields that must never be typed into, however "name"-ish they look
{
  const forbidden = [
    ['<input type="text" aria-label="Enter a meeting code" />', 'meeting code'],
    ['<input type="text" aria-label="Meeting name" />', 'meeting name'],
    ['<input type="text" aria-label="Your name or email" />', 'name or email'],
    ['<input type="text" aria-label="Name (or PIN)" />', 'name or PIN'],
    ['<input type="text" aria-label="Passcode name" />', 'passcode'],
    ['<input type="text" aria-label="Your name" name="user-id" />', 'user id'],
    ['<input type="text" aria-label="Room" />', 'no "name" in the hint'],
  ];
  forbidden.forEach(function (entry) {
    const p = page(`<body>${entry[0]}<button>Ask to join</button></body>`,
                   { url: 'https://meet.google.com/' });
    p.run(clickerMeet);
    check(`Never typed into: ${entry[1]}`, p.document.querySelector('input').value === '',
          JSON.stringify(p.document.querySelector('input').value));
  });
}

// 14. A name box someone already filled in is left alone
{
  const p = page(`<body><input type="text" aria-label="Your name" value="Alice" />
                  <button>Join now</button></body>`);
  const r = p.run(clickerTeams);
  check('A name already typed is not overwritten',
        p.document.querySelector('input').value === 'Alice' && r.filled_name === false,
        p.document.querySelector('input').value);
  check('...and the pass gets on with pressing Join', r.clicked === 'join now', JSON.stringify(r.clicked));
}

// 15. The repeat guard: same button, same page -> not pressed again
{
  const p = page(`<body><button>Join now</button></body>`, { url: GUARD_URL });
  const r = p.run(clickerGuarded);
  check('Repeat guard: the same button on the same page is left alone',
        r.clicked === null && p.clicks.length === 0, JSON.stringify(r));
}
{
  const p = page(`<body><button>Join now</button></body>`, { url: 'https://meet.google.com/moved-on' });
  const r = p.run(clickerGuarded);
  check('Repeat guard: a new page releases it', r.clicked === 'join now', JSON.stringify(r.clicked));
}

// 16. The lobby: waiting to be admitted is not a reason to click harder
{
  const p = page(`<body><h1>Asking to be let in</h1><button>Ask to join</button></body>`,
                 { url: 'https://meet.google.com/x' });
  const r = p.run(clickerMeet);
  check('Lobby: nothing is pressed', r.clicked === null && p.clicks.length === 0, JSON.stringify(r));
  check('Lobby: the pass says what the page says', r.waiting === 'asking to be let in', JSON.stringify(r.waiting));
}
{
  const p = page(`<body><div>Please wait, the host will let you in.</div>
                  <button>Join now</button></body>`, { url: 'https://us02web.zoom.us/wc/1' });
  const r = p.run(clickerMeet);
  check('Lobby: "please wait" next to being let in counts', r.waiting !== '' && r.clicked === null, JSON.stringify(r));
}
{
  const p = page(`<body><div>Please wait while the page loads.</div>
                  <button>Join now</button></body>`, { url: 'https://us02web.zoom.us/wc/1' });
  const r = p.run(clickerMeet);
  check('"Please wait" on its own is not a lobby', r.waiting === '' && r.clicked === 'join now', JSON.stringify(r));
}

// 17. The mute pass can only ever mute
{
  const p = page(`<body><button aria-label="Unmute microphone"></button></body>`);
  const r = p.run(clickerMute);
  check('Mute pass never presses Unmute', r.clicked === null && p.clicks.length === 0, JSON.stringify(r));
}
{
  const p = page(`<body><button aria-label="Turn off microphone"></button></body>`);
  const r = p.run(clickerMute);
  check('Mute pass presses "Turn off microphone"', r.clicked === 'turn off microphone', JSON.stringify(r.clicked));
}

// 18. in-call detection
{
  const mk = (html) => {
    const dom = new JSDOM(html, { runScripts: 'outside-only' });
    dom.window.eval(PATCH);
    return dom.window.eval(inCall);
  };
  check('in-call: detects "Leave call"', mk('<body><button aria-label="Leave call"></button></body>') === 'true');
  check('in-call: detects "Hang up"',   mk('<body><button>Hang up</button></body>') === 'true');
  check('in-call: detects "Leave the meeting"', mk('<body><button>Leave the meeting</button></body>') === 'true');
  check('in-call: detects a title attribute', mk('<body><span title="End call"></span></body>') === 'true');
  check('in-call: false on pre-join',   mk('<body><button>Join now</button></body>') === 'false');
  check('in-call: false in the lobby',
        mk('<body><div>Asking to be let in</div><button>Cancel</button></body>') === 'false');
}

// 19. Cross-origin iframe must not throw
t = run(`<body><iframe src="https://example.com/x"></iframe><button>Join now</button></body>`, clickerMeet);
check('Cross-origin iframe does not break the pass', t.result.clicked === 'join now', JSON.stringify(t.result.clicked));

console.log(failures === 0 ? '\nALL JS TESTS PASSED' : `\n${failures} JS TEST(S) FAILED`);
process.exit(failures ? 1 : 0);
