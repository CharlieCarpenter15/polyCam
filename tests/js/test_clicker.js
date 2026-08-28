const { JSDOM } = require('jsdom');
const fs = require('fs');
const clickerTeams = fs.readFileSync(__dirname + '/clicker_teams.js', 'utf8');
const clickerMeet  = fs.readFileSync(__dirname + '/clicker_meet.js', 'utf8');
const inCall       = fs.readFileSync(__dirname + '/incall.js', 'utf8');

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

function run(html, script, opts = {}) {
  const dom = new JSDOM(html, { runScripts: 'outside-only', url: opts.url || 'https://teams.microsoft.com/x' });
  const { window } = dom;
  window.eval(PATCH);
  // Record clicks
  window.eval(`
    const origClick = window.HTMLElement.prototype.click;
    window.HTMLElement.prototype.click = function () {
      window.__clicks.push((this.innerText || this.textContent || this.getAttribute('aria-label') || '').trim());
      if (origClick) { try { origClick.call(this); } catch (e) {} }
    };
  `);
  const out = window.eval(script);
  return { result: out ? JSON.parse(out) : null, clicks: window.__clicks, window };
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

// 2. Teams pre-join: fills the name box and presses Join now
t = run(`<body>
  <input type="text" aria-label="Type your name" />
  <button>Join now</button><button>Cancel</button></body>`, clickerTeams);
check('Teams: clicks "Join now"', t.result.clicked === 'join now', JSON.stringify(t.result.clicked));
check('Teams: fills the name field', t.result.filled_name === true);
check('Teams: name value is the room name',
      t.window.document.querySelector('input').value === 'Meeting Room',
      t.window.document.querySelector('input').value);

// 3. Hidden buttons must be ignored
t = run(`<body><button data-hidden>Join now</button><button>Ask to join</button></body>`, clickerMeet);
check('Hidden "Join now" skipped, uses "Ask to join"', t.result.clicked === 'ask to join', JSON.stringify(t.result.clicked));

// 4. Disabled and aria-disabled must be ignored
t = run(`<body><button disabled>Join now</button><button aria-disabled="true">Join meeting</button>
         <button>Ask to join</button></body>`, clickerMeet);
check('Disabled buttons skipped', t.result.clicked === 'ask to join', JSON.stringify(t.result.clicked));

// 5. Nothing matching -> no click, reports candidate count
t = run(`<body><button>Report a problem</button><button>Settings</button></body>`, clickerMeet);
check('No match -> clicked null', t.result.clicked === null, JSON.stringify(t.result));
check('No match -> no click fired', t.clicks.length === 0);

// 6. Shadow DOM traversal
t = run(`<body><div id="host"></div></body>`, clickerMeet);
// build shadow content before running: rerun manually
{
  const dom = new JSDOM(`<body><div id="host"></div></body>`, { runScripts: 'outside-only', url:'https://meet.google.com/x' });
  const w = dom.window;
  w.eval(PATCH);
  w.eval(`
    const origClick = window.HTMLElement.prototype.click;
    window.HTMLElement.prototype.click = function () {
      window.__clicks.push((this.innerText || this.textContent || '').trim());
    };
    const host = document.getElementById('host');
    const sr = host.attachShadow({mode:'open'});
    sr.innerHTML = '<button>Join now</button>';
  `);
  const r = JSON.parse(w.eval(clickerMeet));
  check('Shadow DOM: finds button inside shadow root', r.clicked === 'join now', JSON.stringify(r.clicked));
}

// 7. Exact match beats a longer "contains" match
t = run(`<body><button>Join now with the mobile app instead</button><button>Join now</button></body>`, clickerMeet);
check('Exact text preferred over contains', t.clicks[0] === 'Join now', JSON.stringify(t.clicks));

// 8. Priority ordering: pre-join step before Join now
t = run(`<body><button>Join now</button><a role="button" href="#">Continue on this browser</a></body>`, clickerTeams);
check('Provider priority text wins', t.result.clicked === 'continue on this browser', JSON.stringify(t.result.clicked));

// 9. aria-label only button
t = run(`<body><button aria-label="Join now"><svg></svg></button></body>`, clickerMeet);
check('aria-label used when there is no text', t.result.clicked === 'join now', JSON.stringify(t.result.clicked));

// 10. Meeting-code box must NOT be filled as a name
{
  const dom = new JSDOM(`<body><input type="text" aria-label="Enter a meeting code" /><button>Ask to join</button></body>`,
                        { runScripts:'outside-only', url:'https://meet.google.com/' });
  const w = dom.window; w.eval(PATCH);
  const r = JSON.parse(w.eval(clickerMeet));
  check('Meeting-code field is not treated as a name box', w.document.querySelector('input').value === '',
        JSON.stringify(w.document.querySelector('input').value));
}

// 11. in-call detection
{
  const mk = (html) => {
    const dom = new JSDOM(html, { runScripts:'outside-only' });
    dom.window.eval(PATCH);
    return dom.window.eval(inCall);
  };
  check('in-call: detects "Leave call"', mk('<body><button aria-label="Leave call"></button></body>') === 'true');
  check('in-call: detects "Hang up"',   mk('<body><button>Hang up</button></body>') === 'true');
  check('in-call: false on pre-join',   mk('<body><button>Join now</button></body>') === 'false');
}

// 12. Cross-origin iframe must not throw
t = run(`<body><iframe src="https://example.com/x"></iframe><button>Join now</button></body>`, clickerMeet);
check('Cross-origin iframe does not break the pass', t.result.clicked === 'join now', JSON.stringify(t.result.clicked));

console.log(failures === 0 ? '\nALL JS TESTS PASSED' : `\n${failures} JS TEST(S) FAILED`);
process.exit(failures ? 1 : 0);
