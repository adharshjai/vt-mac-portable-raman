#!/usr/bin/env node
// Smoke test of the Simulation Studio page script (studio/index.html), without a browser.
//
// It loads the page's three script blocks into a bare JavaScript context with a stub of the DOM,
// steps animation frames by hand and presses the controls, in demo mode and in live mode. Live
// mode talks to a stand-in for the Studio server: the endpoints of PROTOCOL.md section 7,
// answered from a second copy of the page's own engine.
//
// What it checks: the script runs through every control without throwing; every id it asks for
// exists in the markup; the SVG and HTML it builds is well formed and holds no NaN or
// "undefined"; estimated text boxes stay inside their drawing and do not overlap; the visible
// counts and states are the expected ones.
// What it cannot check: how the page looks. The stub has no layout, fonts or CSS.
//
//   /usr/local/bin/node studio/test_page.mjs [path/to/index.html]

import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const page = fs.readFileSync(path.resolve(process.argv[2] || path.join(here, 'index.html')), 'utf8');
const block = (id) => page.match(new RegExp('<script id="' + id + '"[^>]*>([\\s\\S]*?)</script>'))[1];
const markup = page.replace(/<script[\s\S]*?<\/script>/g, '').replace(/<style[\s\S]*?<\/style>/g, '');
const markupIds = [...markup.matchAll(/\bid="([^"]+)"/g)].map((match) => match[1]);
const knownIds = new Set(markupIds);
const selectOptions = {};
for (const match of markup.matchAll(/<select id="([^"]+)"[^>]*>([\s\S]*?)<\/select>/g)) {
  selectOptions[match[1]] = [...match[2].matchAll(/<option value="([^"]*)"/g)].map((option) => option[1]);
}

const problems = [];
function expect(condition, message) { if (!condition) problems.push(message); }
expect(markupIds.length === knownIds.size, 'the markup repeats an id: ' + markupIds.filter((id, index) => markupIds.indexOf(id) !== index).join(', '));

// ------------------------------------------------------------------ checks on built markup
const VOID_TAGS = new Set(['input', 'br', 'img', 'meta', 'link', 'hr']);

function checkMarkup(where, html) {
  const odd = html.match(/.{0,50}(NaN|undefined|Infinity|\[object).{0,30}/);
  if (odd) problems.push(where + ': suspicious value in "' + odd[0] + '"');
  const stack = [];
  const tagPattern = /<(\/?)([a-zA-Z][\w:-]*)((?:\s+[\w:-]+(?:="[^"<>]*")?)*)\s*(\/?)>/g;
  let last = 0, match;
  while ((match = tagPattern.exec(html))) {
    if (/[<>]/.test(html.slice(last, match.index))) problems.push(where + ': stray angle bracket near "' + html.slice(last, match.index).slice(0, 50) + '"');
    last = tagPattern.lastIndex;
    const [, closing, tag, , selfClosing] = match;
    if (closing) {
      const open = stack.pop();
      if (open !== tag) problems.push(where + ': </' + tag + '> closes <' + open + '>');
    } else if (!selfClosing && !VOID_TAGS.has(tag)) stack.push(tag);
  }
  if (/[<>]/.test(html.slice(last))) problems.push(where + ': stray angle bracket at the end');
  if (stack.length) problems.push(where + ': unclosed <' + stack.join('>, <') + '>');
}

// Estimated boxes of the text drawn into each SVG. Widths are estimates: Node has no font metrics.
const DRAWINGS = {
  map: { size: [240, 240], groups: ['mapGrid', 'mapAxis', 'mapPlan', 'mapBox', 'mapPts'] },
  zoom: { size: [156, 156], groups: ['zoomAxis', 'zoomPlan', 'zoomPts'] },
  zin: { size: [124, 240], groups: ['zinStatic', 'zinPts'] },
  spec: { size: [300, 176], groups: ['specAxis'] },
  lanes: { size: [706, 176], groups: ['lanesStatic', 'lanesDyn'] },
  chain: { size: [286, 112], groups: ['chain'] }
};

function textBoxes(html) {
  const boxes = [];
  for (const match of html.matchAll(/<text([^>]*)>([\s\S]*?)<\/text>/g)) {
    const attrs = match[1];
    const content = match[2].replace(/<[^>]+>/g, '').replace(/&amp;/g, '&').replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&#39;/g, "'").replace(/&quot;/g, '"');
    const classes = (attrs.match(/class="([^"]*)"/) || [, ''])[1] + ' ' + [...match[2].matchAll(/class="([^"]*)"/g)].map((c) => c[1]).join(' ');
    const size = /t-mono/.test(classes) ? 8.5 : /t-tiny/.test(classes) ? 8 : /t-small/.test(classes) ? 9 : 9.5;
    const width = content.length * size * (/t-mono/.test(classes) ? 0.61 : /t-strong|t-warn/.test(classes) ? 0.56 : 0.52);
    const rotated = attrs.match(/transform="translate\(([-\d.]+) ([-\d.]+)\) rotate\(-90\)"/);
    if (rotated) {
      const cx = Number(rotated[1]), cy = Number(rotated[2]);
      boxes.push({ text: content, x0: cx - size * 0.8, x1: cx + size * 0.25, y0: cy - width / 2, y1: cy + width / 2 });
      continue;
    }
    const x = Number((attrs.match(/\bx="([-\d.]+)"/) || [, 'NaN'])[1]), y = Number((attrs.match(/\by="([-\d.]+)"/) || [, 'NaN'])[1]);
    const anchor = (attrs.match(/text-anchor="([^"]*)"/) || [, 'start'])[1];
    const x0 = anchor === 'middle' ? x - width / 2 : anchor === 'end' ? x - width : x;
    boxes.push({ text: content, x0, x1: x0 + width, y0: y - size * 0.8, y1: y + size * 0.25 });
  }
  return boxes;
}

function checkTextLayout(where) {
  for (const [name, drawing] of Object.entries(DRAWINGS)) {
    const boxes = drawing.groups.flatMap((id) => textBoxes($(id).innerHTML));
    for (const box of boxes) {
      if (box.x0 < -1 || box.y0 < -1 || box.x1 > drawing.size[0] + 1 || box.y1 > drawing.size[1] + 1) {
        problems.push(where + ', ' + name + ': "' + box.text + '" leaves the drawing');
      }
    }
    for (let i = 0; i < boxes.length; i++) {
      for (let j = i + 1; j < boxes.length; j++) {
        const a = boxes[i], b = boxes[j];
        if (Math.min(a.x1, b.x1) - Math.max(a.x0, b.x0) > 1 && Math.min(a.y1, b.y1) - Math.max(a.y0, b.y0) > 1) {
          problems.push(where + ', ' + name + ': "' + a.text + '" overlaps "' + b.text + '"');
        }
      }
    }
  }
}

// ------------------------------------------------------------------ the stub DOM
const elements = new Map();

class StubElement {
  constructor(tag, id) {
    this.tagName = tag; this.id = id || ''; this.attrs = {}; this.children = []; this.parentNode = null; this.listeners = {};
    this.text = ''; this.html = ''; this.currentValue = ''; this.options = null;
    this.checked = false; this.disabled = false; this.open = false; this.className = ''; this.title = ''; this.placeholder = '';
    this.scrollTop = 0; this.clientHeight = 180; this.width = 0; this.height = 0;
  }
  get scrollHeight() { return this.children.length * 15; }
  get value() { return this.currentValue; }
  set value(next) {
    const allowed = this.options || selectOptions[this.id];
    // A select keeps its value when it has no option for the new one, as in a browser.
    if (allowed && allowed.length && !allowed.includes(String(next))) return;
    this.currentValue = String(next);
  }
  setAttribute(name, value) {
    const text = String(value);
    if (/NaN|undefined|Infinity/.test(text)) problems.push('attribute ' + name + ' of ' + (this.id || this.tagName) + ' is "' + text.slice(0, 60) + '"');
    this.attrs[name] = text;
  }
  getAttribute(name) { return name in this.attrs ? this.attrs[name] : null; }
  removeAttribute(name) { delete this.attrs[name]; }
  appendChild(child) { child.parentNode = this; this.children.push(child); return child; }
  removeChild(child) {
    const index = this.children.indexOf(child);
    if (index < 0) throw new Error('removeChild: not a child');
    this.children.splice(index, 1);
    child.parentNode = null;
    return child;
  }
  get firstChild() { return this.children[0] || null; }
  contains(other) { for (let el = other; el; el = el.parentNode) if (el === this) return true; return false; }
  addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
  dispatch(type, extra) {
    const event = Object.assign({ type, target: this, key: '', shiftKey: false, clientX: 0, preventDefault() {} }, extra || {});
    (this.listeners[type] || []).forEach((fn) => fn(event));
  }
  get textContent() { return this.text; }
  set textContent(value) {
    this.text = String(value); this.children = []; this.html = '';
    if (/NaN|undefined/.test(this.text)) problems.push('text of ' + (this.id || this.tagName) + ' is "' + this.text.slice(0, 80) + '"');
  }
  get innerHTML() { return this.html; }
  set innerHTML(value) {
    this.html = String(value); this.children = []; this.text = '';
    checkMarkup('markup built for #' + this.id, this.html);
    for (const match of this.html.matchAll(/\bid="([^"]+)"/g)) elements.set(match[1], new StubElement('x', match[1]));
    if (this.tagName === 'select') this.options = [...this.html.matchAll(/<option value="([^"]*)"/g)].map((option) => option[1]);
  }
  getContext() { return { createImageData: (w, h) => ({ data: new Uint8ClampedArray(w * h * 4) }), putImageData() {} }; }
  toDataURL() { return 'data:image/png;base64,AAAA'; }
  getBoundingClientRect() { return { left: 0, top: 0, width: 300, height: 176 }; }
  focus() { document.activeElement = this; }
}

const document = {
  activeElement: null, listeners: {},
  getElementById(id) {
    if (elements.has(id)) return elements.get(id);
    // An id the markup does not hold: the page script would fail on it in a browser too.
    if (!knownIds.has(id)) return null;
    const el = new StubElement(selectOptions[id] || id === 'relabel' ? 'select' : 'x', id);
    elements.set(id, el);
    return el;
  },
  createElement: (tag) => new StubElement(tag),
  createElementNS: (namespace, tag) => new StubElement(tag),
  createTextNode: (text) => ({ nodeType: 3, textContent: String(text), parentNode: null }),
  addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); },
  dispatch(type, extra) {
    (this.listeners[type] || []).forEach((fn) => fn(Object.assign({ type, target: new StubElement('x'), key: '', preventDefault() {} }, extra || {})));
  }
};

// A window whose frames and timers only run when the test steps them.
function makeWindow(options) {
  const frames = [], timers = [];
  let now = 0, timerSeq = 0;
  const store = {};
  const window = {
    document, console, Date, Math, JSON, Object, Array, Map, Set, String, Number, Promise, Error, isFinite, parseInt, parseFloat, encodeURIComponent,
    Uint8Array, Uint32Array, DataView, Uint8ClampedArray,
    matchMedia: () => ({ matches: !!options.reducedMotion, addEventListener() {} }),
    requestAnimationFrame: (fn) => { frames.push(fn); return frames.length; },
    setTimeout: (fn, delay) => { const id = ++timerSeq; timers.push({ id, at: now + (delay || 0), fn }); return id; },
    clearTimeout: (id) => { const index = timers.findIndex((timer) => timer.id === id); if (index >= 0) timers.splice(index, 1); },
    location: options.location || { protocol: 'file:', hostname: '' },
    localStorage: options.storageBlocked
      ? { getItem() { throw new Error('storage blocked'); }, setItem() { throw new Error('storage blocked'); } }
      : { getItem: (key) => (key in store ? store[key] : null), setItem: (key, value) => { store[key] = String(value); } },
    AbortController: class { constructor() { this.signal = {}; } abort() {} }
  };
  if (options.fetch) window.fetch = options.fetch;
  window.window = window;
  window.globalThis = window;
  const settle = () => new Promise((resolve) => setImmediate(resolve));
  async function step(count, ms) {
    for (let i = 0; i < (count || 1); i++) {
      now += ms || 16;
      for (const timer of timers.filter((entry) => entry.at <= now).sort((a, b) => a.at - b.at)) {
        const index = timers.indexOf(timer);
        if (index >= 0) { timers.splice(index, 1); timer.fn(); }
      }
      await settle();
      for (const fn of frames.splice(0)) fn(now);
      await settle();
    }
  }
  const context = vm.createContext(window);
  for (const id of ['studio-data', 'studio-engine', 'studio-ui']) vm.runInContext(block(id), context, { filename: id });
  return { step };
}

function freshPage() { elements.clear(); document.listeners = {}; document.activeElement = null; }
const $ = (id) => { const el = document.getElementById(id); if (!el) throw new Error('the test asks for an id the page does not have: ' + id); return el; };
const text = (id) => $(id).textContent;
const built = (id) => $(id).innerHTML;
function click(id) { if ($(id).disabled) throw new Error('the test wants to press ' + id + ', but it is disabled'); $(id).dispatch('click'); }
function choose(id, value) { $(id).value = value; $(id).dispatch('change'); }
function toggle(id, on) { $(id).checked = on; $(id).dispatch('change'); }
function type(id, value) { $(id).value = value; $(id).dispatch('input'); }
function marks(id) { return built(id).split('data-cid').length - 1; }
async function until(win, test, frames, what) {
  for (let i = 0; i < frames; i++) { if (test()) return; await win.step(1); }
  problems.push('timed out waiting for ' + what + ' (state line: ' + text('roState') + ')');
}

// ------------------------------------------------------------------ demo mode
async function demoRun(label, options) {
  freshPage();
  const win = makeWindow(options);
  await win.step(3);

  // at rest: the example data
  expect(text('modeBadge') === 'DEMO', label + ': the badge says DEMO');
  expect(/Nothing leaves this page/.test(text('modeNote')), label + ': the demo note says that nothing leaves the page');
  expect(/\/137$/.test(text('selCount')), label + ': 137 example spectra, shown ' + text('selCount'));
  expect(/Needs review/.test(text('statePill')), label + ': opens on a spectrum that needs review, pill says ' + text('statePill'));
  expect(!$('btnConfirm').disabled && !$('btnRelabel').disabled && !$('btnReject').disabled, label + ': review buttons ready at rest');
  // 120 raster points and the 1D point at X = 2 mm, which also lies inside the inset
  expect(marks('zoomPts') === 121, label + ': 121 marks in the inset, drawn ' + marks('zoomPts'));
  expect(marks('mapPts') === 14, label + ': 13 marks of the 1D scan and one for the focus sweep on the map, drawn ' + marks('mapPts'));
  expect($('lanesTok').children.length === 137, label + ': one square per spectrum');
  expect($('specRaw').getAttribute('d').startsWith('M'), label + ': a spectrum is drawn');
  expect(built('chain').includes('node lit'), label + ': the chain of a classified spectrum is lit');
  expect(Number(text('queueCount')) > 0, label + ': the example leaves reviews to do');
  expect(/1 Capture: 137 .*2 Register: 137 .*3 Classify: 137 .*4 Link: 137/.test(text('steps')), label + ': step counters, shown ' + text('steps'));
  expect(/stand-in classifier/.test(built('lanesDyn')), label + ': the lanes name the stand-in classifier');
  expect(/Not run yet/.test(text('honest')) && /ARC cluster/.test(text('honest')) && /DataFed/.test(text('honest')), label + ': the footnote says what has not been run');
  expect(/<b>mot<\/b> motor/.test(built('tickerKey')), label + ': the key of the demo ticker');
  expect(/leaves the laser on; the demo run then switches it off/.test($('btnStop').title), label + ': Stop says what it does to the laser, shown "' + $('btnStop').title + '"');
  expect(/class="term" title="INTERSECT: /.test(built('ly-plan')) && /class="term" title="Bluesky: /.test(built('tx-acquire')) &&
    /class="term" title="Carta: /.test(built('tx-record')) && /<title>Endpoint: /.test(built('lanesDyn')) && /^Endpoint: /.test($('endpointTerm').title),
    label + ': INTERSECT, Bluesky, Carta and endpoint are explained in a tool tip');
  expect(/DataFed keeps a searchable record/.test(built('tx-record')) && /review queue is this project’s, kept in DataFed/.test(built('tx-decide')), label + ': each step says who provides what');
  checkTextLayout(label + ' at rest');

  // selecting through the drawings and the buttons; reading the spectrum
  const cid = built('zoomPts').match(/data-cid="([^"]+)"/)[1];
  const hit = new StubElement('circle');
  hit.parentNode = new StubElement('g');
  hit.parentNode.setAttribute('data-cid', cid);
  $('zoom').dispatch('click', { target: hit });
  await win.step(2);
  expect(built('zoomPts').includes('pt-sel'), label + ': the selected point is ringed');
  expect(/selected/.test(text('specSub')), label + ': the chart follows the selection');
  click('btnNext'); click('btnPrev'); await win.step(2);
  click('btnLatest'); await win.step(2);
  expect(/latest/.test(text('specSub')), label + ': Latest follows the newest spectrum again');
  $('spec').dispatch('pointermove', { clientX: 150 }); await win.step(1);
  expect(/cm/.test(text('specRead')), label + ': a reading under the pointer, shown "' + text('specRead') + '"');
  $('spec').focus();
  $('spec').dispatch('keydown', { key: 'ArrowRight' });
  $('spec').dispatch('keydown', { key: 'ArrowLeft', shiftKey: true });
  $('spec').dispatch('blur');
  document.activeElement = null;

  // find, ground truth, the "without" view
  type('find', 'needs_review'); await win.step(2);
  expect(Number(text('findCount')) === Number(text('queueCount')), label + ': Find counts the review queue');
  type('find', '');
  toggle('truth', true); await win.step(2);
  expect(/\d+ of 137 agree/.test(text('status')), label + ': the status line counts agreement with the ground truth, shown "' + text('status') + '"');
  expect(built('mapPts').includes('<path d="M'), label + ': split marks with ground truth on');
  expect(built('facts').includes('Truth'), label + ': the record names the truth');
  checkTextLayout(label + ' with ground truth');
  toggle('without', true); await win.step(2);
  expect(built('facts').includes('class="lost"') && /no catalog to search/.test(built('facts')), label + ': the "without" view shows what the record loses');
  expect(/Copy by hand/.test(text('steps')) && /No links/.test(text('steps')) && /No queue/.test(text('steps')), label + ': the "without" view renames the steps');
  expect($('find').disabled && $('btnConfirm').disabled, label + ': no search and no review without a catalog');
  expect(/0 record ids/.test(text('ev-record')) && /0 task ids/.test(text('ev-transfer')), label + ': the loop shows what is missing');
  expect(/In LIVE the local demo catalog gives the same on this Mac alone/.test(text('honest')), label + ': the "without" view says what the local demo catalog already gives');
  expect(!built('chain').includes('node lit'), label + ': no chain to light without the records');
  checkTextLayout(label + ' without DataFed and Globus');
  toggle('without', false); toggle('truth', false); await win.step(2);

  // review: confirm, relabel, reject
  const queueBefore = Number(text('queueCount'));
  click('btnQueue'); await win.step(2);
  click('btnConfirm'); await win.step(2);
  choose('relabel', 'adenine'); click('btnRelabel'); await win.step(2);
  click('btnReject'); await win.step(2);
  expect(Number(text('queueCount')) === queueBefore - 3, label + ': three reviews leave the queue');
  expect(/done 3/.test(built('lanesDyn')), label + ': three spectra reach the reviewer lane');

  // threshold
  type('threshold', '0.95'); await win.step(2);
  expect(Number(text('queueCount')) > queueBefore - 3, label + ': a higher threshold sends more to review');
  type('threshold', '0.8'); await win.step(2);
  expect(Number(text('queueCount')) === queueBefore - 3, label + ': back at 0.80 the queue is as before');

  // a 1D scan with pause, step and resume; the laptop goes offline while it runs
  choose('speed', '32');
  choose('power', 'MEDIUM');
  click('btnPlay'); await win.step(3);
  expect(/Demo run started/.test(text('status')), label + ': Play starts a run');
  expect($('scanType').disabled, label + ': the scan type is locked while a run is on');
  await until(win, () => /point [1-9]/.test(text('roState')), 2000, 'the first point');
  click('btnPause'); await win.step(1);
  await until(win, () => /held: paused/.test(text('roState')), 2000, 'the held state');
  expect(text('playText') === 'Resume', label + ': Play offers Resume while paused');
  click('btnStep'); await win.step(1);
  await until(win, () => /held: paused/.test(text('roState')) && !$('btnStep').disabled, 3000, 'the hold after one step');
  click('btnPlay'); await win.step(1);
  toggle('offline', true); await win.step(5);
  await until(win, () => /run complete/.test(text('roState')), 6000, 'the end of the 1D scan');
  await win.step(40);
  expect(Number((built('lanesDyn').match(/waiting list (\d+)/) || [0, 0])[1]) > 0, label + ': captures wait while the laptop is offline');
  expect(built('lanesDyn').includes('barrier') && /OFFLINE/.test(built('lanesDyn')), label + ': the offline barrier is drawn');
  checkTextLayout(label + ' offline');
  toggle('offline', false); await win.step(80);
  expect(/waiting list 0</.test(built('lanesDyn')), label + ': the waiting list empties after reconnect');
  click('btnSync'); await win.step(2);
  click('btnClassify'); await win.step(80);
  expect(/\/150$/.test(text('selCount')), label + ': 150 spectra after the second scan');
  expect(Number(text('clsCount')) === 0, label + ': nothing is left waiting for the classifier');

  // a 2D scan stopped mid-way, a single capture, a refused start
  choose('scanType', '2d');
  click('btnPlay'); await win.step(3);
  await until(win, () => /point 6\//.test(text('roState')), 6000, 'the sixth point of the 2D scan');
  checkTextLayout(label + ' during the 2D scan');
  click('btnStop'); await win.step(3);
  await until(win, () => /run stopped/.test(text('roState')), 3000, 'the stopped run');
  expect(/laser off/.test(text('roState')), label + ': the stopped run ends with the laser off');
  choose('scanType', 'single');
  click('btnPlay');
  await until(win, () => /run complete/.test(text('roState')), 3000, 'the single capture');
  await win.step(10);
  expect(built('mapPts').includes('pt-single'), label + ': the single capture is drawn as "position not recorded"');
  $('operator').value = ''; $('operator').dispatch('change');
  click('btnPlay'); await win.step(2);
  expect(/required/.test(text('status')) && $('runDetails').open, label + ': a run without an operator is refused and the details open');
  $('operator').value = 'someone'; $('operator').dispatch('change');
  document.dispatch('keydown', { key: 'Escape' });
  expect(!$('runDetails').open, label + ': Escape closes the details');

  // reset, then a whole 2D scan captured offline: more than the waiting list can draw
  click('btnReset'); await win.step(3);
  expect(/^0\/0$/.test(text('selCount')) && $('lanesTok').children.length === 0, label + ': Reset empties the demo');
  expect(built('facts').includes('No spectrum yet'), label + ': the empty record view says what to do');
  checkTextLayout(label + ' after reset');
  toggle('offline', true);
  choose('scanType', '2d');
  click('btnPlay');
  await until(win, () => /run complete/.test(text('roState')), 20000, 'the offline 2D scan');
  await win.step(90);
  expect(/waiting list 124</.test(built('lanesDyn')) && />\+71</.test(built('lanesDyn')), label + ': 124 captures wait, 71 of them behind the "+71" label');
  expect($('lanesTok').children.filter((el) => el.getAttribute('visibility') === 'hidden').length === 71, label + ': the squares behind the label are hidden');
  checkTextLayout(label + ' with a full waiting list');
  toggle('offline', false); await win.step(120);
  expect(/to classify 124</.test(built('lanesDyn')), label + ': all 124 are stored after reconnect');
  checkTextLayout(label + ' with a full classifier queue');
  click('btnClassify'); await win.step(8);
  checkTextLayout(label + ' while the job runs');
  await win.step(120);
  expect(/accepted \d+</.test(built('lanesDyn')) && Number(text('clsCount')) === 0, label + ': the job empties the classifier queue');
  checkTextLayout(label + ' after the job');
  document.dispatch('click', {});
}

// ------------------------------------------------------------------ live mode
// A stand-in for `python -m raman_datafed studio`. It exists only to drive the page's live code.
// example: start from the demo's two finished scans instead of an empty folder.
function standInServer(options) {
  const context = vm.createContext({});
  vm.runInContext(block('studio-data'), context);
  vm.runInContext(block('studio-engine'), context);
  const engine = context.StudioEngine.createDemo(context.STUDIO_EMBED, { seed: 5 });
  if (options && options.example) engine.runExample();
  // `run` is the run of this server as studio_server.py reports it; `lockHeld` makes the next run
  // end at once with exit code 3, as raman_sim_runner does while the controller window is open.
  // `hold`: endpoints whose answers wait for release(); `provenanceDown`: provenance answers as
  // studio_server.py does while the catalog is offline (503); `realSession`: the newest session was
  // recorded with real instruments; `extraTotal`: captures beyond the newest ones the list holds.
  const state = { down: false, calls: [], run: null, lockHeld: false, hold: new Set(), waiting: [], provenanceDown: false, realSession: false, extraTotal: 0 };
  const plain = (value) => JSON.parse(JSON.stringify(value));
  function answer(url, body) {
    switch (url.pathname) {
      case '/api/ping': return { ok: true, home: '/tmp/home', mode: 'local-demo', simulated_stack_available: true };
      case '/api/sample': return Object.assign(plain(context.STUDIO_EMBED.sample), { reference_spectra: { dark: [], signals: {} } });
      case '/api/scan': {
        const scan = plain(engine.scan());
        delete scan.demo;
        const run = state.run;
        if (run && run.running && !engine.runActive()) Object.assign(run, { running: false, exit_code: run.stop_requested ? 143 : 0 });
        scan.run = run && plain(run);
        scan.active = scan.active || !!(run && run.running);
        if (run && run.exit_code === 3) scan.log_tail.push('[runner] Not started: /tmp/home/controller.lock is held; the controller window or another simulated run is open in this folder');
        if (state.realSession && scan.session) scan.session.simulated = { backend: false, motor: false };
        return scan;
      }
      case '/api/pipeline': {
        const view = plain(engine.pipeline());
        delete view.demo;
        Object.assign(view.status, { mode: 'local-demo', mode_label: 'Local demo catalog (files on this Mac only; nothing is sent anywhere)' });
        const newest = engine.scan().session;
        view.items.concat(view.review).forEach((item) => {
          // The real list marks a review recorded offline on the review itself (PROTOCOL.md section 6).
          if (item.demo && item.demo.queued_review) item.review = Object.assign({}, item.review, { queued: true });
          delete item.demo; item.point_file = 'point_' + String(item.point_id).padStart(4, '0') + '.json';
          if (state.realSession && newest && item.session_id === newest.session_id) item.simulated = false;
        });
        view.total = view.items.length + state.extraTotal;
        return view;
      }
      case '/api/provenance':
        return state.provenanceDown ? { ok: false, error: 'The catalog cannot be reached', detail: 'stand-in', kind: 'offline' }
          : plain(engine.provenance(url.searchParams.get('record')));
      case '/api/sync': return Object.assign({ ok: true }, plain(engine.sync()));
      case '/api/classify': {
        // As in the real job, a spectrum whose upload has not finished stays in the queue as skipped.
        const skipped = engine.pipeline().items.filter((item) => item.state === 'registered' || item.state === 'uploading').length;
        const started = plain(engine.classify());
        engine.advance(9000);
        return started.ok ? Object.assign(started, { predictions: started.processed, skipped }) : started;
      }
      case '/api/review': return plain(engine.review(body.prediction, body.decision, body.label, body.reviewer, body.comment));
      case '/api/offline': return plain(engine.setOffline(!!body.offline));
      case '/api/run': {
        const run = { pid: 4242 + state.calls.length, mode: body.mode, power: body.power, running: true, exit_code: null,
          started_utc: engine.live().demo.clock_utc, stop_requested: false, simulated: true };
        if (state.lockHeld) {
          state.run = Object.assign(run, { running: false, exit_code: 3 });
          return { ok: true, pid: run.pid };
        }
        engine.setDetails({ scan_mode: body.mode, power: body.power, sample_id: body.sample_id, operator: body.operator, location: body.location });
        const started = plain(engine.start());
        if (started.ok) state.run = run;
        return started.ok ? { ok: true, pid: run.pid } : started;
      }
      case '/api/stop': {
        if (state.run) state.run.stop_requested = true;
        return plain(engine.stop());
      }
      default: return { ok: false, error: 'unknown endpoint ' + url.pathname };
    }
  }
  function fetch(target, options) {
    if (state.down) return Promise.reject(new Error('connection refused'));
    const url = new URL(target, 'http://127.0.0.1:8765/');
    const body = options && options.body ? JSON.parse(options.body) : undefined;
    state.calls.push((body === undefined ? 'GET ' : 'POST ') + url.pathname);
    // Answered when it is given, so a held answer reflects the state at its release.
    const reply = () => {
      const data = answer(url, body);
      return { status: data && data.kind === 'offline' ? 503 : 200, json: () => Promise.resolve(data) };
    };
    if (state.hold.has(url.pathname)) return new Promise((resolve) => state.waiting.push({ path: url.pathname, give: () => resolve(reply()) }));
    return Promise.resolve(reply());
  }
  function release(pathname) {
    state.hold.delete(pathname);
    const due = state.waiting.filter((entry) => entry.path === pathname);
    state.waiting = state.waiting.filter((entry) => entry.path !== pathname);
    due.forEach((entry) => entry.give());
  }
  return { fetch, engine, state, release };
}

async function liveRun() {
  freshPage();
  const server = standInServer();
  const win = makeWindow({ fetch: server.fetch, location: { protocol: 'http:', hostname: '127.0.0.1' } });
  await win.step(10);
  expect(text('modeBadge') === 'LIVE', 'live: the badge says LIVE once /api/ping answers');
  expect(/instruments simulated/.test(text('modeNote')) && /Local demo catalog: nothing is sent anywhere/.test(text('modeNote')), 'live: the note names the simulated instruments and the demo catalog');
  expect($('btnPause').disabled && $('btnStep').disabled && $('btnReset').disabled && $('speed').disabled && $('threshold').disabled, 'live: demo-only controls are disabled');
  expect(/^0\/0$/.test(text('selCount')), 'live: starts with what the server has, shown ' + text('selCount'));
  expect(built('mapPlan') === '', 'live: no plan is drawn, the server sends none');
  const emptyLog = $('ticker').children;
  expect(emptyLog.length === 1 && /^No log here yet\. This box shows the log of runs started with Play/.test(emptyLog[0].children[0].textContent),
    'live: an empty log says why it is empty');
  expect(built('tickerKey') === 'log of the last run started with Play', 'live: the key says what the box shows, without the demo\'s tags');
  expect(/the runner stops the scan, then switches the laser off/.test($('btnStop').title) && /window's Stop leaves the laser on/.test($('btnStop').title),
    'live: Stop says what it does to the laser, shown "' + $('btnStop').title + '"');

  choose('scanType', '1d-x');
  click('btnPlay'); await win.step(5, 130);
  expect(/simulated run started/.test(text('status')), 'live: /api/run started a run');
  expect(/^1d-x · run starting/.test(text('roState')) && built('mapPlan') === '', 'live: before its session exists the run is shown as starting, shown "' + text('roState') + '"');
  // the stand-in server runs on its own clock; move it along with the page
  let guard = 0;
  while (server.engine.runActive() && guard++ < 4000) { server.engine.advance(200); await win.step(2, 130); }
  await win.step(40, 130);
  expect(/^13\/13$/.test(text('selCount')) && marks('mapPts') === 13, 'live: the 13 spectra of the run are shown');
  expect(/simulated run finished/.test(text('status')), 'live: the end of the run is reported, shown "' + text('status') + '"');
  expect($('ticker').children.length > 3, 'live: the log tail is shown');
  expect($('specRaw').getAttribute('d').startsWith('M'), 'live: the newest spectrum is drawn');
  expect(/last: 1d-x, 13 saved/.test(text('roState')), 'live: the state line, shown "' + text('roState') + '"');
  expect(/local demo catalog/.test(built('lanesDyn')), 'live: the catalog lane is labelled as the local demo catalog');
  checkTextLayout('live after the 1D scan');
  server.engine.advance(5000);
  await win.step(40, 130);
  click('btnSync'); await win.step(10, 130);
  expect(/^Sync:/.test(text('status')), 'live: /api/sync answered');
  click('btnClassify'); await win.step(30, 130);
  expect(/^Classifier/.test(text('status')), 'live: /api/classify answered');
  server.engine.advance(3000);
  await win.step(40, 130);
  const queue = Number(text('queueCount'));
  expect(queue > 0, 'live: the review queue is shown');
  click('btnQueue'); await win.step(10, 130);
  expect(built('chain').includes('node lit'), 'live: the chain is lit from /api/provenance');
  click('btnConfirm'); await win.step(3, 130);
  expect(/Enter your name/.test(text('status')), 'live: a review needs a reviewer name');
  $('reviewer').value = 'page test'; $('reviewer').dispatch('change');
  click('btnConfirm'); await win.step(30, 130);
  expect(Number(text('queueCount')) === queue - 1, 'live: the review reached the server');
  toggle('offline', true); await win.step(20, 130);
  expect(built('lanesDyn').includes('barrier'), 'live: /api/offline takes the catalog offline');
  toggle('offline', false); await win.step(20, 130);
  expect(!built('lanesDyn').includes('barrier'), 'live: and online again');

  // the server goes away and comes back
  server.state.down = true;
  await win.step(80, 130);
  expect(/NO ANSWER/.test(text('modeBadge')) && /stopped answering/.test(text('status')), 'live: a silent server is reported');
  expect($('btnPlay').disabled && $('btnSync').disabled && $('btnStop').disabled, 'live: commands are disabled while the server is silent');
  expect(/\/13$/.test(text('selCount')), 'live: the last data stays on screen');
  server.state.down = false;
  await win.step(60, 130);
  expect(text('modeBadge') === 'LIVE' && !$('btnPlay').disabled, 'live: the page recovers when the server answers again');

  // a second run, stopped from the page
  choose('scanType', '2d');
  click('btnPlay'); await win.step(5, 130);
  guard = 0;
  while (!(server.engine.live().active && server.engine.scan().points.length >= 3) && guard++ < 4000) { server.engine.advance(200); await win.step(2, 130); }
  expect(/^2d/.test(text('roState')) && /STEP \d+/.test(text('roState')), 'live: the state line during a scan, shown "' + text('roState') + '"');
  click('btnStop'); await win.step(5, 130);
  server.engine.advance(8000);
  await win.step(30, 130);
  expect(!server.engine.runActive(), 'live: /api/stop stopped the run');
  expect(/stopped on request/.test(text('status')), 'live: the stopped run is reported, shown "' + text('status') + '"');

  // a run that cannot start: the controller window holds the lock of the folder
  server.state.lockHeld = true;
  choose('scanType', 'single');
  click('btnPlay'); await win.step(10, 130);
  expect(/did not start: the controller window or another run holds/.test(text('status')), 'live: a run refused for the lock is reported, shown "' + text('status') + '"');
  expect(!$('btnPlay').disabled, 'live: Play is ready again right after a refused run');
  server.state.lockHeld = false;

  // a capture made offline: one sync after reconnecting starts its upload, and classify skips it until it is done
  toggle('offline', true); await win.step(10, 130);
  click('btnPlay'); await win.step(5, 130);
  guard = 0;
  while (server.engine.runActive() && guard++ < 4000) { server.engine.advance(200); await win.step(2, 130); }
  toggle('offline', false); await win.step(10, 130);
  click('btnSync'); await win.step(10, 130);
  click('btnClassify'); await win.step(30, 130);
  expect(/\. 1 skipped: Sync now finishes their uploads\.$/.test(text('status')), 'live: classify says what finishes a skipped upload, shown "' + text('status') + '"');

  // to the demo and back
  click('btnDemo'); await win.step(5);
  expect(text('modeBadge') === 'DEMO' && /\/137$/.test(text('selCount')), 'the demo view returns with its example data');
  click('btnLive'); await win.step(30, 130);
  expect(text('modeBadge') === 'LIVE', 'and live again');
  const used = new Set(server.state.calls);
  for (const endpoint of ['GET /api/ping', 'GET /api/sample', 'GET /api/scan', 'GET /api/pipeline', 'GET /api/provenance', 'POST /api/run', 'POST /api/stop',
    'POST /api/sync', 'POST /api/classify', 'POST /api/review', 'POST /api/offline']) {
    expect(used.has(endpoint), 'live: ' + endpoint + ' was never called');
  }
}

// Live mode with the demo's two finished scans in the stand-in: a provenance read that fails, a
// real session, a review sent by a later sync, answers that arrive after the switch to the demo,
// and a list the server cut short.
async function liveDetails() {
  freshPage();
  const server = standInServer({ example: true });
  const win = makeWindow({ fetch: server.fetch, location: { protocol: 'http:', hostname: '127.0.0.1' } });
  await win.step(20, 130);
  expect(text('modeBadge') === 'LIVE' && /\/137$/.test(text('selCount')), 'live details: the example data, shown ' + text('selCount'));
  const pick = (cid) => {
    const hit = new StubElement('circle');
    hit.parentNode = new StubElement('g');
    hit.parentNode.setAttribute('data-cid', cid);
    $('map').dispatch('click', { target: hit });
  };

  // A chain read while the catalog is offline says so, and is read again once the catalog answers.
  toggle('offline', true); await win.step(10, 130);
  server.state.hold.add('/api/provenance');
  click('btnQueue'); await win.step(3, 130);
  expect(/loading…/.test(built('chain')) && !/none yet/.test(built('chain')), 'live details: a chain still being read says so, not "none yet"');
  server.state.provenanceDown = true;
  server.release('/api/provenance'); await win.step(3, 130);
  expect(/not read: the catalog is offline/.test(built('chain')) && !/none yet/.test(built('chain')) && !built('chain').includes('node lit'),
    'live details: a chain the offline catalog could not give is not shown as empty');
  server.state.provenanceDown = false;
  await win.step(10, 130);
  expect(/not read/.test(built('chain')), 'live details: and is not asked for again at every frame while the catalog is away');
  toggle('offline', false); await win.step(20, 130);
  expect(built('chain').includes('node lit') && !/not read/.test(built('chain')), 'live details: the catalog is back: the chain is read again');

  // The newest session was recorded with real instruments: the virtual plate and the simulated stage say nothing about it.
  server.state.realSession = true;
  await win.step(20, 130);
  expect(/NOT simulated/.test(text('modeNote')), 'live details: the note says that the newest session was not simulated');
  expect($('mapProbe').children[0].getAttribute('visibility') === 'hidden' && text('roX') === 'X –',
    'live details: no probe and no stage position from the simulated stage file');
  toggle('truth', true); await win.step(3, 130);
  expect(/ 124 not simulated, left out\.$/.test(text('status')), 'live details: agreement leaves the real spectra out, shown "' + text('status') + '"');
  // Halves are drawn only for the simulated 1D point that also lies in the inset.
  expect((built('zoomPts').match(/ A3\.6 3\.6 0 0 [01] /g) || []).length === 2, 'live details: no ground-truth halves on the real raster points');
  pick(server.engine.scan().points[10].capture_id); await win.step(3, 130);
  expect(/none: not known to be a capture of the virtual sample/.test(built('facts')), 'live details: the record of a real spectrum has no truth');
  toggle('truth', false);
  click('btnQueue'); await win.step(3, 130);
  expect(!$('btnLatest').disabled, 'live details: a spectrum is selected');
  // Play follows the new run, as in the demo; its single capture is real here and its stage is not known.
  choose('scanType', 'single');
  click('btnPlay'); await win.step(3, 130);
  expect($('btnLatest').disabled, 'live details: Play lets the selection go');
  let guard = 0;
  while (server.engine.runActive() && guard++ < 4000) { server.engine.advance(200); await win.step(2, 130); }
  await win.step(20, 130);
  expect(/^Point 1, single$/.test(text('specTitle')) && /^latest/.test(text('specSub')), 'live details: the panels show the new run, shown "' + text('specSub') + '"');
  expect(!built('mapPts').includes('pt-single'), 'live details: a real single capture is not drawn where the simulated stage stands');
  server.state.realSession = false;

  // A review recorded offline is sent by a later sync, and the sync result says so.
  toggle('offline', true); await win.step(10, 130);
  $('reviewer').value = 'page test'; $('reviewer').dispatch('change');
  click('btnQueue'); await win.step(5, 130);
  click('btnConfirm'); await win.step(10, 130);
  expect(/recorded locally/.test(text('status')) && text('statePill') === 'Review queued', 'live details: a review recorded offline is queued');
  toggle('offline', false); await win.step(10, 130);
  click('btnSync'); await win.step(10, 130);
  expect(/^Sync: .*1 queued review recorded/.test(text('status')), 'live details: the sync that sends it says so, shown "' + text('status') + '"');

  // Answers that arrive after the switch to the demo stay out of its status line.
  server.state.hold.add('/api/sync');
  click('btnSync'); await win.step(2, 130);
  click('btnDemo'); await win.step(3);
  const greeting = text('status');
  server.release('/api/sync'); await win.step(5);
  expect(text('modeBadge') === 'DEMO' && /^Demo: /.test(greeting) && text('status') === greeting, 'live details: a late sync answer is not shown beside DEMO, shown "' + text('status') + '"');
  click('btnLive'); await win.step(20, 130);
  server.state.hold.add('/api/classify');
  click('btnClassify'); await win.step(2, 130);
  click('btnDemo'); await win.step(3);
  server.release('/api/classify'); await win.step(5);
  expect(text('status') === greeting, 'live details: nor a late classifier answer');
  click('btnLive'); await win.step(20, 130);
  expect(!$('btnClassify').disabled, 'live details: and the classifier button is ready again');

  // The server lists the newest captures only: the page says that its counts are of those.
  server.state.extraTotal = 100;
  await win.step(40, 130);
  const shown = server.engine.pipeline().items.length;
  expect($('event').title.startsWith('newest ' + shown + ' of ' + (shown + 100) + ' captures shown') && text('steps').startsWith('1 Capture: ' + shown + '+'),
    'live details: a list cut short is said to be, shown "' + $('event').title + '" and "' + text('steps') + '"');
  checkTextLayout('live details at the end');
}

await demoRun('demo', {});
await demoRun('demo with reduced motion and blocked storage', { reducedMotion: true, storageBlocked: true });
await liveRun();
await liveDetails();

const unique = [...new Set(problems)];
if (unique.length) {
  console.log(unique.length + ' problem(s):');
  unique.slice(0, 80).forEach((problem) => console.log(' - ' + problem));
  process.exit(1);
}
console.log('page script: demo, demo with reduced motion and blocked storage, live against a stand-in server, and live details ran clean');
