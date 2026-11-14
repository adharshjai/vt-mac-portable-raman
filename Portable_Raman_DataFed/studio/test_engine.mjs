#!/usr/bin/env node
// Checks the engine of the Simulation Studio (studio/index.html).
//
// The page keeps its embedded data and its engine in two script blocks. This test reads both out
// of the page and runs them in a bare JavaScript context: no window, no document, no timers, no
// network. An engine that touched the page would fail here.
//
//   /usr/local/bin/node studio/test_engine.mjs [path/to/index.html]
//
// Where the motor program is next to the page (macos-native/motor), the sequences are also
// compared with its source, so that a change there shows up here.

import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const pagePath = path.resolve(process.argv[2] || path.join(here, 'index.html'));
const page = fs.readFileSync(pagePath, 'utf8');

function block(id) {
  const match = page.match(new RegExp('<script id="' + id + '"[^>]*>([\\s\\S]*?)</script>'));
  if (!match) throw new Error('the page has no <script id="' + id + '"> block');
  return match[1];
}

const dataSource = block('studio-data');
const engineSource = block('studio-engine');
const sandbox = vm.createContext({});
vm.runInContext(dataSource, sandbox, { filename: 'studio-data' });
vm.runInContext(engineSource, sandbox, { filename: 'studio-engine' });
const E = sandbox.StudioEngine;
const EMBED = sandbox.STUDIO_EMBED;

// Values made inside the sandbox belong to another realm; compare plain copies.
const plain = (value) => JSON.parse(JSON.stringify(value));
const sha = (text) => crypto.createHash('sha256').update(text, 'utf8').digest('hex');

let passed = 0, skipped = 0;
const failures = [];
function test(name, fn) {
  try {
    const note = fn();
    if (typeof note === 'string' && note.startsWith('skip')) { skipped++; console.log('skip  ' + name + ' (' + note.slice(5) + ')'); return; }
    passed++;
    console.log('ok    ' + name);
  } catch (error) {
    failures.push(name);
    console.log('FAIL  ' + name + '\n      ' + String(error && error.message ? error.message : error).split('\n').join('\n      '));
  }
}
function check(condition, message) { if (!condition) throw new Error(message); }
function equal(actual, expected, message) {
  const a = JSON.stringify(actual), b = JSON.stringify(expected);
  if (a !== b) throw new Error((message || 'values differ') + '\n  actual:   ' + a.slice(0, 400) + '\n  expected: ' + b.slice(0, 400));
}
function near(actual, expected, tolerance, message) {
  if (!(Math.abs(actual - expected) <= tolerance)) throw new Error((message || 'not close') + ': ' + actual + ' vs ' + expected);
}

const STEPS = 34700;
const START = EMBED.sample.start_position_mm;
const newDemo = (seed) => E.createDemo(EMBED, { seed: seed === undefined ? 7 : seed });

// Advance in small slices and keep every ticker line (the engine itself keeps only the latest).
function advanceCollect(demo, ms, lines, watch) {
  let last = lines.length ? lines[lines.length - 1].seq : 0;
  for (let spent = 0; spent < ms; spent += 100) {
    demo.advance(100);
    for (const line of demo.ticker(last)) { lines.push(line); last = line.seq; }
    if (watch) watch(demo.live());
  }
}
function runCollect(demo, watch) {
  const lines = [];
  for (const line of demo.ticker(0)) lines.push(line);
  let guard = 0;
  while (demo.runActive() && guard++ < 100000) advanceCollect(demo, 100, lines, watch);
  check(!demo.runActive(), 'the run did not end');
  return lines;
}
function runScan(mode, seed, extra) {
  const demo = newDemo(seed);
  demo.setDetails(Object.assign({ scan_mode: mode }, extra || {}));
  const started = demo.start();
  check(started.ok, 'start failed: ' + JSON.stringify(started));
  const lines = runCollect(demo);
  return { demo, lines, scan: plain(demo.scan()) };
}
const texts = (lines, who) => lines.filter((line) => !who || line.who === who).map((line) => line.text);
const count = (lines, pattern) => lines.filter((line) => pattern.test(line.text)).length;
const states = (demo) => plain(demo.pipeline()).status.counts;
function settle(demo, syncs) { demo.advance((syncs || 4) * E.constants.SYNC_PERIOD_MS); }
function classifyAll(demo) {
  const result = plain(demo.classify());
  demo.advance(E.constants.SYNC_PERIOD_MS * 8);
  return result;
}

// ---------------------------------------------------------------------------- embedded data
test('embedded data: sample and 256-value reference curves', () => {
  equal(EMBED.schema, 'raman-studio-embed/1');
  equal(EMBED.sample.schema, 'raman-virtual-sample/1');
  const materials = Object.keys(EMBED.sample.materials);
  check(materials.length >= 2, 'the sample names fewer than two materials');
  check(materials.includes(EMBED.sample.background), 'the background material is not listed');
  equal(Object.keys(EMBED.reference.signals).sort(), materials.slice().sort(), 'one signal curve per material');
  for (const [name, curve] of [['dark', EMBED.reference.dark], ['raman_shift', EMBED.reference.raman_shift],
    ...materials.map((material) => [material, EMBED.reference.signals[material]])]) {
    check(Array.isArray(plain(curve)) && curve.length === 256, name + ' does not hold 256 values');
    check(plain(curve).every((value) => Number.isFinite(value)), name + ' holds a value that is not a number');
  }
  const shift = plain(EMBED.reference.raman_shift);
  check(shift.every((value, index) => index === 0 || value > shift[index - 1]), 'the Raman shift axis does not increase');
  check(typeof EMBED.placeholder === 'boolean', 'placeholder flag missing');
});

test('engine block does not reach for the page', () => {
  const forbidden = /\b(document|window|navigator|localStorage|sessionStorage|requestAnimationFrame|fetch|XMLHttpRequest|setTimeout|setInterval|Math\.random|Date\.now)\b/;
  const hit = engineSource.match(forbidden);
  check(!hit, 'the engine block mentions ' + (hit && hit[0]));
  check(E && typeof E.createDemo === 'function', 'StudioEngine.createDemo is missing');
});

// ---------------------------------------------------------------------------- hashes, aliases
test('SHA-256 matches Node for short, block-edge and non-ASCII text', () => {
  // Two-, three- and four-byte UTF-8 sequences: e acute, a check mark, a letter outside the BMP.
  const nonAscii = 'h' + String.fromCodePoint(0xe9) + 'llo ' + String.fromCodePoint(0x2713, 0x10348);
  for (const text of ['', 'abc', 'a'.repeat(55), 'a'.repeat(56), 'a'.repeat(63), 'a'.repeat(64), 'a'.repeat(119), 'a'.repeat(1000), nonAscii]) {
    equal(E.sha256Hex(text), sha(text), 'digest of ' + JSON.stringify(text).slice(0, 24));
  }
});

test('aliases follow PROTOCOL.md 5.2', () => {
  const capture = '3f2a9c1e-0000-4000-8000-123456789abc_17';
  const model = sha('model');
  equal(E.alias.raw(capture), 'rs-' + sha(capture).slice(0, 12));
  equal(E.alias.session('abc'), 'rsess-' + sha('abc').slice(0, 12));
  equal(E.alias.dark(model), 'rdark-' + sha(model).slice(0, 12));
  equal(E.alias.model(model), 'rmodel-' + model.slice(0, 12));
  equal(E.alias.prediction(capture, model), 'rp-' + sha(capture + ':' + model).slice(0, 12));
  for (const value of [E.alias.raw(capture), E.alias.session('abc'), E.alias.dark(model), E.alias.model(model), E.alias.prediction(capture, model)]) {
    check(/^[a-z0-9._-]{1,40}$/.test(value), 'alias ' + value + ' breaks the alias rule');
  }
});

// ---------------------------------------------------------------------------- virtual sample
test('region weights equal the formula of PROTOCOL.md section 4', () => {
  const sample = plain(EMBED.sample);
  const clamp = (value) => Math.min(1, Math.max(0, value));
  function expected(x, y) {
    const weights = {};
    for (const region of sample.regions) {
      const b = sample.edge_blend_mm[region.shape];
      let value;
      if (region.shape === 'frame') {
        const d = Math.max(x, y);
        value = clamp((d - region.inner_mm + b / 2) / b) * clamp((region.outer_mm + b / 2 - d) / b);
      } else {
        const r = Math.hypot(x - region.center_mm[0], y - region.center_mm[1]);
        value = clamp((region.radius_mm + b / 2 - r) / b);
      }
      weights[region.material] = Math.max(weights[region.material] || 0, value);
    }
    let sum = Object.values(weights).reduce((a, b) => a + b, 0);
    if (sum > 1) { for (const key of Object.keys(weights)) weights[key] /= sum; sum = 1; }
    weights[sample.background] = (weights[sample.background] || 0) + 1 - sum;
    return weights;
  }
  const spots = [];
  for (let x = 0; x <= 25; x += 0.37) for (let y = 0; y <= 25; y += 0.41) spots.push([x, y]);
  for (let x = 1.9; x <= 3.1; x += 0.013) for (let y = 1.9; y <= 3.1; y += 0.017) spots.push([x, y]);
  for (const [x, y] of spots) {
    const got = plain(E.weightsAt(EMBED.sample, x, y)), want = expected(x, y);
    let total = 0;
    for (const material of Object.keys(sample.materials)) {
      near(got[material] || 0, want[material] || 0, 1e-9, 'weight of ' + material + ' at ' + x + ', ' + y);
      check((got[material] || 0) >= -1e-12, 'negative weight');
      total += got[material] || 0;
    }
    near(total, 1, 1e-9, 'weights at ' + x + ', ' + y + ' do not add up to 1');
  }
});

test('focus factor and spectrum formula', () => {
  const focus = EMBED.sample.focus;
  near(E.focusAt(EMBED.sample, focus.z_focus_mm), 1, 1e-12);
  near(E.focusAt(EMBED.sample, focus.z_focus_mm + focus.half_width_mm), 0.5, 1e-12);
  near(E.focusAt(EMBED.sample, focus.z_focus_mm - focus.half_width_mm), 0.5, 1e-12);
  const dark = plain(EMBED.reference.dark);
  equal(plain(E.synthSpectrum(EMBED, {}, 0, null)), dark.map((value) => Math.min(65534, Math.max(0, Math.round(value)))), 'a capture without laser signal is the dark');
  const materials = Object.keys(EMBED.sample.materials);
  const weights = {}; weights[materials[0]] = 0.25; weights[materials[1]] = 0.75;
  const got = plain(E.synthSpectrum(EMBED, weights, 0.5, null));
  for (const index of [0, 1, 17, 128, 255]) {
    const mix = 0.25 * EMBED.reference.signals[materials[0]][index] + 0.75 * EMBED.reference.signals[materials[1]][index];
    equal(got[index], Math.min(65534, Math.max(0, Math.round(dark[index] + 0.5 * mix))), 'value ' + index);
  }
  check(got.every((value) => value >= 0 && value <= 65534), 'a value left 0..65534');
});

test('outcome table of PROTOCOL.md 5.3', () => {
  const background = EMBED.sample.background;
  const material = Object.keys(EMBED.sample.materials).find((name) => name !== background);
  equal(plain(E.outcomeFor({ confidence: 0.93, top_class: material }, 0.8, background)), { outcome: 'confident', label: material, needs_review: false });
  equal(plain(E.outcomeFor({ confidence: 0.93, top_class: background }, 0.8, background)), { outcome: 'background', label: 'UNKNOWN', needs_review: false });
  equal(plain(E.outcomeFor({ confidence: 0.61, top_class: material }, 0.8, background)), { outcome: 'low_confidence', label: 'UNKNOWN', needs_review: true });
  equal(plain(E.outcomeFor({ confidence: 0.8, top_class: material }, 0.8, background)).outcome, 'confident', 'a score equal to the threshold passes');
  equal(plain(E.outcomeFor({ rejected: true, confidence: 0, top_class: null }, 0.8, background)), { outcome: 'rejected', label: 'REJECTED', needs_review: true });
});

// ---------------------------------------------------------------------------- sequences
test('1D positions: 0 to 24 mm every 2 mm, 13 targets', () => {
  equal(plain(E.lineScanPositions()), Array.from({ length: 13 }, (_, index) => index * 2 * STEPS));
});

for (const [mode, axis, other] of [['1d-x', 'x', 'y'], ['1d-y', 'y', 'x']]) {
  test(mode + ': 13 spectra at 0, 2, ... 24 mm, only ' + axis.toUpperCase() + ' homed, other axes untouched', () => {
    const { demo, lines, scan } = runScan(mode);
    equal(scan.points.length, 13, 'number of points');
    equal(scan.points.map((point) => point.point_id), Array.from({ length: 13 }, (_, index) => index + 1));
    scan.points.forEach((point, index) => {
      near(point[axis + '_mm'], index * 2, 1e-9, 'position of point ' + (index + 1));
      near(point[other + '_mm'], START[other], 1e-9, 'the other axis moved');
      near(point.z_mm, START.z, 1e-9, 'Z moved');
    });
    equal(count(lines, new RegExp('^Homing ' + axis.toUpperCase() + '\\.\\.\\.$')), 1, 'the scanned axis is homed once');
    equal(count(lines, /^Homing /), 1, 'no other axis is homed');
    equal(count(lines, /^request\.txt ← STEP /), 13, 'STEP requests');
    equal(count(lines, /^\[Point \d+\] saved, advance/), 13, 'acknowledged points');
    const steps = texts(lines).filter((text) => text.startsWith('request.txt')).map((text) => text.split(' ').slice(-4).map(Number));
    steps.forEach(([id, x, y, z], index) => {
      equal(id, index + 1);
      equal(axis === 'x' ? x : y, index * 2 * STEPS, 'steps of the scanned axis');
      equal(axis === 'x' ? y : x, Math.round(START[other] * STEPS), 'steps of the other axis');
      equal(z, Math.round(START.z * STEPS), 'steps of Z');
    });
    const end = plain(demo.live()).stage;
    near(end[axis + '_mm'], 24, 1e-9); near(end[other + '_mm'], START[other], 1e-9); near(end.z_mm, START.z, 1e-9);
    equal(scan.session.scan_mode, mode);
    equal(plain(demo.live()).demo.run.state, 'complete');
    equal(plain(demo.live()).demo.laser.on, false, 'the run ends with the laser off');
  });
}

let twoD = null;
test('2D: 4 focus-sweep spectra, then 120 raster spectra, 124 in all', () => {
  let seenZ = 0, targetZ = 0;
  const demo = newDemo();
  demo.setDetails({ scan_mode: '2d' });
  check(demo.start().ok, 'start failed');
  const lines = runCollect(demo, (live) => {
    if (live.stage.z_mm > seenZ) seenZ = live.stage.z_mm;
    if (live.demo.settled.z_mm > targetZ) targetZ = live.demo.settled.z_mm;
  });
  const scan = plain(demo.scan());
  twoD = { demo, lines, scan };
  equal(scan.points.length, 124, 'number of points');
  equal(count(lines, /^request\.txt ← STEP /), 124, 'STEP requests');
  const sweep = scan.points.slice(0, 4), raster = scan.points.slice(4);
  equal(sweep.map((point) => [point.x_mm, point.y_mm]), [[0, 0], [0, 0], [0, 0], [0, 0]], 'the sweep is taken at the homed X and Y');
  [3.9, 4.0, 4.1, 4.2].forEach((z, index) => near(sweep[index].z_mm, z, 1e-9, 'sweep height ' + index));
  raster.forEach((point) => near(point.z_mm, 4.0, 1e-9, 'working height'));
  near(targetZ, 20, 1e-9, 'Z is sent to 20 mm after homing');
  // The stage is sampled every 100 ms of demo time, so the turn at 20 mm can fall between samples.
  check(seenZ > 19.7 && seenZ <= 20, 'the stage was seen no higher than ' + seenZ + ' mm');
  equal(count(lines, /^Move Z to 20 mm \(motor pos: 694000\)\.\.\.$/), 1);
  equal(count(lines, /^Move Z back to 3\.8 mm \(motor pos: 131860\)\.\.\.$/), 1);
  equal(count(lines, /^Moving to Z_offset = {2}3\.9000 mm \(motor pos: 135330\) \.\.\. OK$/), 1);
  equal(count(lines, /^Moving to Z = 4\.0000 mm \(motor pos: 138800\)\.\.\. OK$/), 1);
  const homing = texts(lines).filter((text) => /^Homing [XYZ]\.\.\.$/.test(text));
  equal(homing, ['Homing X...', 'Homing Y...', 'Homing Z...'], 'homing order');
  const order = ['Homing Z...', 'Move Z to 20 mm', 'Move Z back to 3.8 mm', 'Moving to Z_offset =  3.9000', 'Moving to Z = 4.0000',
    'Move X to start (+2.0000 mm) pos=69400', 'Move Y to start (+2.0000 mm) pos=69400', '---- Sequence begin (X/Y) ----', '---- Sequence end (X/Y) ----'];
  let at = -1;
  for (const marker of order) {
    const index = texts(lines).findIndex((text, position) => position > at && text.startsWith(marker));
    check(index > at, 'out of order or missing: ' + marker);
    at = index;
  }
});

test('2D raster: serpentine in Y, 0.1 mm steps, start corner not captured', () => {
  check(twoD, 'the 2D run did not complete');
  const raster = twoD.scan.points.slice(4).map((point) => [Math.round(point.x_mm * STEPS), Math.round(point.y_mm * STEPS)]);
  equal(raster.length, 120);
  equal(raster[0], [69400, 72870], 'first raster point is (2.0, 2.1)');
  equal(raster[9], [69400, 104100], 'tenth is (2.0, 3.0)');
  equal(raster[10], [72870, 104100], 'then one step in X: (2.1, 3.0)');
  equal(raster[20], [72870, 69400], 'down to (2.1, 2.0)');
  equal(raster[119], [104100, 104100], 'last is (3.0, 3.0)');
  const seen = new Set(raster.map((point) => point.join(',')));
  equal(seen.size, 120, 'every raster position is distinct');
  check(!seen.has('69400,69400'), 'the start corner (2.0, 2.0) must not be captured');
  let previous = [69400, 69400];
  for (const point of raster) {
    const dx = Math.abs(point[0] - previous[0]), dy = Math.abs(point[1] - previous[1]);
    check((dx === 3470 && dy === 0) || (dx === 0 && dy === 3470), 'a move is not one 0.1 mm step on one axis: ' + previous + ' -> ' + point);
    check(point[0] >= 69400 && point[0] <= 104100 && point[1] >= 69400 && point[1] <= 104100, 'outside the 2..3 mm square');
    previous = point;
  }
});

const motorDir = path.join(here, '..', 'macos-native', 'motor');
test('2D call list equals XY_SEQUENCE of the motor program', () => {
  const file = path.join(motorDir, 'raman_autofocus.py');
  if (!fs.existsSync(file)) return 'skip the motor program is not next to the page';
  const source = fs.readFileSync(file, 'utf8');
  const table = source.slice(source.indexOf('XY_SEQUENCE'), source.indexOf('# ======================= Main sequence'));
  const entries = [...table.matchAll(/\("([XY])",\s*([0-9.]+),\s*([0-9.]+),\s*"([^"]+)"\)/g)]
    .map((match) => ({ axis: match[1].toLowerCase(), start: Number(match[2]), end: Number(match[3]), label: match[4] }));
  equal(entries.length, 120, 'entries parsed from raman_autofocus.py');
  equal(plain(E.xySequence()), entries);
});

test('constants equal those of the motor program and the handshake', () => {
  const file = path.join(motorDir, 'raman_autofocus.py');
  const handshake = path.join(motorDir, 'scan_handshake.py');
  if (!fs.existsSync(file) || !fs.existsSync(handshake)) return 'skip the motor program is not next to the page';
  const source = fs.readFileSync(file, 'utf8');
  const value = (name) => {
    const match = source.match(new RegExp('^' + name + '\\s*=\\s*([0-9.]+)', 'm'));
    check(match, name + ' not found in raman_autofocus.py');
    return Number(match[1]);
  };
  const constants = E.constants;
  equal(constants.STEPS_PER_MM, value('STEPS_PER_MM_X'));
  equal(constants.STEPS_PER_MM, value('STEPS_PER_MM_Y'));
  equal(constants.STEPS_PER_MM, value('STEPS_PER_MM_Z'));
  equal(constants.Z_OFFSET_AFTER_HOME, value('Z_OFFSET_AFTER_HOME'));
  equal(constants.Z_RANGE, value('Z_RANGE'));
  equal(constants.Z_STEP, value('Z_STEP'));
  equal(constants.X_OFFSET_AFTER_HOME, value('X_OFFSET_AFTER_HOME'));
  equal(constants.X_STEP, value('X_STEP'));
  equal(constants.Y_OFFSET_AFTER_HOME, value('Y_OFFSET_AFTER_HOME'));
  equal(constants.Y_STEP, value('Y_STEP'));
  check(/highZ = 20\.0/.test(source), 'the 20 mm excursion is no longer in raman_autofocus.py');
  equal(constants.HIGH_Z, 20);
  check(/range\(0, 25, 2\)/.test(fs.readFileSync(handshake, 'utf8')), 'line_scan_positions no longer reads range(0, 25, 2)');
  equal(plain(E.zSweepOffsets()).length, 4, 'Z sweep count');
});

test('planned points equal the captured points', () => {
  for (const mode of ['1d-x', '1d-y', '2d', 'single']) {
    const demo = newDemo();
    demo.setDetails({ scan_mode: mode });
    const plan = plain(demo.scan()).demo.plan.points;
    check(demo.start().ok, 'start failed');
    demo.runToEnd();
    const points = plain(demo.scan()).points;
    equal(plan.length, points.length, mode + ': planned count');
    points.forEach((point, index) => {
      if (mode === 'single') return;
      near(plan[index].x_mm, point.x_mm, 1e-9, mode + ' x ' + index);
      near(plan[index].y_mm, point.y_mm, 1e-9, mode + ' y ' + index);
      near(plan[index].z_mm, point.z_mm, 1e-9, mode + ' z ' + index);
    });
  }
});

test('one point: STEP, HOLD, capture command, result file, point file, ACK OK, advance, next move', () => {
  const { lines } = runScan('1d-x');
  const all = texts(lines);
  const first = all.findIndex((text) => /^request\.txt ← STEP \S+ 1 /.test(text));
  const expected = [/^request\.txt ← STEP \S+ 1 0 /, /^response\.txt ← ACK \S+ 1 HOLD \S+$/, /^command\.txt ← S \S+_1$/,
    /^\[Capture handshake v2\.2\] Waiting 1250 ms$/, /^\[Capture ready\] elapsed_ms=\d+$/,
    /^capture_results\/\S+_1\.txt ← CAPTURE OK 2048$/, /^point_0001\.json \+ combined log saved$/,
    /^response\.txt ← ACK \S+ 1 OK \S+$/, /^\[Point 1\] saved, advance$/, /^\[1D\] X at 2 mm$/, /^request\.txt ← STEP \S+ 2 69400 /];
  const mine = all.slice(first).filter((text) => !text.startsWith('sync:'));
  expected.forEach((pattern, index) => check(pattern.test(mine[index]), 'line ' + index + ' is ' + JSON.stringify(mine[index])));
  const who = lines.slice(lines.findIndex((line) => line.text === all[first])).filter((line) => line.who !== 'data').slice(0, 9).map((line) => line.who);
  equal(who, ['motor', 'controller', 'controller', 'backend', 'backend', 'backend', 'controller', 'controller', 'motor']);
  const hold = lines.find((line) => /ACK \S+ 1 HOLD/.test(line.text)), ready = lines.find((line) => /^\[Capture ready\]/.test(line.text));
  equal(ready.t - hold.t, E.constants.FIRST_STATUS_MS, 'the capture takes the backend\'s first status delay');
});

// ---------------------------------------------------------------------------- pause, step, stop
function untilPoints(demo, wanted, lines) {
  let guard = 0;
  while (plain(demo.scan()).points.length < wanted && demo.runActive() && guard++ < 100000) advanceCollect(demo, 100, lines || []);
}

test('pause holds the next STEP with PAUSED heartbeats; resume continues the same point', () => {
  const demo = newDemo();
  const lines = [];
  demo.setDetails({ scan_mode: '1d-x' });
  demo.start();
  untilPoints(demo, 3, lines);
  check(demo.pause().ok, 'pause refused');
  advanceCollect(demo, 20000, lines);
  const live = plain(demo.live());
  const saved = plain(demo.scan()).points.length;
  check(saved === 3 || saved === 4, 'at most the point in hand is saved after Pause, got ' + saved);
  check(live.demo.run.held && live.demo.run.paused, 'the motor is not held');
  check(live.ack === 'PAUSED', 'the last ACK is ' + live.ack);
  const beats = live.demo.run.heartbeats;
  demo.advance(1000);
  equal(plain(demo.live()).demo.run.heartbeats - beats, 1000 / E.constants.STEP_TIMER_MS, 'one heartbeat per controller tick');
  equal(plain(demo.scan()).points.length, saved, 'no capture while paused');
  check(count(lines, /^\[Paused\] Incoming STEP will be held\.$/) === 1, 'pause line missing');
  check(count(lines, /ACK \S+ \d+ PAUSED /) >= 1, 'no PAUSED acknowledgement');
  check(demo.resume().ok, 'resume refused');
  const rest = runCollect(demo);
  check(count(rest, /^\[Resume\] Continuing the same pending point; existing data preserved\.$/) === 1, 'resume line missing');
  equal(plain(demo.scan()).points.length, 13, 'the scan completes after Resume');
  equal(plain(demo.scan()).points.map((point) => point.point_id), Array.from({ length: 13 }, (_, index) => index + 1), 'no point repeated or skipped');
});

test('pause during the exposure saves the point and holds the motor', () => {
  const demo = newDemo();
  const lines = [];
  demo.setDetails({ scan_mode: '1d-x' });
  demo.start();
  let guard = 0;
  while (!plain(demo.live()).demo.exposure.active || plain(demo.live()).demo.exposure.dark || plain(demo.live()).demo.exposure.point !== 2) {
    advanceCollect(demo, 100, lines);
    check(guard++ < 10000, 'exposure of point 2 never started');
  }
  demo.pause();
  equal(plain(demo.resume()).ok, false, 'Resume is refused while a capture is in progress');
  advanceCollect(demo, 5000, lines);
  equal(plain(demo.scan()).points.length, 2, 'the exposed point is saved');
  check(count(lines, /ACK \S+ 2 PAUSED \S+ {2}\(point saved/) === 1, 'the saved point is acknowledged with PAUSED');
  equal(count(lines, /^\[Point 2\] saved, advance/), 0, 'the motor does not advance while paused');
  demo.resume();
  advanceCollect(demo, 300, lines);
  check(count(lines, /ACK \S+ 2 OK \S+ {2}\(saved point, again\)/) === 1, 'no second acknowledgement after Resume');
  equal(count(lines, /^\[Point 2\] saved, advance/), 1, 'the motor advances after Resume');
});

test('step lets exactly one point through each time', () => {
  const demo = newDemo();
  demo.setDetails({ scan_mode: '1d-y' });
  check(plain(demo.step()).ok, 'step from idle refused');
  demo.advance(60000);
  equal(plain(demo.scan()).points.length, 1, 'after the first step');
  check(plain(demo.live()).demo.run.held, 'not held after the first step');
  demo.step();
  demo.advance(60000);
  equal(plain(demo.scan()).points.length, 2, 'after the second step');
  demo.step();
  demo.advance(60000);
  equal(plain(demo.scan()).points.length, 3, 'after the third step');
  demo.resume();
  demo.runToEnd();
  equal(plain(demo.scan()).points.length, 13, 'Resume finishes the scan');
});

test('stop during a move: cancel.txt, the stage stops, nothing more is captured', () => {
  const demo = newDemo();
  const lines = [];
  demo.setDetails({ scan_mode: '1d-x' });
  demo.start();
  untilPoints(demo, 2, lines);
  let guard = 0;
  while (!plain(demo.live()).stage.moving) { advanceCollect(demo, 100, lines); check(guard++ < 1000, 'no move after point 2'); }
  check(plain(demo.stop()).ok, 'stop refused');
  const where = plain(demo.live()).stage.x_mm;
  check(where > 2 && where < 4, 'the stage should stand between 2 and 4 mm, it is at ' + where);
  const rest = runCollect(demo);
  const all = lines.concat(rest);
  for (const pattern of [/^cancel\.txt ← STOP$/, /^\[Error\] Movement cancelled or timed out; scan aborted\.$/, /^Scan aborted; no further point acquired\.$/,
    /^\[Stop\] Scan stopped\. This does not switch the laser off\.$/, /^\[Laser\] OFF confirmed\.$/]) {
    check(count(all, pattern) >= 1, 'missing line ' + pattern);
  }
  equal(plain(demo.scan()).points.length, 2, 'no further point');
  near(plain(demo.live()).stage.x_mm, where, 1e-9, 'the stage moved after the stop');
  equal(plain(demo.live()).demo.run.state, 'stopped');
  equal(plain(demo.live()).active, false);
  equal(plain(demo.stop()).ok, false, 'a second stop has nothing to stop');
});

test('stop during the exposure: the capture is discarded, not saved', () => {
  const demo = newDemo();
  const lines = [];
  demo.setDetails({ scan_mode: '1d-x' });
  demo.start();
  let guard = 0;
  while (plain(demo.live()).demo.exposure.point !== 3 || !plain(demo.live()).demo.exposure.active) {
    advanceCollect(demo, 100, lines);
    check(guard++ < 10000, 'exposure of point 3 never started');
  }
  demo.stop();
  const all = lines.concat(runCollect(demo));
  equal(plain(demo.scan()).points.length, 2, 'point 3 must not be saved');
  check(count(all, /^\[Point 3\] FAILED\/cancelled, do not advance$/) === 1, 'the motor line is missing');
  check(count(all, /^\[Capture discarded from scan\] Scan stopped; raw backend result retained: /) === 1, 'the discard line is missing');
  const discard = all.findIndex((line) => line.text.startsWith('[Capture discarded')), off = all.findIndex((line) => line.text === '[Laser] OFF confirmed.' && line.t > 2000);
  check(discard >= 0 && off > discard, 'the laser must stay on until the capture in hand is over');
});

test('a scan does not start with an empty required field or while one is running', () => {
  const demo = newDemo();
  demo.setDetails({ operator: '   ' });
  equal(plain(demo.start()).ok, false, 'started without an operator');
  demo.setDetails({ operator: 'someone', scan_mode: '1d-x' });
  equal(plain(demo.start()).ok, true);
  equal(plain(demo.start()).ok, false, 'a second run started');
});

test('single capture: one spectrum, no motor program, no recorded position', () => {
  const { demo, lines, scan } = runScan('single');
  equal(scan.points.length, 1);
  equal([scan.points[0].x_mm, scan.points[0].y_mm, scan.points[0].z_mm], [null, null, null]);
  equal(count(lines, /^request\.txt/), 0, 'a single capture has no STEP');
  equal(count(lines, /^Homing /), 0, 'a single capture moves nothing');
  equal(scan.session.scan_mode, 'single');
  check(/^single_\d{8}_\d{6}_\d{3}$/.test(scan.session.folder), 'folder name ' + scan.session.folder);
  settle(demo);
  const item = plain(demo.pipeline()).items[0];
  equal(item.position_mm, null);
  equal(item.state, 'uploaded');
});

// ---------------------------------------------------------------------------- pipeline
test('register: one record per spectrum with the aliases of 5.2, raw file uploaded', () => {
  const { demo, scan } = runScan('1d-x');
  settle(demo);
  const view = plain(demo.pipeline());
  equal(view.items.length, 13);
  equal(view.status.counts.uploaded, 13, 'all uploaded');
  const catalog = demo.catalog();
  for (const item of view.items) {
    equal(item.record_alias, 'rs-' + sha(item.capture_id).slice(0, 12));
    check(/^d\/\d+$/.test(item.record_id), 'record id ' + item.record_id);
    equal(item.upload.status, 'succeeded');
    const record = plain(catalog.records[item.record_id]);
    equal(record.metadata.type, 'raman.raw_spectrum');
    equal(record.metadata.capture_id, item.capture_id);
    equal(record.metadata.simulated, true, 'simulated spectra are marked');
    equal(record.parent, 'rsess-' + sha(scan.session.session_id).slice(0, 12), 'session collection');
    check(/^rdark-[0-9a-f]{12}$/.test(plain(catalog.byAlias) && Object.keys(plain(catalog.byAlias)).find((name) => name.startsWith('rdark-'))), 'dark record alias');
  }
  equal(plain(catalog.collections['raman-portable-to-classify']).items.length, 13, 'waiting for the classifier');
  equal(view.status.demo, true);
  check(/demo/i.test(view.status.mode_label) && /nothing is sent/i.test(view.status.mode_label), 'the catalog label must say demo and that nothing is sent');
});

function reviewEverything(demo) {
  const decisions = ['confirm', 'relabel', 'reject'];
  const queue = plain(demo.pipeline()).review;
  queue.forEach((item, index) => {
    const decision = decisions[index % 3];
    const result = plain(demo.review(item.prediction_id, decision, decision === 'relabel' ? demo.classes[0] : null, 'test reviewer', ''));
    check(result.ok, 'review refused: ' + JSON.stringify(result));
  });
  return queue;
}

test('classify, link, review: every token reaches a final state', () => {
  const { demo } = runScan('2d');
  settle(demo);
  equal(states(demo).uploaded, 124);
  const started = classifyAll(demo);
  equal(started.processed, 124);
  let view = plain(demo.pipeline());
  equal(view.status.counts.accepted + view.status.counts.needs_review, 124, 'every spectrum has a prediction');
  check(view.status.counts.needs_review > 0, 'this sample should leave something to review');
  equal(view.review.length, view.status.counts.needs_review);
  const catalog = demo.catalog();
  for (const item of view.items) {
    const record = plain(catalog.records[item.prediction.record_id]);
    equal(record.alias, 'rp-' + sha(item.capture_id + ':' + demo.modelHash).slice(0, 12));
    equal(record.deps.map((dep) => dep.relation), ['der', 'der', 'der'], 'derived from raw spectrum, model and dark');
    const chain = plain(demo.provenance(record.id));
    equal(chain.nodes.map((node) => node.type).sort(), ['raman.dark_spectrum', 'raman.model', 'raman.prediction', 'raman.raw_spectrum']);
    equal(chain.edges.length, 3);
    check(chain.edges.every((edge) => edge.from === record.id && edge.relation === 'der' && edge.label === 'is derived from'), 'edges must start at the prediction');
    equal(chain.nodes.find((node) => node.role === 'start').id, record.id, 'the chain starts at the record asked for');
    equal(chain.nodes.filter((node) => node.role === 'source').length, 3);
    const back = plain(demo.provenance(item.record_id));
    equal(back.nodes.find((node) => node.role === 'derived').id, record.id, 'from the raw spectrum the chain reaches its prediction');
    equal(back.nodes.length, 4, 'and through it the model and the dark');
    equal(chain.edges[0].to, item.record_id, 'the first link is the raw spectrum');
    equal(record.metadata.needs_review, item.prediction.needs_review);
    equal(item.state, item.prediction.needs_review ? 'needs_review' : 'accepted');
    equal(record.metadata.simulated, true);
  }
  equal(plain(catalog.collections['raman-portable-to-classify']).items.length, 0, 'the classifier queue is empty');
  equal(plain(catalog.collections['raman-portable-needs-review']).items.length, view.review.length);
  const before = {};
  view.review.forEach((item) => { before[item.prediction_id] = plain(catalog.records[item.prediction_id]).metadata; });
  reviewEverything(demo);
  view = plain(demo.pipeline());
  equal(view.status.counts.accepted + view.status.counts.reviewed, 124, 'final states');
  check(view.items.every((item) => E.constants.FINAL_STATES.includes(item.state)), 'a token is not in a final state');
  equal(view.review.length, 0);
  equal(plain(catalog.collections['raman-portable-needs-review']).items.length, 0);
  for (const id of Object.keys(before)) {
    const after = plain(catalog.records[id]).metadata;
    for (const key of ['label', 'top_class', 'confidence', 'scores', 'outcome']) equal(after[key], before[id][key], 'a review must not overwrite the model\'s ' + key);
    check(['confirmed', 'relabelled', 'rejected'].includes(after.review.status), 'review status ' + after.review.status);
    equal(after.needs_review, false);
    equal(after.review.reviewer, 'test reviewer');
  }
  equal(plain(demo.review(Object.keys(before)[0], 'confirm', null, 'test reviewer', '')).ok, false, 'a reviewed prediction is not reviewed twice');
});

test('the page\'s opening example leaves only reviews to do', () => {
  const demo = newDemo(20261113);
  demo.runExample();
  let view = plain(demo.pipeline());
  equal(view.items.length, 137, '13 + 124 spectra');
  equal(view.status.counts.accepted + view.status.counts.needs_review, 137);
  check(view.status.counts.needs_review > 0, 'the example should show a review queue');
  equal(plain(demo.live()).demo.laser.on, false);
  equal(demo.runActive(), false);
  reviewEverything(demo);
  view = plain(demo.pipeline());
  check(view.items.every((item) => E.constants.FINAL_STATES.includes(item.state)), 'a token is not in a final state');
});

// ---------------------------------------------------------------------------- offline
test('offline: captures wait locally, then register and upload on reconnect', () => {
  const demo = newDemo();
  equal(plain(demo.setOffline(true)).offline, true);
  demo.setDetails({ scan_mode: '1d-x' });
  demo.start();
  demo.runToEnd();
  settle(demo, 6);
  let view = plain(demo.pipeline());
  equal(view.status.online, false);
  equal(view.status.counts.captured, 13, 'all captures wait in the ledger');
  equal(view.status.pending, 13);
  check(view.items.every((item) => item.record_id === null && item.upload === null), 'nothing may be registered while offline');
  equal(Object.keys(plain(demo.catalog().records)).length, 0, 'no record exists yet');
  equal(plain(demo.classify()).ok, false, 'the classifier cannot reach the catalog');
  demo.setOffline(false);
  demo.advance(E.constants.SYNC_PERIOD_MS);
  view = plain(demo.pipeline());
  equal(view.status.counts.uploading, 13, 'the first sync after reconnect registers every waiting capture');
  check(view.items.every((item) => item.upload.status === 'queued'), 'uploads start queued');
  settle(demo, 3);
  view = plain(demo.pipeline());
  equal(view.status.counts.uploaded, 13, 'the uploads finish');
  equal(view.status.pending, 0);
});

test('offline: transfers in flight resume after reconnect', () => {
  const demo = newDemo();
  demo.setDetails({ scan_mode: '1d-x' });
  demo.start();
  let guard = 0;
  while (states(demo).uploading === 0) { demo.advance(100); check(guard++ < 10000, 'no upload ever started'); }
  demo.setOffline(true);
  const frozen = plain(demo.pipeline()).items.filter((item) => item.state === 'uploading').map((item) => [item.capture_id, item.upload.status]);
  check(frozen.length > 0, 'expected an upload in flight');
  demo.runToEnd();
  settle(demo, 6);
  let view = plain(demo.pipeline());
  equal(view.items.filter((item) => item.state === 'uploading').map((item) => [item.capture_id, item.upload.status]), frozen, 'a transfer must not move while offline');
  check(view.status.counts.captured > 0, 'later captures wait');
  demo.setOffline(false);
  settle(demo, 4);
  view = plain(demo.pipeline());
  equal(view.status.counts.uploaded, 13, 'everything arrives after reconnect');
});

test('offline: a review is queued locally and sent by the next sync', () => {
  const { demo } = runScan('1d-x');
  settle(demo);
  // With the threshold at its top value every prediction needs a review, whatever the plate looks like.
  demo.setThreshold(0.99);
  classifyAll(demo);
  const queue = plain(demo.pipeline()).review;
  check(queue.length > 0, 'nothing to review');
  demo.setOffline(true);
  const result = plain(demo.review(queue[0].prediction_id, 'confirm', null, 'test reviewer', ''));
  equal([result.ok, result.queued], [true, true]);
  let item = plain(demo.pipeline()).items.find((entry) => entry.capture_id === queue[0].capture_id);
  equal(item.state, 'needs_review', 'the review has not reached the catalog');
  equal(item.demo.queued_review.decision, 'confirm');
  equal(plain(demo.pipeline()).status.reviews_queued, 1);
  settle(demo, 3);
  equal(plain(demo.pipeline()).items.find((entry) => entry.capture_id === queue[0].capture_id).state, 'needs_review');
  demo.setOffline(false);
  settle(demo, 2);
  item = plain(demo.pipeline()).items.find((entry) => entry.capture_id === queue[0].capture_id);
  equal(item.state, 'reviewed');
  equal(item.review.status, 'confirmed');
});

// ---------------------------------------------------------------------------- threshold
test('threshold: raising it sends more predictions to review, lowering it fewer', () => {
  const demo = newDemo(20261113);
  demo.runExample();
  const ids = () => plain(demo.pipeline()).review.map((item) => item.capture_id).sort();
  const base = ids();
  const confidences = plain(demo.pipeline()).items.map((item) => item.prediction.confidence).filter((value) => value > 0);
  check(confidences.some((value) => value >= 0.8 && value < 0.99), 'no prediction between 0.80 and 0.99 to move');
  equal(plain(demo.setThreshold(0.99)).threshold, 0.99);
  const high = ids();
  check(high.length > base.length, 'a higher threshold must add reviews (' + base.length + ' -> ' + high.length + ')');
  check(base.every((id) => high.includes(id)), 'the higher set must contain the lower one');
  demo.setThreshold(0.5);
  const low = ids();
  check(low.length < base.length, 'a lower threshold must remove reviews (' + base.length + ' -> ' + low.length + ')');
  check(low.every((id) => base.includes(id)), 'the lower set must be inside the base set');
  const view = plain(demo.pipeline());
  for (const item of view.items) {
    const needs = item.prediction.outcome === 'rejected' || item.prediction.confidence < 0.5;
    equal(item.prediction.needs_review, needs, 'rule at 0.50 for point ' + item.point_id);
    equal(item.state, needs ? 'needs_review' : 'accepted');
  }
  demo.setThreshold(0.8);
  equal(ids(), base, 'back at 0.80 the original set returns');
  const reviewed = plain(demo.pipeline()).review[0];
  demo.review(reviewed.prediction_id, 'reject', null, 'test reviewer', '');
  demo.setThreshold(0.99);
  demo.setThreshold(0.5);
  const after = plain(demo.pipeline()).items.find((item) => item.capture_id === reviewed.capture_id);
  equal([after.state, after.review.status], ['reviewed', 'rejected'], 'a person\'s decision is not changed by the threshold');
});

test('stand-in classifier: weaker signal lowers the score, a strong scatterer dominates a mixed spot', () => {
  const sample = EMBED.sample;
  const spot = sample.regions.find((region) => region.shape === 'frame');
  const middle = (spot.inner_mm + spot.outer_mm) / 2;
  const weights = E.weightsAt(sample, middle, 0);
  const score = (signal) => plain(E.standInScores(EMBED, weights, signal, null))[spot.material];
  check(score(1) > score(sample.power_scale.MEDIUM) && score(sample.power_scale.MEDIUM) > score(sample.power_scale.LOW), 'the score must fall with laser power');
  check(score(1) > 0.9, 'a pure, focused spot at full power should be confident');
  check(score(0.05) < 0.8, 'almost no signal must stay under the default threshold');
  near(Object.values(plain(E.standInScores(EMBED, weights, 0.6, null))).reduce((a, b) => a + b, 0), 1, 1e-3, 'scores add up to 1');
  const mean = (name) => plain(EMBED.reference.signals[name]).reduce((a, b) => a + Math.max(0, b), 0) / 256;
  const byStrength = Object.keys(sample.materials).sort((a, b) => mean(b) - mean(a));
  const strong = byStrength[0], weak = byStrength[byStrength.length - 1];
  const half = {}; half[strong] = 0.5; half[weak] = 0.5;
  const mixed = plain(E.standInScores(EMBED, half, 1, null));
  check(mixed[strong] > mixed[weak], 'in an even mix the stronger scatterer must lead');
});

// ---------------------------------------------------------------------------- determinism
test('the same seed gives the same run, ids and records; another seed does not', () => {
  const snapshot = (seed) => {
    const demo = newDemo(seed);
    demo.setDetails({ scan_mode: '1d-y', power: 'MEDIUM' });
    demo.start();
    const lines = runCollect(demo);
    settle(demo);
    classifyAll(demo);
    return JSON.stringify({ scan: demo.scan(), pipeline: demo.pipeline(), lines: lines, records: demo.catalog().records });
  };
  const a = snapshot(42), b = snapshot(42), c = snapshot(43);
  check(a === b, 'two runs with seed 42 differ');
  check(a !== c, 'seeds 42 and 43 give the same run');
  equal(JSON.parse(a).scan.session.session_id === JSON.parse(c).scan.session.session_id, false, 'session ids must depend on the seed');
});

console.log('\n' + passed + ' passed, ' + failures.length + ' failed' + (skipped ? ', ' + skipped + ' skipped' : ''));
if (failures.length) { console.log('failed: ' + failures.join('; ')); process.exit(1); }
