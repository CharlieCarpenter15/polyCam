// DOM tests for the meeting-window reader that app/minutes/roster.py injects.
//
// The fake Teams / Meet / Zoom markup below is reconstructed from what several
// open-source meeting bots scrape, not from a live tenant. Passing here proves
// the scripts are well formed, total, and correct against the DOM shapes the
// research documented. It says nothing about whether those shapes still match
// a real page today; only a real meeting settles that.
//
// These tests are optional: Node and jsdom are not installed on the appliance
// and are not needed there. Run them on a development machine with
//
//   cd tests/js
//   npm install jsdom
//   python3 emit_roster.py     # writes the current roster JS next to the test
//   node test_roster.js
//
// emit_roster.py writes clicker_roster_teams.js, clicker_roster_meet.js,
// clicker_roster_zoom.js, clicker_roster_generic.js, the observer and drain
// pair, and clicker_roster_captions.js. All are generated, and all are already
// ignored by the repository's tests/js/clicker_*.js rule.
const { JSDOM } = require('jsdom');
const fs = require('fs');

const read = (name) => fs.readFileSync(__dirname + '/' + name, 'utf8');
const teams   = read('clicker_roster_teams.js');
const meet    = read('clicker_roster_meet.js');
const zoom    = read('clicker_roster_zoom.js');
const generic = read('clicker_roster_generic.js');
const install = read('clicker_roster_install.js');
const installMeet  = read('clicker_roster_install_meet.js');
const installQuiet = read('clicker_roster_install_quiet.js');
const drain      = read('clicker_roster_drain.js');
const drainFlush = read('clicker_roster_drain_flush.js');
const drainOther = read('clicker_roster_drain_other.js');
const captions   = read('clicker_roster_captions.js');

// Kept in step with TICK_MS in emit_roster.py.
const TICK_MS = 20;

// jsdom has no layout engine, so give every element a plausible box — the
// caption click pass rejects zero-size elements the way a browser would.
const PATCH = `
  Element.prototype.getBoundingClientRect = function () {
    const hidden = this.hasAttribute('data-hidden');
    return hidden ? {width:0,height:0,top:0,left:0,bottom:0,right:0}
                  : {width:180,height:44,top:100,left:100,bottom:144,right:280};
  };
  Element.prototype.scrollIntoView = function(){};
  window.__clicks = [];
`;

const OPEN = [];
function page(html, opts = {}) {
  const dom = new JSDOM(html, {
    runScripts: 'outside-only',
    url: opts.url || 'https://teams.microsoft.com/x',
    pretendToBeVisual: true,
  });
  OPEN.push(dom);
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
    dom,
    window,
    document: window.document,
    clicks: window.__clicks,
    run(script) {
      const out = window.eval(script);
      return typeof out === 'string' ? JSON.parse(out) : out;
    },
  };
}

function run(html, script, opts = {}) {
  const p = page(html, opts);
  return { result: p.run(script), clicks: p.clicks, window: p.window, page: p };
}

let failures = 0;
function check(name, cond, detail) {
  console.log(`${cond ? 'PASS' : 'FAIL'}  ${name}${detail ? '  -> ' + detail : ''}`);
  if (!cond) failures++;
}

const names = (r) => r.participants.map((p) => p.name);
const sleep = (ms) => new Promise((done) => setTimeout(done, ms));

// The shape every probe promises Python, whatever it finds.
function contractOk(r) {
  return !!r &&
    typeof r.provider === 'string' &&
    Array.isArray(r.participants) &&
    Array.isArray(r.speaking) &&
    Array.isArray(r.captions) &&
    typeof r.ok === 'boolean' &&
    typeof r.reason === 'string' &&
    typeof r.source === 'string' &&
    typeof r.at === 'number' &&
    r.health !== null && typeof r.health === 'object' &&
    r.participants.every((p) => typeof p.name === 'string' && typeof p.role === 'string');
}

// ---------------------------------------------------------------------------
// 1. Microsoft Teams
// ---------------------------------------------------------------------------

let r = run(`<body>
  <div data-stream-type="Video" data-tid="Alice Ng">
    <div data-tid="voice-level-stream-outline" aria-label="Alice Ng is speaking"></div></div>
  <div data-stream-type="Video" data-tid="Bob Chen">
    <div data-tid="voice-level-stream-outline"></div></div>
  <div data-stream-type="Video" data-tid="ops@corp.com">
    <div data-tid="voice-level-stream-outline"></div></div>
</body>`, teams);
check('Teams: the contract shape is honoured', contractOk(r.result), JSON.stringify(r.result));
check('Teams: names come from data-tid on the v2 client',
      JSON.stringify(names(r.result)) === '["Alice Ng","Bob Chen"]', JSON.stringify(r.result.participants));
check('Teams: an email address in data-tid is rejected as personal data, not a name',
      JSON.stringify(r.result).indexOf('ops@corp.com') === -1);
check('Teams: the aria speaking state is believed',
      JSON.stringify(r.result.speaking) === '["Alice Ng"]', JSON.stringify(r.result.speaking));
check('Teams: found the meeting -> ok true, no reason',
      r.result.ok === true && r.result.reason === '', JSON.stringify(r.result.reason));
check('Teams: the reply says which selector answered',
      r.result.source.indexOf('tiles:[data-stream-type="Video"][data-tid]') === 0, r.result.source);

r = run(`<body><div data-cid="calling-participant-stream" aria-label="Cara Diaz, Muted">
  <div data-tid="voice-level-stream-outline" class="vdi-frame-occlusion"></div>
  <div data-cid="roster-participant-muted"></div></div></body>`, teams);
check('Teams: a muted tile can never be the speaker',
      r.result.speaking.length === 0 && names(r.result)[0] === 'Cara Diaz', JSON.stringify(r.result));

r = run(`<body>
  <div data-tid="roster"><div data-tid="roster-participant" aria-label="Alice Ng"></div>
    <div data-tid="roster-participant" aria-label="Bob Chen"></div></div>
  <div data-tid="closed-caption-renderer-wrapper">Alice Ng: hello there
    Bob Chen: yes I agree with that</div></body>`, teams);
check('Teams: the roster panel is read where a human has opened it',
      r.result.participants.length === 2, JSON.stringify(r.result.participants));
check('Teams: unstructured captions attribute the latest author only, never all of them',
      JSON.stringify(r.result.speaking) === '["Bob Chen"]', JSON.stringify(r.result.speaking));

r = run(`<body><div data-tid="closed-caption-v2-virtual-list-content">
  <div class="fui-ChatMessageCompact">
    <span data-tid="author">Alice Ng</span>
    <span data-tid="closed-caption-text">so the plan is to ship on Friday</span></div>
  <div class="fui-ChatMessageCompact">
    <span data-tid="author">Bob Chen</span>
    <span data-tid="closed-caption-text">that works for me</span></div>
</div></body>`, teams);
check('Teams: structured caption rows carry the speaker with the words',
      r.result.captions.length === 2 &&
      r.result.captions[0].speaker === 'Alice Ng' &&
      r.result.captions[1].text === 'that works for me', JSON.stringify(r.result.captions));
check('Teams: the newest caption author is the one shown as speaking',
      JSON.stringify(r.result.speaking) === '["Bob Chen"]', JSON.stringify(r.result.speaking));

r = run(`<body><div data-tid="closed-caption-v2-virtual-list-content">
  <div><span data-tid="author">Alice Ng</span></div>
  <div><span data-tid="closed-caption-text">the wrapper is in the way</span></div>
</div></body>`, teams);
check('Teams: the host view wrapper falls back to pairing by document order',
      r.result.captions.length === 1 && r.result.captions[0].speaker === 'Alice Ng',
      JSON.stringify(r.result.captions));

r = run('<body><h1>Loading</h1></body>', teams);
check('Teams: an empty page -> ok false with a machine-readable reason',
      r.result.ok === false && r.result.reason === 'no-provider-surface', JSON.stringify(r.result));

r = run(`<body><div data-stream-type="Video" data-tid="Alice Ng">
  <div data-tid="voice-level-stream-outline"></div></div></body>`, teams);
check('Teams: found the meeting, nobody talking -> ok true with an empty speaking list',
      r.result.ok === true && r.result.speaking.length === 0 && r.result.reason === '',
      JSON.stringify(r.result));

// ---------------------------------------------------------------------------
// 2. Google Meet
// ---------------------------------------------------------------------------

r = run(`<body><div aria-label="Participants" role="list">
  <div role="listitem" aria-label="Dana Ito"><div class="IisKdb Oaajhc"></div></div>
  <div role="listitem" aria-label="Eve Park"><div class="IisKdb gjg47c"></div></div>
  <div role="listitem" aria-label="Merged audio"><div aria-label="Adaptive audio group"></div></div>
</div></body>`, meet, { url: 'https://meet.google.com/abc' });
check('Meet: the contract shape is honoured', contractOk(r.result), JSON.stringify(r.result));
check('Meet: the listitem aria-label is the name',
      JSON.stringify(names(r.result)) === '["Dana Ito","Eve Park","Merged audio"]',
      JSON.stringify(r.result.participants));
check('Meet: an adaptive-audio cohort is marked merged, not blamed on a person',
      r.result.participants[2].role === 'merged', JSON.stringify(r.result.participants[2]));
check('Meet: the level meter names a speaker',
      JSON.stringify(r.result.speaking) === '["Dana Ito"]', JSON.stringify(r.result.speaking));
check('Meet: the silence class is not mistaken for speech',
      r.result.speaking.indexOf('Eve Park') === -1);

r = run(`<body>
  <div data-participant-id="p1"><span class="notranslate">Frank Li</span></div>
  <div data-participant-id="p2"><span class="notranslate">Gita Rao</span><div class="kssMZb"></div></div>
  <div role="region" aria-label="Captions"><div jsname="dsyhDe">
    <div class="zs7s8d">Frank Li</div><div class="iTTPOb">so the plan is</div></div></div>
</body>`, meet, { url: 'https://meet.google.com/abc' });
check('Meet: tile names still work with the panel closed',
      r.result.participants.length === 2, JSON.stringify(r.result.participants));
check('Meet: a tile level class and a caption author both count as speaking',
      r.result.speaking.indexOf('Gita Rao') !== -1 && r.result.speaking.indexOf('Frank Li') !== -1,
      JSON.stringify(r.result.speaking));
check('Meet: caption rows carry speaker and words',
      r.result.captions.length === 1 && r.result.captions[0].speaker === 'Frank Li' &&
      r.result.captions[0].text === 'so the plan is', JSON.stringify(r.result.captions));

r = run('<body><div>a page that is not a meeting</div></body>', meet,
        { url: 'https://meet.google.com/abc' });
check('Meet: nothing found -> ok false',
      r.result.ok === false && r.result.reason === 'no-provider-surface', JSON.stringify(r.result));

// ---------------------------------------------------------------------------
// 3. Zoom
// ---------------------------------------------------------------------------

r = run(`<body>
  <div id="participants-ul"><span class="participants-item__display-name">Hana Sato (Host)</span>
    <span class="participants-item__display-name">Ivan Ruiz</span></div>
  <div class="speaker-active-container__video-frame"><div class="video-avatar__avatar-footer">
    <span role="none">Hana Sato</span></div></div>
  <div class="speaker-bar-container__video-frame--active"><div class="video-avatar__avatar-footer">
    <span role="none">Ivan Ruiz</span></div></div>
</body>`, zoom, { url: 'https://us02web.zoom.us/wc/1' });
check('Zoom: the contract shape is honoured', contractOk(r.result), JSON.stringify(r.result));
check('Zoom: roster names, with the role suffix stripped',
      JSON.stringify(names(r.result)) === '["Hana Sato","Ivan Ruiz"]',
      JSON.stringify(r.result.participants));
check('Zoom: the host is recorded as the host',
      r.result.participants[0].role === 'host', JSON.stringify(r.result.participants[0]));
check('Zoom: the --active speaker bar beats a pinned main tile',
      JSON.stringify(r.result.speaking) === '["Ivan Ruiz"]', JSON.stringify(r.result.speaking));
check('Zoom: the reply says which active-speaker selector answered',
      r.result.source.indexOf('speaking:.speaker-bar-container__video-frame--active') !== -1,
      r.result.source);

r = run(`<body><div class="video-avatar__avatar"><div class="video-avatar__avatar-footer">
  <span role="none">Jo Kim</span></div></div></body>`, zoom, { url: 'https://us02web.zoom.us/wc/1' });
check('Zoom: found the meeting, nobody talking -> ok true, speaking empty',
      r.result.ok === true && r.result.speaking.length === 0, JSON.stringify(r.result));
check('Zoom: there are no captions in the browser client, and it says so',
      r.result.captions.length === 0);

r = run('<body></body>', zoom, { url: 'https://us02web.zoom.us/wc/1' });
check('Zoom: nothing found -> ok false',
      r.result.ok === false && r.result.reason === 'no-provider-surface', JSON.stringify(r.result));

// ---------------------------------------------------------------------------
// 4. An unrecognised meeting link tries all three and latches on
// ---------------------------------------------------------------------------

r = run(`<body><div class="video-avatar__avatar"><div class="video-avatar__avatar-footer">
  <span role="none">Jo Kim</span></div></div></body>`, generic, { url: 'https://example.com/x' });
check('Generic: an unknown provider still finds a Zoom-shaped page',
      r.result.ok === true && r.result.provider === 'zoom' && names(r.result)[0] === 'Jo Kim',
      JSON.stringify(r.result));

r = run('<body><h1>nothing here</h1></body>', generic, { url: 'https://example.com/x' });
check('Generic: an unknown provider on a page that is not a meeting -> ok false',
      r.result.ok === false && contractOk(r.result), JSON.stringify(r.result));

// ---------------------------------------------------------------------------
// 5. Hostile and awkward pages must not break any probe
// ---------------------------------------------------------------------------

[['teams', teams], ['meet', meet], ['zoom', zoom], ['generic', generic]].forEach(([label, script]) => {
  const p = page(`<body><div id="h"></div><iframe src="https://other.example/x"></iframe></body>`,
                 { url: 'https://x.example/' });
  p.window.eval(`const sr = document.getElementById('h').attachShadow({mode:'open'});
    sr.innerHTML = '<div class="video-avatar__avatar"><div class="video-avatar__avatar-footer"><span role="none">Shadow Sam</span></div></div>' +
                   '<div data-participant-id="s1"><span class="notranslate">Shadow Sam</span></div>' +
                   '<div data-stream-type="Video" data-tid="Shadow Sam"><div data-tid="voice-level-stream-outline"></div></div>';`);
  let out = null;
  try { out = p.run(script); } catch (e) { out = null; }
  check(`${label}: an open shadow root and a cross-origin iframe are both survived`,
        contractOk(out) && out.reason.indexOf('exception') === -1, JSON.stringify(out));
  check(`${label}: the participant inside the shadow root is found`,
        !!out && out.participants.length === 1, JSON.stringify(out && out.participants));
});

[['teams', teams], ['meet', meet], ['zoom', zoom], ['generic', generic]].forEach(([label, script]) => {
  const p = page(`<body>
    <div data-participant-id="x"><span class="notranslate">${'A'.repeat(500)}</span></div>
    <div class="video-avatar__avatar"></div>
    <div data-stream-type="Video" data-tid="10:42 AM"><div data-tid="voice-level-stream-outline"></div></div>
    <div data-stream-type="Video" data-tid="video-stream-2"></div>
    <div aria-label="Participants" role="list"><div role="listitem" aria-label="more_vert"></div></div>
    </body>`, { url: 'https://x.example/' });
  let out = null;
  try { out = p.run(script); } catch (e) { out = null; }
  check(`${label}: a junk page yields valid JSON and no invented names`,
        contractOk(out) && out.participants.every(
          (q) => q.name.length <= 120 && !/^\d{1,2}:\d{2}/.test(q.name) &&
                 q.name !== 'more_vert' && q.name !== 'video-stream-2'),
        JSON.stringify(out && out.participants));
});

// ---------------------------------------------------------------------------
// 6. The one pass that may switch captions on
// ---------------------------------------------------------------------------

{
  const p = page('<body><button>Turn on live captions</button></body>');
  const out = p.run(captions);
  check('Captions: "Turn on live captions" is pressed',
        out.clicked === 'turn on live captions', JSON.stringify(out.clicked));
}
{
  const p = page('<body><button>Turn off live captions</button></body>');
  const out = p.run(captions);
  check('Captions: captions already on are never switched off',
        out.clicked === null && p.clicks.length === 0, JSON.stringify(out));
}
{
  const p = page('<body><button>Caption settings</button><button>Language and speech</button></body>');
  const out = p.run(captions);
  check('Captions: a settings or language menu is not mistaken for the control',
        out.clicked === null && p.clicks.length === 0, JSON.stringify(out));
}

// ---------------------------------------------------------------------------
// 7. The resident observer: installing, draining, and not doing either twice
// ---------------------------------------------------------------------------

(async function () {
  {
    const p = page(`<body>
      <div data-stream-type="Video" data-tid="Alice Ng">
        <div id="ring" data-tid="voice-level-stream-outline"></div></div>
      <div data-stream-type="Video" data-tid="Bob Chen">
        <div data-tid="voice-level-stream-outline"></div></div>
      <div data-tid="closed-caption-v2-virtual-list-content" id="caps"></div>
    </body>`);

    const first = p.run(install);
    check('Observer: installs once', first.ok === true && first.state === 'installed',
          JSON.stringify(first));
    const again = p.run(install);
    check('Observer: re-installing the same recording is a no-op, not a second timer',
          again.ok === true && again.state === 'already', JSON.stringify(again));

    let out = p.run(drain);
    check('Observer: the first drain already has a sample from the install tick',
          out.ok === true && out.installed === true && out.samples.length >= 1,
          JSON.stringify(out.samples));
    check('Observer: the drain reports the current roster for the web page',
          out.participants.length === 2, JSON.stringify(out.participants));
    check('Observer: a sample is stamped in the page clock, in milliseconds',
          out.samples[0].at > 1e12 && typeof out.now === 'number', JSON.stringify(out.samples[0]));
    check('Observer: the first sample carries the roster, because it changed',
          Array.isArray(out.samples[0].participants), JSON.stringify(out.samples[0]));
    check('Observer: found the meeting but nobody talking yet',
          out.samples[0].speaking.length === 0 && out.signal === false, JSON.stringify(out));
    check('Observer: a drain empties the buffer, so nothing is counted twice',
          p.run(drain).samples.length === 0);

    // Somebody starts talking. The observer should catch the change within a
    // tick or two, which a two-second Python poll never would.
    p.document.getElementById('ring').setAttribute('aria-label', 'Alice Ng is speaking');
    await sleep(TICK_MS * 6);
    out = p.run(drain);
    check('Observer: a speaking edge between polls is caught',
          out.samples.length >= 1 && out.samples[0].speaking[0] === 'Alice Ng',
          JSON.stringify(out.samples));
    check('Observer: the roster is not repeated when it has not changed',
          out.samples.every((s) => s.participants === undefined), JSON.stringify(out.samples));
    check('Observer: having heard a speaker once is remembered',
          out.signal === true && out.surface === true, JSON.stringify({ s: out.signal, u: out.surface }));

    // ...and stops. The observer must record the silence, or the span never closes.
    p.document.getElementById('ring').removeAttribute('aria-label');
    await sleep(TICK_MS * 6);
    out = p.run(drain);
    check('Observer: the moment somebody stops is recorded too',
          out.samples.length >= 1 && out.samples[out.samples.length - 1].speaking.length === 0,
          JSON.stringify(out.samples));

    // A caption line grows a word at a time while it is recognised. Only the
    // finished sentence should ever reach Python.
    const caps = p.document.getElementById('caps');
    caps.innerHTML = '<div class="fui-ChatMessageCompact"><span data-tid="author">Alice Ng</span>' +
                     '<span data-tid="closed-caption-text">so</span></div>';
    await sleep(TICK_MS * 3);
    check('Observer: an interim caption is held back while it is still growing',
          p.run(drain).captions.length === 0);
    caps.querySelector('[data-tid="closed-caption-text"]').textContent = 'so the plan';
    await sleep(TICK_MS * 3);
    caps.querySelector('[data-tid="closed-caption-text"]').textContent = 'so the plan is to ship';
    await sleep(TICK_MS * 3);
    check('Observer: it is still held back, not sent three times',
          p.run(drain).captions.length === 0);

    out = p.run(drainFlush);
    check('Observer: a flushing drain hands over one finished sentence, not the drafts',
          out.captions.length === 1 && out.captions[0].text === 'so the plan is to ship' &&
          out.captions[0].speaker === 'Alice Ng', JSON.stringify(out.captions));

    // A second speaker's line is a new line, even though the row above it grew.
    caps.insertAdjacentHTML('beforeend',
      '<div class="fui-ChatMessageCompact"><span data-tid="author">Bob Chen</span>' +
      '<span data-tid="closed-caption-text">agreed</span></div>');
    await sleep(TICK_MS * 3);
    out = p.run(drainFlush);
    check('Observer: a different speaker starts a new caption line',
          out.captions.length === 1 && out.captions[0].speaker === 'Bob Chen',
          JSON.stringify(out.captions));

    // A virtualised list that re-renders wholesale hands the sampler brand new
    // nodes for sentences it has already dealt with. They must not come back.
    caps.innerHTML = caps.innerHTML;
    await sleep(TICK_MS * 4);
    out = p.run(drainFlush);
    check('Observer: a wholesale re-render does not repeat sentences already sent',
          out.captions.length === 0, JSON.stringify(out.captions));

    check('Observer: a drain from a different recording is refused',
          p.run(drainOther).installed === false, JSON.stringify(p.run(drainOther)));
  }

  // Captions switched off in the settings: the observer collects none.
  {
    const p = page(`<body><div data-tid="closed-caption-v2-virtual-list-content">
      <div class="fui-ChatMessageCompact"><span data-tid="author">Alice Ng</span>
      <span data-tid="closed-caption-text">this should not be recorded</span></div></div></body>`);
    p.run(installQuiet);
    await sleep(TICK_MS * 4);
    const out = p.run(drainFlush);
    check('Observer: with caption reading off, no caption is collected at all',
          out.captions.length === 0, JSON.stringify(out.captions));
  }

  // A page that navigates somewhere else must stop the observer rather than
  // sample whatever is on screen now and file it against this meeting.
  {
    const p = page(`<body><div aria-label="Participants" role="list">
      <div role="listitem" aria-label="Dana Ito"></div></div></body>`,
      { url: 'https://meet.google.com/abc' });
    p.run(installMeet);
    check('Observer: Meet installs too', p.run(drain).installed === true);
    p.window.eval('window.__pcRoster.host = "somewhere.else.example";');
    await sleep(TICK_MS * 3);
    const out = p.run(drain);
    check('Observer: it stops itself once the page has moved on',
          out.installed === false && out.reason === 'page-moved-on', JSON.stringify(out));
  }

  // The observer must never throw, whatever it is pointed at.
  {
    const p = page('<body><iframe src="https://other.example/x"></iframe></body>',
                   { url: 'https://x.example/' });
    let ok = true;
    try { p.run(install); await sleep(TICK_MS * 3); p.run(drain); } catch (e) { ok = false; }
    check('Observer: an empty page with a cross-origin iframe does not throw', ok);
  }

  OPEN.forEach((dom) => { try { dom.window.close(); } catch (e) { /* ignore */ } });
  console.log(failures === 0 ? '\nALL ROSTER JS TESTS PASSED' : `\n${failures} ROSTER JS TEST(S) FAILED`);
  process.exit(failures ? 1 : 0);
})();
