#!/usr/bin/env node
// Checks the upload to ARC in the Simulation Studio (studio/index.html, design section 15).
//
// The engine part loads the page's data and engine blocks into a bare JavaScript context, as
// test_engine.mjs does, and drives StudioEngine.createArcDemo: its states, times, faults and
// buttons. The page part runs the three script blocks with a stub of the DOM (a copy of the one in
// test_page.mjs), in demo mode and in live mode against a stand-in for the Studio server, and checks
// the strip under the lanes: its markup, the lane drawing, the line and the buttons. Nothing here
// opens a connection, starts a program or touches an instrument.
//
//   /usr/local/bin/node studio/test_arc.mjs [path/to/index.html]

import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const page = fs.readFileSync(path.resolve(process.argv[2] || path.join(here, 'index.html')), 'utf8');
const block = (id) => {
  const match = page.match(new RegExp('<script id="' + id + '"[^>]*>([\\s\\S]*?)</script>'));
  if (!match) throw new Error('the page has no <script id="' + id + '"> block');
  return match[1];
};
const markup = page.replace(/<script[\s\S]*?<\/script>/g, '').replace(/<style[\s\S]*?<\/style>/g, '');
const markupIds = [...markup.matchAll(/\bid="([^"]+)"/g)].map((match) => match[1]);
const knownIds = new Set(markupIds);
const selectOptions = {};
for (const match of markup.matchAll(/<select id="([^"]+)"[^>]*>([\s\S]*?)<\/select>/g)) {
  selectOptions[match[1]] = [...match[2].matchAll(/<option value="([^"]*)"/g)].map((option) => option[1]);
}

const engineSource = block('studio-engine');
const sandbox = vm.createContext({});
vm.runInContext(block('studio-data'), sandbox, { filename: 'studio-data' });
vm.runInContext(engineSource, sandbox, { filename: 'studio-engine' });
const E = sandbox.StudioEngine;
const EMBED = sandbox.STUDIO_EMBED;

// Values made inside the sandbox belong to another realm; compare plain copies.
const plain = (value) => JSON.parse(JSON.stringify(value));
const sha = (text) => crypto.createHash('sha256').update(text, 'utf8').digest('hex');

let passed = 0;
const failures = [];
async function test(name, fn) {
  try {
    await fn();
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
  if (a !== b) throw new Error((message || 'values differ') + '\n  actual:   ' + a.slice(0, 600) + '\n  expected: ' + b.slice(0, 600));
}

// The states of an upload and their labels: design section 7.3.
const LABELS = {
  queued: 'Waiting to start', waiting_for_terminal: 'Waiting for Terminal', preparing: 'Preparing the files',
  signing_in: 'Signing in', checking: 'Checking ARC', submitting: 'Handing over to Globus',
  transferring: { globus: 'Copying with Globus', ssh: 'Copying over SSH' }, verifying: 'Checking the copy on ARC',
  placing: 'Putting the copy in place', uncertain: 'Not sure Globus got it', conflict: 'Name already on ARC',
  verified: 'On ARC, checked', failed: 'Failed', stopped: 'Stopped'
};
const labelOf = (state, transport) => (typeof LABELS[state] === 'string' ? LABELS[state] : LABELS[state][transport]);
// An upload on ARC whose newest Check status failed (fix plan I1): a display state, not a stored one.
const CHECK_FAILED = 'On ARC, last check failed';
// A UTC time as the page shows it, in this computer's time zone: "2026-11-15 at 18:05".
const two = (value) => String(value).padStart(2, '0');
function localWhen(utc) {
  const at = new Date(utc);
  return at.getFullYear() + '-' + two(at.getMonth() + 1) + '-' + two(at.getDate()) + ' at ' + two(at.getHours()) + ':' + two(at.getMinutes());
}
// The keys of an arc-list item (design 8.4).
const ITEM_KEYS = ['upload_id', 'created_utc', 'updated_utc', 'session_folder', 'session_id', 'sample_id', 'kind', 'area',
  'transport', 'demo', 'state', 'state_label', 'display_state', 'display_label', 'final', 'message', 'progress', 'destination',
  'worker', 'may_run', 'identity', 'needs', 'actions', 'error', 'warnings', 'notes', 'verification', 'last_check', 'leftover_on_arc'];
const WORKING = ['queued', 'waiting_for_terminal', 'preparing', 'signing_in', 'checking', 'submitting', 'transferring', 'verifying', 'placing'];

const FOLDER = '/projects/pcl_photonics/raman_globus_upload';
const newArc = (extra) => E.createArcDemo(Object.assign({ folder: FOLDER, collectionRoot: '/projects' }, extra || {}));
const S1D = { folder: '1d_x_scan_20261113_101500_123', session_id: '5f0c1d2e-3a4b-4c5d-8e6f-7a8b9c0d1e2f', scan_mode: '1d-x', points: 13, active: false };
const S2D = { folder: '2d_scan_20261113_103000_456', session_id: '6a1b2c3d-4e5f-4a6b-8c7d-8e9f0a1b2c3d', scan_mode: '2d', points: 124, active: false };
const SINGLE = { folder: 'single_20261113_104500_789', session_id: '7b2c3d4e-5f6a-4b7c-9d8e-9f0a1b2c3d4e', scan_mode: 'single', points: 1, active: false };

const itemOf = (arc, id) => plain(arc.uploads()).find((item) => item.upload_id === id);
// Advance in small steps until the upload waits for nobody; the states seen in order and their labels.
function settle(arc, id, stepMs) {
  const seen = [];
  for (let guard = 0; guard < 100000; guard++) {
    const item = itemOf(arc, id);
    const last = seen[seen.length - 1];
    if (!last || last.state !== item.state) seen.push({ state: item.state, label: item.state_label });
    if (WORKING.indexOf(item.state) < 0) return { seen, item };
    arc.advance(stepMs || 10);
  }
  throw new Error('the upload never stopped working');
}
// When each state began, in ms after the upload was made, from its events.
function entries(item) {
  const start = Date.parse(item.created_utc);
  const out = {};
  for (const event of item.events) if (!(event.state in out)) out[event.state] = Date.parse(event.utc) - start;
  return out;
}

// ============================================================================ the engine
await test('engine block still free of the words test_engine.mjs forbids', () => {
  const forbidden = /\b(document|window|navigator|localStorage|sessionStorage|requestAnimationFrame|fetch|XMLHttpRequest|setTimeout|setInterval|Math\.random|Date\.now)\b/;
  const hit = engineSource.match(forbidden);
  check(!hit, 'the engine block mentions ' + (hit && hit[0]));
  check(typeof E.createArcDemo === 'function', 'StudioEngine.createArcDemo is missing');
});

await test('labels: the table of design 7.3, also readable as JSON from the page', () => {
  equal(plain(E.ARC_STATE_LABELS), LABELS);
  const literal = engineSource.match(/const ARC_STATE_LABELS = (\{[\s\S]*?\n {2}\});/);
  check(literal, 'no "const ARC_STATE_LABELS = {...};" in the engine block');
  equal(JSON.parse(literal[1]), LABELS, 'the constant as JSON');
});

await test('Globus: the states in order, their labels, and the time of each step', () => {
  const arc = newArc();
  const made = plain(arc.upload(S1D, 'globus', { fault: null }));
  equal(made.ok, true);
  const { seen, item } = settle(arc, made.id, 10);
  equal(seen.map((entry) => entry.state), ['queued', 'preparing', 'signing_in', 'checking', 'submitting', 'transferring', 'verifying', 'placing', 'verified']);
  seen.forEach((entry) => equal(entry.label, labelOf(entry.state, 'globus'), 'label of ' + entry.state));
  equal(item.progress.files, 22, 'files of a 13-point scan');
  equal(entries(item), { queued: 0, preparing: 0, signing_in: 600, checking: 1800, submitting: 2300, transferring: 2600,
    verifying: 2600 + 80 * 22, placing: 2600 + 80 * 22 + 400, verified: 2600 + 80 * 22 + 600 });
  equal([item.display_state, item.display_label, item.final, item.identity], ['verified', 'On ARC, checked', true, 'demo (no account)']);
  equal(item.verification.method, 'globus-checksum');
  equal(item.step, 'in the demo ARC folder, checked');
  equal(plain(arc.remote()).folders, [S1D.folder]);
});

await test('SSH: the states in order, their labels, and the time of each step', () => {
  const arc = newArc();
  const made = plain(arc.upload(S1D, 'ssh', { fault: null }));
  const { seen, item } = settle(arc, made.id, 10);
  equal(seen.map((entry) => entry.state), ['queued', 'preparing', 'signing_in', 'checking', 'transferring', 'verifying', 'placing', 'verified']);
  seen.forEach((entry) => equal(entry.label, labelOf(entry.state, 'ssh'), 'label of ' + entry.state));
  equal(entries(item), { queued: 0, preparing: 0, signing_in: 600, checking: 2100, transferring: 2400,
    verifying: 2400 + 60 * 22, placing: 2400 + 60 * 22 + 400, verified: 2400 + 60 * 22 + 600 });
  equal([item.identity, item.globus, item.verification.method], [null, null, 'sha256-on-arc']);
  equal(plain(arc.remote()).tasks, [], 'SSH asks nothing of Globus');
});

await test('may_run: true only while the stand-in for Globus may run the transfer, as arc-list says (review R2-04)', () => {
  // arc_records.globus_may_run: the request may have reached Globus (submitting) or its task runs (transferring);
  // once the task has ended (verifying, placing, verified) nothing runs on Globus. SSH never has a Globus transfer.
  const expected = {
    globus: { queued: [false], preparing: [false], signing_in: [false], checking: [false], submitting: [true],
      transferring: [true], verifying: [false], placing: [false], verified: [false] },
    ssh: { queued: [false], preparing: [false], signing_in: [false], checking: [false], transferring: [false],
      verifying: [false], placing: [false], verified: [false] }
  };
  for (const transport of ['globus', 'ssh']) {
    const arc = newArc();
    const made = plain(arc.upload(S1D, transport, { fault: null }));
    const seen = {};
    for (let guard = 0; guard < 100000; guard++) {
      const item = itemOf(arc, made.id);
      seen[item.state] = [...new Set([...(seen[item.state] || []), item.may_run])];
      if (item.state === 'verified') break;
      arc.advance(10);
    }
    equal(seen, expected[transport], transport);
  }
  // A transfer the engine runs: may_run true, so Stop of a check of it would cancel it.
  const arc = newArc();
  const made = plain(arc.upload(S1D, 'globus', {}));
  arc.advance(2700);
  equal([itemOf(arc, made.id).state, itemOf(arc, made.id).may_run], ['transferring', true]);
});

await test('files: points, session.json, 4 handshake files of a scan, 2 dark files, the manifest and SHA256SUMS', () => {
  const arc = newArc();
  for (const [session, files] of [[S1D, 22], [S2D, 133], [SINGLE, 6], [Object.assign({}, S1D, { folder: 'empty_folder', points: 0 }), 9]]) {
    const made = plain(arc.upload(session, 'globus', {}));
    const item = itemOf(arc, made.id);
    equal(item.progress.files, files, session.folder);
    check(item.progress.bytes > 0 && item.progress.files_done === 0 && item.progress.bytes_done === 0, 'progress of ' + session.folder);
  }
});

await test('the same uploads whatever the size of the steps the page advances by', () => {
  const run = (step) => {
    const arc = newArc();
    arc.upload(S2D, 'globus', { fault: 'lose_submit_reply' });
    arc.upload(S1D, 'ssh', { fault: 'drop_connection' });
    for (let spent = 0; spent < 20000; spent += step) arc.advance(step);
    return JSON.stringify({ uploads: arc.uploads(), remote: arc.remote() });
  };
  const once = (() => { const arc = newArc(); arc.upload(S2D, 'globus', { fault: 'lose_submit_reply' }); arc.upload(S1D, 'ssh', { fault: 'drop_connection' }); arc.advance(20000); return JSON.stringify({ uploads: arc.uploads(), remote: arc.remote() }); })();
  equal(run(5), once, '5 ms steps');
  equal(run(250), once, '250 ms steps');
  equal(run(16 * 32), once, 'frames at speed 32');
});

await test('ids, the arc-list shape and the paths of design 5 (area simulated)', () => {
  const arc = newArc();
  const first = plain(arc.upload(S1D, 'globus', {})), second = plain(arc.upload(S2D, 'ssh', {}));
  equal(first.id, 'u' + sha(S1D.session_id + ' 1').slice(0, 12));
  equal(second.id, 'u' + sha(S2D.session_id + ' 2').slice(0, 12));
  const item = itemOf(arc, first.id);
  ITEM_KEYS.forEach((key) => check(key in item, 'the item has no ' + key));
  equal([item.kind, item.area, item.demo, item.transport, item.session_folder, item.session_id], ['simulated', 'simulated', true, 'globus', S1D.folder, S1D.session_id]);
  equal(item.destination.final_path, FOLDER + '/simulated/' + S1D.folder);
  equal(item.destination.globus_final_path, '/pcl_photonics/raman_globus_upload/simulated/' + S1D.folder);
  equal(item.destination.incoming_path, FOLDER + '/simulated/_incoming/' + S1D.folder + '.' + first.id);
  equal(itemOf(arc, second.id).destination.globus_final_path, null, 'an SSH upload has no Globus path');
  const whole = E.createArcDemo({ folder: FOLDER, collectionRoot: '/' });
  const made = plain(whole.upload(S1D, 'globus', {}));
  equal(itemOf(whole, made.id).destination.globus_final_path, FOLDER + '/simulated/' + S1D.folder, 'a collection that shows the whole cluster');
});

await test('a session being scanned is refused; a session under way gets no second upload', () => {
  const arc = newArc();
  equal(plain(arc.upload(Object.assign({}, S1D, { active: true }), 'globus', {})), { ok: false, reason: 'running', text: 'Upload it when the scan has ended.' });
  equal(plain(arc.uploads()), []);
  const made = plain(arc.upload(S1D, 'globus', {}));
  arc.advance(1000);
  equal(plain(arc.upload(S1D, 'ssh', {})), { ok: true, id: made.id, existing: true });
  equal(plain(arc.uploads()).length, 1);
  equal(plain(arc.upload(S1D, 'scp', {})).reason, 'usage');
  equal(plain(arc.upload(S2D, 'ssh', { fault: 'lose_submit_reply' })).reason, 'usage', 'a Globus fault for SSH');
  equal(plain(arc.upload({}, 'globus', {})).reason, 'unknown_session');
});

await test('lose_submit_reply: the reply is lost, the same request number goes again, Globus answers Duplicate: one task', () => {
  const arc = newArc();
  const made = plain(arc.upload(S1D, 'globus', { fault: 'lose_submit_reply' }));
  const steps = [];
  for (let guard = 0; guard < 10000; guard++) {
    const item = itemOf(arc, made.id);
    if (steps[steps.length - 1] !== item.step) steps.push(item.step);
    if (item.state === 'verified') break;
    arc.advance(10);
  }
  const item = itemOf(arc, made.id);
  equal(item.state, 'verified');
  equal(item.globus.posts.map((post) => post.outcome), ['reply_lost', 'duplicate']);
  const remote = plain(arc.remote());
  equal([remote.requests, remote.tasks.length, remote.tasks[0].status], [1, 1, 'SUCCEEDED'], 'one request number, one task');
  equal(remote.tasks[0].request, item.globus.request);
  equal(entries(item).transferring - entries(item).submitting, 600, 'two requests of 300 ms');
  const at = (pattern) => steps.findIndex((text) => pattern.test(text));
  const saved = at(/^saved request number [0-9a-f]{4} before sending it$/), lost = at(/reply of Globus was lost/), duplicate = at(/^Globus answered Duplicate: already received/);
  check(saved >= 0 && lost > saved && duplicate > lost, 'the steps in order: ' + JSON.stringify(steps.slice(0, 8)));
  check(steps[lost].endsWith(item.globus.request.slice(0, 4)) && steps[duplicate].indexOf(item.globus.task.slice(0, 4)) > 0, 'the steps name the request and the task');
});

await test('drop_connection: SSH stops at half; Retry same request completes it into one folder', () => {
  const arc = newArc();
  const made = plain(arc.upload(S1D, 'ssh', { fault: 'drop_connection' }));
  let { item } = settle(arc, made.id, 10);
  equal([item.state, item.message, item.step], ['failed', 'The SSH connection ended during the upload.', 'The SSH connection ended during the upload.']);
  equal([item.progress.files_done, item.error.reason, item.error.retryable], [11, 'ssh_interrupted', true]);
  equal(item.actions, ['retry', 'check']);
  equal(item.leftover_on_arc, item.destination.incoming_path);
  equal(plain(arc.remote()).incoming, [{ name: S1D.folder + '.' + made.id, files: 11 }]);
  equal(plain(arc.retry(made.id)), { ok: true });
  ({ item } = settle(arc, made.id, 10));
  equal([item.state, item.progress.files_done, item.attempt, item.leftover_on_arc], ['verified', 22, 2, null]);
  check(entries(item).transferring !== undefined, 'the retry sent the files');
  // As arc_remote.sh does, and as the fix says: a partial copy in _incoming is not continued, every file is
  // sent again into the same folder.
  equal(item.error, null);
  const texts = item.events.map((event) => event.text);
  const retried = texts.findIndex((text) => /^Retry same request: attempt 2 /.test(text));
  check(retried >= 0 && texts[retried] === 'Retry same request: attempt 2 sends the files again into the same _incoming folder', 'the retry says what it sends: ' + texts[retried]);
  check(texts.slice(retried).some((text) => text === 'sending 22 files over SSH into _incoming/' + S1D.folder + '.' + made.id), 'the retry sends all 22 files: ' + JSON.stringify(texts.slice(retried)));
  const remote = plain(arc.remote());
  equal([remote.folders, remote.incoming], [[S1D.folder], []], 'one folder, and nothing left in _incoming');
});

await test('a name already on ARC: Globus stops before sending; Keep as new copy puts it beside, as _copy2 then _copy3', () => {
  const arc = newArc();
  settle(arc, plain(arc.upload(S1D, 'globus', {})).id);
  const again = plain(arc.upload(S1D, 'globus', {}));
  let { item } = settle(arc, again.id);
  equal([item.state, item.state_label, item.conflict_at, item.needs, item.actions], ['conflict', 'Name already on ARC', 'checking', 'decision', ['keep_copy', 'retry', 'stop']]);
  // Review COR-05: the demo's copy and its name are in the demo ARC folder, not on ARC.
  equal(item.message, 'Demo: the demo ARC folder already has a folder named ' + S1D.folder + '. Nothing was sent. Keep this upload as a new copy beside it, or move the other folder and press Retry same request.');
  equal(item.step, 'the name ' + S1D.folder + ' is taken in the demo ARC folder, so nothing was sent; Keep as new copy puts this upload beside it');
  equal(plain(arc.remote()).tasks.length, 1, 'nothing was sent for the second upload');
  arc.retry(again.id);
  ({ item } = settle(arc, again.id));
  equal(item.state, 'conflict', 'Retry same request finds the name taken again');
  arc.keepCopy(again.id);
  ({ item } = settle(arc, again.id));
  equal([item.state, item.destination.placed_as], ['verified', S1D.folder + '_copy2']);
  equal(item.destination.final_path, FOLDER + '/simulated/' + S1D.folder + '_copy2');
  const third = plain(arc.upload(S1D, 'globus', {}));
  settle(arc, third.id);
  arc.keepCopy(third.id);
  equal(settle(arc, third.id).item.destination.placed_as, S1D.folder + '_copy3');
  equal(plain(arc.remote()).folders, [S1D.folder, S1D.folder + '_copy2', S1D.folder + '_copy3']);
});

await test('a name already on ARC over SSH: the checked copy waits in _incoming; Keep as new copy renames it', () => {
  const arc = newArc();
  settle(arc, plain(arc.upload(S1D, 'globus', {})).id);
  const made = plain(arc.upload(S1D, 'ssh', {}));
  let { item } = settle(arc, made.id);
  equal([item.state, item.conflict_at, item.progress.files_done], ['conflict', 'placing', 22]);
  equal(item.step, 'the name ' + S1D.folder + ' is taken in the demo ARC folder; the checked copy waits in _incoming/' + S1D.folder + '.' + made.id);
  check(!/on ARC/.test(item.message), 'the demo says no "on ARC": ' + item.message);
  equal(plain(arc.remote()).incoming, [{ name: S1D.folder + '.' + made.id, files: 22 }]);
  arc.keepCopy(made.id);
  ({ item } = settle(arc, made.id));
  equal([item.state, item.destination.placed_as, item.attempt], ['verified', S1D.folder + '_copy2', 2]);
  check(item.events.every((event) => event.state !== 'transferring' || Date.parse(event.utc) < Date.parse(item.events.find((e) => /^Keep as new copy/.test(e.text)).utc)),
    'Keep as new copy sends no file again');
  equal(plain(arc.remote()).incoming, []);
});

await test('Stop: before anything was sent, and during the copy; Retry same request finishes the upload', () => {
  const arc = newArc();
  const early = plain(arc.upload(S1D, 'globus', {}));
  arc.advance(700);
  equal(itemOf(arc, early.id).state, 'signing_in');
  equal(plain(arc.stop(early.id)), { ok: true });
  let item = itemOf(arc, early.id);
  equal([item.state, item.message, item.actions, item.leftover_on_arc], ['stopped', 'Demo: stopped before anything was sent.', ['retry'], null]);
  arc.retry(early.id);
  equal(settle(arc, early.id).item.state, 'verified');

  const late = plain(arc.upload(S2D, 'globus', {}));
  arc.advance(2600 + 80 * 30);
  equal(itemOf(arc, late.id).state, 'transferring');
  arc.stop(late.id);
  item = itemOf(arc, late.id);
  equal(item.state, 'stopped');
  equal(item.message, 'Demo: stopped. Files already copied stay in ' + item.destination.incoming_path + '; nothing was deleted.');
  equal(item.actions, ['retry', 'check']);
  equal(plain(arc.remote()).tasks.find((task) => task.task_id === item.globus.task).status, 'FAILED', 'the task is cancelled');
  arc.retry(late.id);
  item = settle(arc, late.id).item;
  equal([item.state, item.attempt, item.globus.earlier_attempts.length], ['verified', 2, 1]);
  const remote = plain(arc.remote());
  equal(remote.folders, [S1D.folder, S2D.folder], 'one folder for each session');
  equal(remote.incoming, []);
});

await test('Check status, and the refusals of the four buttons', () => {
  const arc = newArc();
  const made = plain(arc.upload(S1D, 'globus', {}));
  arc.advance(2700);
  equal(plain(arc.check(made.id)), { ok: false, reason: 'not_checkable', text: 'This upload cannot be checked while it is Copying with Globus.' });
  equal(plain(arc.keepCopy(made.id)).reason, 'no_conflict');
  equal(plain(arc.retry(made.id)).reason, 'not_retryable');
  settle(arc, made.id);
  const before = arc.revision();
  equal(plain(arc.check(made.id)), { ok: true });
  check(arc.revision() !== before, 'a check is a change');
  const item = itemOf(arc, made.id);
  // Review COR-05: the demo's copy is in the demo ARC folder, not on ARC; its check looked (review REG-01).
  equal([item.state, item.last_check.ok, item.last_check.message], ['verified', true, 'Demo: still in the demo ARC folder: 22 files with the right sizes.']);
  equal([item.last_check.looked, item.last_check.fix, item.may_run], [true, null, false]);
  equal(item.step, 'checked: ' + S1D.folder + ' is still in the demo ARC folder with all 22 files');
  equal(plain(arc.stop(made.id)), { ok: false, reason: 'nothing_to_stop', text: 'This upload cannot be stopped while it is On ARC, checked.' });
  equal(plain(arc.retry('u000000000000')).reason, 'unknown_upload');
});

await test('Check status of an upload whose folder left ARC: check_failed with that check\'s words; the session can go again', () => {
  equal(plain(E.ARC_DISPLAY_LABELS), { check_failed: CHECK_FAILED });
  const arc = newArc();
  const made = plain(arc.upload(S1D, 'globus', {}));
  settle(arc, made.id);
  equal(plain(arc.removeFolder('no_such_folder')), { ok: false, reason: 'unknown_folder', text: 'There is no folder no_such_folder in the demo ARC folder.' });
  const before = arc.revision();
  equal(plain(arc.removeFolder(S1D.folder)), { ok: true });
  check(arc.revision() !== before, 'a folder that leaves ARC is a change');
  equal(plain(arc.remote()).folders, []);
  let item = itemOf(arc, made.id);
  equal([item.display_state, item.display_label], ['verified', 'On ARC, checked'], 'nothing is known before Check status');
  equal(plain(arc.check(made.id)), { ok: true });
  item = itemOf(arc, made.id);
  const gone = 'Demo: the folder ' + FOLDER + '/simulated/' + S1D.folder + ' is no longer in the demo ARC folder.';
  equal([item.state, item.state_label, item.display_state, item.display_label, item.final],
    ['verified', 'On ARC, checked', 'check_failed', CHECK_FAILED, true]);
  equal([item.last_check.ok, item.last_check.message, item.message], [false, gone, gone], 'the message is the newest check\'s');
  equal(item.actions, ['check', 'show_in_finder']);
  equal(item.step, 'checked: ' + S1D.folder + ' is no longer in the demo ARC folder');
  // The same session again: its name is free, so the new upload goes in place under it.
  const again = plain(arc.upload(S1D, 'globus', {}));
  equal(again.existing, false);
  const placed = settle(arc, again.id).item;
  equal([placed.state, placed.destination.placed_as], ['verified', S1D.folder]);
  arc.check(made.id);
  item = itemOf(arc, made.id);
  equal([item.display_state, item.display_label, item.last_check.ok], ['verified', 'On ARC, checked', true], 'a check that finds the folder again');
  equal(item.message, 'Demo: 22 files in the demo ARC folder, every one checked. Nothing left this page.');
});

await test('Keep as new copy takes the first free name, the plain name first', () => {
  const arc = newArc();
  settle(arc, plain(arc.upload(S1D, 'globus', {})).id);
  const made = plain(arc.upload(S1D, 'ssh', {}));
  equal(settle(arc, made.id).item.state, 'conflict');
  arc.removeFolder(S1D.folder);
  arc.keepCopy(made.id);
  const item = settle(arc, made.id).item;
  equal([item.state, item.destination.placed_as], ['verified', S1D.folder]);
  check(item.events.some((event) => event.text === 'Keep as new copy: the name ' + S1D.folder + ' is free again, so it goes there'), 'Keep as new copy says where it goes');
});

await test('uploads(): newest first, at most 8; revision() changes with every change and only then', () => {
  const arc = newArc();
  let revision = arc.revision();
  const ids = [];
  for (let n = 0; n < 10; n++) {
    ids.push(plain(arc.upload(Object.assign({}, SINGLE, { folder: 'single_20261113_1100' + String(n).padStart(2, '0') + '_000', session_id: 'session ' + n }), 'ssh', {})).id);
    check(arc.revision() !== revision, 'an upload is a change');
    revision = arc.revision();
  }
  equal(plain(arc.uploads()).map((item) => item.upload_id), ids.slice(2).reverse());
  arc.advance(100);
  check(arc.revision() !== revision, 'the steps are a change');
  arc.advance(60000);
  revision = arc.revision();
  arc.advance(5000);
  arc.uploads();
  arc.remote();
  equal(arc.revision(), revision, 'nothing changes once every upload has ended');
});

await test('the demo\'s own outputs are the same with and without uploads to ARC', () => {
  const run = (withArc) => {
    const demo = E.createDemo(EMBED, { seed: 20261113 });
    const arc = withArc ? newArc() : null;
    const sent = new Set();
    const upload = () => {
      const scan = demo.scan();
      for (const session of scan.sessions) {
        if (sent.has(session.folder) || (scan.active && session.folder === scan.sessions[0].folder)) continue;
        sent.add(session.folder);
        const points = demo.pipeline().items.filter((item) => item.session_folder === session.folder).length;
        arc.upload(Object.assign({}, session, { points, active: false }), sent.size % 2 ? 'globus' : 'ssh', { fault: sent.size % 2 ? 'lose_submit_reply' : 'drop_connection' });
      }
    };
    demo.runExample();
    if (arc) upload();
    demo.setDetails({ scan_mode: 'single' });
    demo.start();
    for (let i = 0; i < 300; i++) {
      demo.advance(100);
      if (arc) { arc.advance(100); upload(); }
    }
    demo.classify();
    demo.advance(5000);
    if (arc) check(plain(arc.uploads()).length === 3, 'three sessions were uploaded');
    const pipeline = demo.pipeline();
    return JSON.stringify({ scan: demo.scan(), pipeline, ticker: demo.ticker(0), records: demo.catalog().records,
      aliases: pipeline.items.map((item) => item.record_alias) });
  };
  const without = run(false), withArc = run(true);
  check(without === withArc, 'the demo differs when uploads to ARC run beside it');
});

await test('two engines given the same steps give the same uploads', () => {
  const run = () => {
    const arc = newArc({ clockMs: 123456 });
    arc.upload(S1D, 'globus', { fault: 'lose_submit_reply' });
    arc.advance(1234);
    arc.upload(S2D, 'ssh', { fault: 'drop_connection' });
    arc.advance(30000);
    arc.retry(plain(arc.uploads())[0].upload_id);
    arc.advance(30000);
    return JSON.stringify({ uploads: arc.uploads(), remote: arc.remote(), revision: arc.revision() });
  };
  equal(run(), run());
  check(JSON.parse(run()).uploads[0].created_utc === new Date(Date.UTC(2026, 10, 13, 15, 15, 0, 0) + 123456 + 1234).toISOString(), 'times follow the demo clock');
});

// ============================================================================ the page
const problems = [];
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

// Estimated boxes of the text of a drawing, as test_page.mjs estimates them. Node has no font metrics.
function textBoxes(html) {
  const boxes = [];
  for (const match of html.matchAll(/<text([^>]*)>([\s\S]*?)<\/text>/g)) {
    const attrs = match[1];
    const content = match[2].replace(/<[^>]+>/g, '').replace(/&amp;/g, '&').replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&#39;/g, "'").replace(/&quot;/g, '"');
    const classes = (attrs.match(/class="([^"]*)"/) || [, ''])[1];
    const size = /t-mono/.test(classes) ? 8.5 : /t-tiny/.test(classes) ? 8 : /t-small/.test(classes) ? 9 : 9.5;
    const width = content.length * size * (/t-mono/.test(classes) ? 0.61 : /t-strong|t-warn/.test(classes) ? 0.56 : 0.52);
    const x = Number((attrs.match(/\bx="([-\d.]+)"/) || [, 'NaN'])[1]), y = Number((attrs.match(/\by="([-\d.]+)"/) || [, 'NaN'])[1]);
    const anchor = (attrs.match(/text-anchor="([^"]*)"/) || [, 'start'])[1];
    const x0 = anchor === 'middle' ? x - width / 2 : anchor === 'end' ? x - width : x;
    boxes.push({ text: content, x0, x1: x0 + width, y0: y - size * 0.8, y1: y + size * 0.25 });
  }
  return boxes;
}
const overlap = (a, b) => Math.min(a.x1, b.x1) - Math.max(a.x0, b.x0) > 1 && Math.min(a.y1, b.y1) - Math.max(a.y0, b.y0) > 1;

// The lane: every text and every parcel inside 706 x 48, no two texts and no two parcels on top of each other.
function checkLane(where, parcels) {
  const html = $('arcLane').innerHTML;
  const boxes = textBoxes(html);
  check(boxes.length > 0, where + ': the lane holds no text');
  for (const box of boxes) {
    if (!(box.x0 >= -1 && box.y0 >= -1 && box.x1 <= 707 && box.y1 <= 49)) problems.push(where + ': "' + box.text + '" leaves the lane');
  }
  for (let i = 0; i < boxes.length; i++) {
    for (let j = i + 1; j < boxes.length; j++) if (overlap(boxes[i], boxes[j])) problems.push(where + ': "' + boxes[i].text + '" overlaps "' + boxes[j].text + '"');
  }
  const rects = [...html.matchAll(/<rect class="arcp [a-z]+" x="([-\d.]+)" y="([-\d.]+)" width="([-\d.]+)" height="([-\d.]+)"/g)]
    .map((m) => ({ x0: Number(m[1]), y0: Number(m[2]), x1: Number(m[1]) + Number(m[3]), y1: Number(m[2]) + Number(m[4]) }));
  for (const rect of rects) if (!(rect.x0 >= 0 && rect.y0 >= 0 && rect.x1 <= 706 && rect.y1 <= 48)) problems.push(where + ': a parcel leaves the lane');
  for (let i = 0; i < rects.length; i++) {
    for (let j = i + 1; j < rects.length; j++) {
      const a = rects[i], b = rects[j];
      if (Math.min(a.x1, b.x1) - Math.max(a.x0, b.x0) > 0.5 && Math.min(a.y1, b.y1) - Math.max(a.y0, b.y0) > 0.5) problems.push(where + ': two parcels overlap');
    }
  }
  if (parcels !== undefined) equal(rects.length, parcels, where + ': parcels drawn');
  return { html, boxes, rects };
}
function parcelTitles() { return [...$('arcLane').innerHTML.matchAll(/<g class="arcparcel"><title>([^<]*)<\/title>/g)].map((m) => m[1]); }
// The parcels drawn: how they look (run, ok, wait, bad) and where they are.
function parcels() {
  return [...$('arcLane').innerHTML.matchAll(/<rect class="arcp ([a-z]+)" x="([-\d.]+)" y="([-\d.]+)" width="([-\d.]+)"/g)]
    .map((m) => ({ look: m[1], x: Number(m[2]), y: Number(m[3]), w: Number(m[4]) }));
}
const onBand = (parcel) => parcel.x >= 160 && parcel.x + parcel.w <= 480;
function expectClean(where) {
  const found = [...new Set(problems.splice(0))];
  check(!found.length, where + ': ' + found.slice(0, 8).join('; '));
}

// ------------------------------------------------------------------ the stub DOM (a copy of test_page.mjs's)
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
    matchMedia: () => ({ matches: false, addEventListener() {} }),
    requestAnimationFrame: (fn) => { frames.push(fn); return frames.length; },
    setTimeout: (fn, delay) => { const id = ++timerSeq; timers.push({ id, at: now + (delay || 0), fn }); return id; },
    clearTimeout: (id) => { const index = timers.findIndex((timer) => timer.id === id); if (index >= 0) timers.splice(index, 1); },
    location: options.location || { protocol: 'file:', hostname: '' },
    localStorage: { getItem: (key) => (key in store ? store[key] : null), setItem: (key, value) => { store[key] = String(value); } },
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
  for (const id of ['studio-data', 'studio-engine', 'studio-ui']) {
    // options.engines collects every demo of the uploads the page makes, so that a test can act as ARC does.
    if (id === 'studio-ui' && options.engines) {
      const make = context.StudioEngine.createArcDemo;
      context.StudioEngine.createArcDemo = (settings) => { const made = make(settings); options.engines.push(made); return made; };
    }
    vm.runInContext(block(id), context, { filename: id });
  }
  return { step };
}

function freshPage() { elements.clear(); document.listeners = {}; document.activeElement = null; problems.length = 0; }
const $ = (id) => { const el = document.getElementById(id); if (!el) throw new Error('the test asks for an id the page does not have: ' + id); return el; };
const text = (id) => $(id).textContent;
function click(id) {
  if ($(id).disabled) throw new Error('the test wants to press ' + id + ', but it is disabled');
  if ($(id).hidden === true) throw new Error('the test wants to press ' + id + ', but it is hidden');
  $(id).dispatch('click');
}
function choose(id, value) { $(id).value = value; $(id).dispatch('change'); }
function toggle(id, on) { $(id).checked = on; $(id).dispatch('change'); }
async function until(win, condition, frames, what, ms) {
  for (let i = 0; i < frames; i++) { if (condition()) return; await win.step(1, ms); }
  throw new Error('timed out waiting for ' + what + ' (ARC line: ' + text('arcLine') + ')');
}
const shownButtons = () => ['btnArcCheck', 'btnArcRetry', 'btnArcKeep', 'btnArcStop'].filter((id) => $(id).hidden === false);
const GREETING = 'Example data (demo): two finished scans, already classified. Press Play to run another.';

// The strip exactly as design 15.2 gives it, whitespace aside.
const STRIP = `<div class="arcstrip" id="arcStrip" aria-labelledby="h-arc">
  <div class="archead">
    <h3 id="h-arc">Upload to ARC</h3>
    <label class="field" for="arcTransport">With <select id="arcTransport">
      <option value="globus">Globus</option><option value="ssh">SSH</option></select></label>
    <button type="button" class="btn" id="btnArcUpload">Upload to ARC (demo)</button>
    <label class="switch" for="arcAuto" id="arcAutoLabel"><input type="checkbox" role="switch" id="arcAuto" checked> Upload after each scan</label>
    <label class="switch" for="arcDrop" id="arcDropLabel"><input type="checkbox" role="switch" id="arcDrop"> Drop the connection</label>
  </div>
  <figure><svg id="arcLane" viewBox="0 0 706 48" role="img" aria-label="Upload to ARC: the session moves from the laptop to the team folder on ARC, through Globus or over SSH, waits in _incoming while it is checked, and is renamed into place."></svg></figure>
  <p class="event" id="arcLine"></p>
  <div class="row">
    <button type="button" class="btn" id="btnArcCheck">Check status</button>
    <button type="button" class="btn" id="btnArcRetry">Retry same request</button>
    <button type="button" class="btn" id="btnArcKeep">Keep as new copy</button>
    <button type="button" class="btn" id="btnArcStop">Stop upload</button>
  </div>
</div>`;
const squeeze = (value) => value.replace(/\s+/g, ' ').replace(/> </g, '><').trim();

await test('page: the strip of design 15.2, where it belongs, with ids that exist once', () => {
  check(squeeze(markup).includes(squeeze(STRIP)), 'the page does not hold the strip of design 15.2 as it stands');
  const repeated = markupIds.filter((id, index) => markupIds.indexOf(id) !== index);
  equal(repeated, [], 'ids that the markup repeats');
  for (const id of ['arcStrip', 'h-arc', 'arcTransport', 'btnArcUpload', 'arcAuto', 'arcAutoLabel', 'arcDrop', 'arcDropLabel', 'arcLane', 'arcLine',
    'btnArcCheck', 'btnArcRetry', 'btnArcKeep', 'btnArcStop']) check(knownIds.has(id), 'no element with id ' + id);
  const event = markup.indexOf('id="event"'), strip = markup.indexOf('id="arcStrip"'), inspector = markup.indexOf('class="inspector"');
  check(event >= 0 && event < strip && strip < inspector, 'the strip is not between #event and the inspector');
  equal(selectOptions.arcTransport, ['globus', 'ssh']);
});

// ------------------------------------------------------------------ demo mode
await test('demo: the page opens with the example\'s two sessions uploading, and both reach ARC', async () => {
  freshPage();
  const win = makeWindow({});
  await win.step(3);
  equal([$('arcAuto').checked, $('arcDrop').checked, $('arcTransport').value], [true, false, 'globus'], 'the switches as the markup has them');
  equal(['arcAuto', 'arcAutoLabel', 'arcDrop', 'arcDropLabel'].map((id) => $(id).hidden), [false, false, false, false], 'the demo switches are shown in the demo');
  equal($('arcAutoLabel').title, 'In the demo, every scan that ends is uploaded to the demo ARC folder.');
  equal($('arcDropLabel').title, 'Shows what happens when the connection breaks during an upload.');
  equal($('btnArcUpload').title, 'Copies the session on show to the demo ARC folder. The Studio never sends anything to ARC; uploads to ARC start in the controller window.');
  // Review DOC-V02: a retry after a failed transfer copies the files again, so it promises nothing else.
  equal($('btnArcRetry').title, 'Continue this upload with the same files and the same place in the demo ARC folder. After a failed transfer the files are copied again into the same _incoming folder.');
  checkLane('demo at the start', 2);
  const titles = parcelTitles();
  check(titles.some((title) => /^1d_x_scan_\d{8}_\d{6}_\d{3} · /.test(title)) && titles.some((title) => /^2d_scan_\d{8}_\d{6}_\d{3} · /.test(title)),
    'the two example sessions are the parcels: ' + JSON.stringify(titles));
  check(/^ARC: /.test(text('arcLine')), 'the line says what the newest upload does, shown "' + text('arcLine') + '"');
  equal(shownButtons(), ['btnArcStop'], 'a working upload offers Stop upload');
  equal(text('status'), GREETING, 'the uploads leave #status alone');
  // The demo's own panels are those test_page.mjs expects.
  check(/\/137$/.test(text('selCount')) && $('lanesTok').children.length === 137, 'the demo panels changed');
  check(/1 Capture: 137 .*2 Register: 137 .*3 Classify: 137 .*4 Link: 137/.test(text('steps')), 'the step counters changed: ' + text('steps'));
  let seenLaptop = false, seenBand = false, seenIncoming = false;
  await until(win, () => {
    const lane = checkLane('demo while the example uploads');
    seenLaptop = seenLaptop || parcels().some((parcel) => parcel.look === 'run' && parcel.x + parcel.w <= 150);
    seenBand = seenBand || parcels().some((parcel) => parcel.look === 'run' && onBand(parcel));
    seenIncoming = seenIncoming || (/_incoming<\/text>/.test(lane.html) && parcels().some((parcel) => parcel.x >= 628));
    return (lane.html.match(/class="arcmark"/g) || []).length === 2;
  }, 400, 'both example uploads on ARC', 16);
  check(seenLaptop && seenBand && seenIncoming, 'the parcels left the laptop, crossed the band and waited in _incoming');
  check(parcels().every((parcel) => parcel.look === 'ok' && parcel.x >= 490), 'both parcels are in the ARC box');
  equal(text('arcLine'), 'ARC: in the demo ARC folder, checked');
  equal(shownButtons(), ['btnArcCheck'], 'an upload on ARC offers Check status');
  equal(text('status'), GREETING, 'the uploads left #status alone');
  equal(parcelTitles().filter((title) => / · On ARC, checked · Globus · in the demo ARC folder as simulated\//.test(title)).length, 2);
  expectClean('demo at the start');
});

await test('demo: a scan that ends is uploaded; an upload of the scan under way is refused in #arcLine only', async () => {
  freshPage();
  const win = makeWindow({});
  await win.step(3);
  choose('speed', '32');
  choose('scanType', '1d-x');
  click('btnPlay');
  await until(win, () => /point [1-9]/.test(text('roState')), 2000, 'the first point of the 1D scan');
  const status = text('status');
  click('btnArcUpload');
  equal(text('status'), status, 'the refusal does not reach #status');
  await win.step(1);
  equal(text('arcLine'), 'Upload it when the scan has ended.');
  equal(parcelTitles().length, 2, 'no upload of the scan under way');
  await until(win, () => /run complete/.test(text('roState')), 6000, 'the end of the 1D scan');
  await until(win, () => parcelTitles().length === 3, 50, 'the upload of the scan that ended');
  // Drawn oldest first, so that the newest lies on top: the new 1D scan, not the example's.
  const titles = parcelTitles();
  check(/^1d_x_scan_/.test(titles[2]) && titles[2].split(' · ')[0] !== titles[0].split(' · ')[0], 'the newest parcel: ' + JSON.stringify(titles));
  await until(win, () => ($('arcLane').innerHTML.match(/class="arcmark"/g) || []).length === 3, 400, 'the third upload on ARC');
  checkLane('demo after a scan', 3);
  expectClean('demo after a scan');
});

await test('demo: SSH with the connection dropped fails at half; Retry same request finishes it into one folder', async () => {
  freshPage();
  const win = makeWindow({});
  await win.step(3);
  choose('speed', '32');
  choose('arcTransport', 'ssh');
  toggle('arcDrop', true);
  choose('scanType', 'single');
  click('btnPlay');
  await until(win, () => /run complete/.test(text('roState')), 3000, 'the single capture');
  await until(win, () => text('arcLine') === 'ARC: The SSH connection ended during the upload.', 400, 'the dropped connection');
  equal(shownButtons(), ['btnArcCheck', 'btnArcRetry'], 'a failed upload that sent files offers Check status and Retry same request');
  check($('arcLane').innerHTML.includes('class="pt-cross"'), 'the failed parcel carries a cross');
  check(/stroke-dasharray|arcband ssh/.test($('arcLane').innerHTML) && />SSH<\/text>/.test($('arcLane').innerHTML), 'the band is the dashed SSH one');
  checkLane('demo with a failed SSH upload', 3);
  toggle('arcDrop', false);
  click('btnArcRetry');
  await until(win, () => text('arcLine') === 'ARC: in the demo ARC folder, checked', 400, 'the retried upload on ARC');
  check(!$('arcLane').innerHTML.includes('_incoming</text>') && !$('arcLane').innerHTML.includes('pt-cross'), 'nothing is left in _incoming');
  equal(parcelTitles().filter((title) => /^single_/.test(title) && / · in the demo ARC folder as simulated\/single_\d{8}_\d{6}_\d{3}$/m.test(title.split('\n')[0])).length, 1);
  checkLane('demo after the retry', 3);
  expectClean('demo with SSH');
});

await test('demo: the same session again is Name already on ARC; Keep as new copy, Check status and Stop upload', async () => {
  freshPage();
  const win = makeWindow({});
  await win.step(3);
  choose('speed', '32');
  await until(win, () => ($('arcLane').innerHTML.match(/class="arcmark"/g) || []).length === 2, 400, 'the example on ARC');
  // The session on show is the newest one, the 2D scan, which is on ARC already.
  click('btnArcUpload');
  await until(win, () => /^ARC: the name 2d_scan_\S+ is taken in the demo ARC folder, so nothing was sent/.test(text('arcLine')), 200, 'the name taken');
  equal(shownButtons(), ['btnArcRetry', 'btnArcKeep', 'btnArcStop']);
  check(/^Put this upload beside the folder that already has its name, as 2d_scan_\S+_copy2 or the next free name\.$/.test($('btnArcKeep').title), 'Keep as new copy says where: ' + $('btnArcKeep').title);
  checkLane('demo with a name taken', 3);
  click('btnArcKeep');
  await until(win, () => text('arcLine') === 'ARC: in the demo ARC folder, checked', 400, 'the new copy on ARC');
  check(/ · in the demo ARC folder as simulated\/2d_scan_\d{8}_\d{6}_\d{3}_copy2\n/.test(parcelTitles()[2]), 'placed beside as _copy2: ' + parcelTitles()[2]);
  click('btnArcCheck');
  await win.step(1);
  check(/^ARC: checked: 2d_scan_\S+_copy2 is still in the demo ARC folder with all 133 files$/.test(text('arcLine')), 'Check status: ' + text('arcLine'));
  const status = text('status');
  click('btnArcUpload');
  // One frame of 16 ms at speed 32 is 512 ms of demo time: the new upload is still being prepared.
  await win.step(1);
  equal(shownButtons(), ['btnArcStop'], 'a new upload offers Stop upload');
  click('btnArcStop');
  await win.step(1);
  equal(text('arcLine'), 'ARC: stopped before anything was sent');
  equal(shownButtons(), ['btnArcRetry'], 'nothing was sent, so there is nothing to check');
  equal(text('status'), status, 'none of it reaches #status');
  checkLane('demo with a stopped upload', 4);
  expectClean('demo with a name taken');
});

await test('demo: Check status of an upload whose folder left ARC draws it as not ok and says so in #arcLine', async () => {
  freshPage();
  const engines = [];
  const win = makeWindow({ engines });
  await win.step(3);
  choose('speed', '32');
  await until(win, () => ($('arcLane').innerHTML.match(/class="arcmark"/g) || []).length === 2, 400, 'the example on ARC');
  const status = text('status');
  const arc = engines[engines.length - 1];
  const newest = plain(arc.uploads())[0];
  // Someone on ARC moves the newest folder away; the strip knows nothing of it until Check status.
  equal(plain(arc.removeFolder(newest.destination.placed_as)), { ok: true });
  await win.step(2);
  equal(parcels().map((parcel) => parcel.look), ['ok', 'ok']);
  click('btnArcCheck');
  await win.step(1);
  equal(text('arcLine'), 'ARC: checked: ' + newest.session_folder + ' is no longer in the demo ARC folder');
  // Drawn oldest first: the newest parcel is the last one.
  equal(parcels().map((parcel) => parcel.look), ['ok', 'wait'], 'the newest is not drawn as checked');
  check(parcels()[1].x >= 490, 'it stays in the ARC box');
  equal(($('arcLane').innerHTML.match(/class="arcmark"/g) || []).length, 1, 'one check mark is left');
  const checked = plain(arc.uploads())[0].last_check;
  const gone = 'the folder ' + FOLDER + '/simulated/' + newest.session_folder + ' is no longer in the demo ARC folder.';
  equal(parcelTitles()[1].split('\n'), [newest.session_folder + ' · ' + CHECK_FAILED + ' · Globus · in the demo ARC folder as simulated/' + newest.session_folder,
    'Checked on ' + localWhen(checked.utc) + ': ' + gone], 'the tool tip names the failed check once');
  equal(shownButtons(), ['btnArcCheck'], 'Check status is still offered');
  checkLane('demo with a failed check', 2);
  equal(text('status'), status, 'none of it reaches #status');
  expectClean('demo with a failed check');
});

await test('demo: without Upload after each scan a scan that ends is not uploaded; Reset empties the strip', async () => {
  freshPage();
  const win = makeWindow({});
  await win.step(3);
  choose('speed', '32');
  toggle('arcAuto', false);
  choose('scanType', 'single');
  click('btnPlay');
  await until(win, () => /run complete/.test(text('roState')), 3000, 'the single capture');
  await win.step(20);
  equal(parcelTitles().length, 2, 'only the example sessions');
  toggle('arcAuto', true);
  await win.step(5);
  equal(parcelTitles().length, 2, 'turning it on later uploads nothing by itself');
  click('btnArcUpload');
  await win.step(2);
  equal(parcelTitles().length, 3, 'Upload to ARC (demo) uploads the session on show');
  click('btnReset');
  await win.step(3);
  equal(parcelTitles().length, 0);
  equal(text('arcLine'), 'ARC: no upload yet. Turn on Upload after each scan, or press Upload to ARC (demo).');
  equal(shownButtons(), []);
  checkLane('demo after reset', 0);
  click('btnArcUpload');
  await win.step(1);
  equal(text('arcLine'), 'There is no session to upload yet. Press Play to record one.');
  expectClean('demo after reset');
});

// ------------------------------------------------------------------ live mode
const SHOWN = '1d_x_scan_20261115_100000_000';
const NO_PORT_BASE = 'http://127.0.0.1:47321/';
// The settings GET /api/arc passes on (from arc-list's).
const LIVE_SETTINGS = { demo: true, transport: 'globus', folder: FOLDER, include_simulated: false, settle_seconds: 60 };
const liveLabel = (state, transport) => (LABELS[state] === undefined ? { not_watched: 'Not being watched', interrupted: 'Interrupted', not_started: 'Not started', unreadable: 'Unreadable' }[state]
  : labelOf(state, transport || 'globus'));

function liveItem(n, state, extra) {
  const transport = (extra && extra.transport) || (n % 2 ? 'ssh' : 'globus');
  const folder = '2d_scan_20261115_1' + String(n).padStart(5, '0') + '_000';
  return Object.assign({
    upload_id: 'u' + String(n).padStart(12, '0'), created_utc: '2026-11-15T10:00:0' + (n % 10) + '.000Z', session_folder: folder,
    kind: 'simulated', area: 'simulated', transport, demo: true, state, state_label: liveLabel(state, transport),
    display_state: state, display_label: liveLabel(state, transport), final: ['verified', 'failed', 'stopped'].includes(state),
    message: 'Demo: ' + liveLabel(state, transport) + '.', progress: { files: 22, files_done: 0, bytes: 433104, bytes_done: 0 },
    destination: { final_path: FOLDER + '/simulated/' + folder, placed_as: state === 'verified' ? folder : null },
    worker: { alive: WORKING.includes(state), action: 'send', terminal: false },
    actions: WORKING.includes(state) ? ['stop'] : state === 'verified' ? ['check', 'show_in_finder'] : state === 'conflict' ? ['keep_copy', 'retry', 'stop'] : ['retry'],
    error: null, leftover_on_arc: null
  }, extra || {});
}

// A stand-in for `python -m raman_catalog studio`: the endpoints the page reads, and those of the strip.
function standIn() {
  const state = { calls: [], arcDown: false, answers: {},
    arc: { ok: true, uploads: [], active: 0, unfinished: 0, settings: LIVE_SETTINGS } };
  const session = { schema: 'raman-session/1', session_id: 'live-session', scan_mode: '1d-x', started_utc: '2026-11-15T10:00:00.000Z',
    sample_id: 'S-001', simulated: { backend: true, motor: true }, folder: SHOWN };
  function answer(pathname) {
    switch (pathname) {
      case '/api/ping': return { ok: true, home: '/tmp/home', mode: 'local-demo', simulated_stack_available: true };
      case '/api/sample': return { ok: false, error: 'This HOME has no virtual sample' };
      case '/api/scan': return { ok: true, folder: SHOWN, session, active: false, stage: null, request: null, ack: null, points: [],
        sessions: [{ folder: SHOWN, session_id: 'live-session', scan_mode: '1d-x', started_utc: session.started_utc, sample_id: 'S-001', simulated: session.simulated }],
        run: null, log_tail: [] };
      case '/api/pipeline': return { ok: true, status: { counts: {}, online: true, mode: 'local-demo', demo: true, problems: [],
        mode_label: 'Local demo catalog (files on this Mac only; nothing is sent anywhere)' }, items: [], review: [], total: 0 };
      case '/api/arc': return state.arc;
      case '/api/arc/upload': return state.answers[pathname] || { ok: true, existing: false, worker: 'detached', message: 'Demo upload started. Nothing leaves this Mac.' };
      default:
        if (pathname.startsWith('/api/arc/')) return state.answers[pathname] || { ok: true, worker: 'detached', message: 'Demo: ' + pathname.slice(9) + ' sent.' };
        return { ok: false, error: 'unknown endpoint ' + pathname };
    }
  }
  function fetch(target, options) {
    const url = new URL(target, NO_PORT_BASE);
    const posted = options && options.body !== undefined;
    state.calls.push({ method: posted ? 'POST' : 'GET', path: url.pathname, body: posted ? options.body : null });
    if (state.arcDown && url.pathname === '/api/arc') return Promise.reject(new Error('connection refused'));
    const data = JSON.parse(JSON.stringify(answer(url.pathname)));
    return Promise.resolve({ status: 200, json: () => Promise.resolve(data) });
  }
  const count = (method, pathname) => state.calls.filter((call) => call.method === method && call.path === pathname).length;
  const posts = (pathname) => state.calls.filter((call) => call.method === 'POST' && call.path === pathname).map((call) => call.body);
  return { fetch, state, count, posts };
}

async function livePage(server) {
  freshPage();
  const win = makeWindow({ fetch: server.fetch, location: { protocol: 'http:', hostname: '127.0.0.1' } });
  await win.step(10, 100);
  check(text('modeBadge') === 'LIVE', 'the page went live');
  return win;
}

await test('live: the demo switches are hidden; the Upload button posts the session on show, and only that', async () => {
  const server = standIn();
  const win = await livePage(server);
  equal(['arcAuto', 'arcAutoLabel', 'arcDrop', 'arcDropLabel'].map((id) => $(id).hidden), [true, true, true, true]);
  check(server.count('GET', '/api/arc') >= 1, 'GET /api/arc is read in live mode');
  equal(text('arcLine'), 'ARC: no upload yet. Upload to ARC (demo) copies the session on show to the demo ARC folder.');
  checkLane('live, no upload', 0);
  click('btnArcUpload');
  await win.step(3, 100);
  equal(server.posts('/api/arc/upload'), [JSON.stringify({ session: SHOWN, transport: 'globus' })]);
  equal(text('arcLine'), 'Demo upload started. Nothing leaves this Mac.');
  choose('arcTransport', 'ssh');
  click('btnArcUpload');
  await win.step(3, 100);
  equal(server.posts('/api/arc/upload')[1], JSON.stringify({ session: SHOWN, transport: 'ssh' }));
  // An upload of the session exists already: what it does is said from its state.
  server.state.answers['/api/arc/upload'] = { ok: true, existing: true, upload: liveItem(1, 'transferring') };
  click('btnArcUpload');
  await win.step(3, 100);
  equal(text('arcLine'), 'This session is being uploaded already (Copying over SSH).');
  for (const [state, label] of [['conflict', 'Name already on ARC'], ['uncertain', 'Not sure Globus got it'], ['interrupted', 'Interrupted']]) {
    server.state.answers['/api/arc/upload'] = { ok: true, existing: true, upload: liveItem(2, state, { worker: { alive: false, action: null, terminal: false } }) };
    click('btnArcUpload');
    await win.step(3, 100);
    equal(text('arcLine'), 'This session has an upload already that waits for you: ' + label + '.', state);
  }
  expectClean('live upload button');
});

await test('demo: Upload to ARC (demo) for a session whose upload works says what that upload does', async () => {
  freshPage();
  const win = makeWindow({});
  await win.step(3);
  // The example's newest session is on show, and its upload is still being prepared.
  click('btnArcUpload');
  await win.step(1);
  check(/^This session is being uploaded already \((Waiting to start|Preparing the files|Signing in)\)\.$/.test(text('arcLine')), 'the line: ' + text('arcLine'));
  equal(parcelTitles().length, 2, 'no second upload');
  expectClean('demo existing upload');
});

await test('live: the buttons act on the newest demo upload; an upload from the window is drawn read-only', async () => {
  const server = standIn();
  server.state.arc.uploads = [liveItem(2, 'conflict', { progress: { files: 22, files_done: 22, bytes: 1, bytes_done: 1 } }), liveItem(1, 'verified')];
  const win = await livePage(server);
  await win.step(60, 100);
  equal(shownButtons(), ['btnArcRetry', 'btnArcKeep', 'btnArcStop']);
  checkLane('live with a name taken', 2);
  check($('arcLane').innerHTML.includes('_incoming</text>'), 'a checked copy waits under _incoming');
  for (const [button, endpoint] of [['btnArcKeep', '/api/arc/keep-copy'], ['btnArcRetry', '/api/arc/retry'], ['btnArcStop', '/api/arc/stop']]) {
    click(button);
    await win.step(3, 100);
    equal(server.posts(endpoint), [JSON.stringify({ upload: 'u000000000002' })], endpoint);
    equal(text('arcLine'), 'Demo: ' + endpoint.slice(9) + ' sent.', 'the answer is said in the line');
  }
  server.state.arc = Object.assign({}, server.state.arc, { uploads: [liveItem(3, 'verified'), liveItem(2, 'conflict')], active: 1 });
  await win.step(60, 100);
  equal(shownButtons(), ['btnArcCheck']);
  click('btnArcCheck');
  await win.step(3, 100);
  equal(server.posts('/api/arc/check'), [JSON.stringify({ upload: 'u000000000003' })]);
  server.state.arc = Object.assign({}, server.state.arc, { uploads: [liveItem(4, 'transferring', { demo: false, message: 'Copying with Globus: 12 of 22 files.' }), liveItem(3, 'verified')] });
  // The word about the last button press stays 6 s; then the newest upload is said again.
  await win.step(70, 100);
  equal(shownButtons(), [], 'an upload of the window is not acted on here');
  check(parcelTitles()[1].endsWith('\n(from the window)') && !parcelTitles()[0].includes('(from the window)'), 'the tool tip says where it came from');
  equal(text('arcLine'), 'ARC: Copying with Globus: 12 of 22 files.');
  equal($('arcLine').title, 'ARC: Copying with Globus: 12 of 22 files. (from the window)');
  expectClean('live buttons');
});

await test('live: an upload on ARC shows its newest Check status; one whose check failed (check_failed) is drawn as not ok', async () => {
  const server = standIn();
  const fine = { utc: '2026-11-15T10:05:00.000Z', ok: true, message: 'Demo: still in the demo folder: 22 files, every one matching.' };
  server.state.arc.uploads = [liveItem(1, 'verified', { last_check: fine })];
  const win = await livePage(server);
  await win.step(60, 100);
  equal(text('arcLine'), 'ARC (demo): checked on ' + localWhen(fine.utc) + ': still in the demo folder: 22 files, every one matching.');
  equal(parcels().map((parcel) => parcel.look), ['ok']);
  equal(parcelTitles()[0].split('\n').slice(1), ['Demo: On ARC, checked.', 'Checked on ' + localWhen(fine.utc) + ': still in the demo folder: 22 files, every one matching.']);
  // The data layer's check_failed (fix plan I1): verified, with the failed check's words as its message.
  const differs = { utc: '2026-11-15T10:07:00.000Z', ok: false, message: 'Demo: the folder on ARC differs from this upload: 1 differ, for example point_0002.json.' };
  const failed = (n, extra) => liveItem(n, 'verified', Object.assign({ display_state: 'check_failed', display_label: CHECK_FAILED, message: differs.message, last_check: differs }, extra || {}));
  server.state.arc = Object.assign({}, server.state.arc, { uploads: [failed(1)] });
  await win.step(60, 100);
  const said = 'checked on ' + localWhen(differs.utc) + ': the folder on ARC differs from this upload: 1 differ, for example point_0002.json.';
  equal(text('arcLine'), 'ARC (demo): ' + said);
  equal(parcels().map((parcel) => parcel.look), ['wait'], 'not drawn as checked');
  check(!$('arcLane').innerHTML.includes('class="arcmark"'), 'no check mark');
  check(parcels()[0].x >= 490, 'drawn in the ARC box');
  equal(parcelTitles()[0].split('\n').slice(1), ['C' + said.slice(1)], 'the tool tip says the failed check once');
  check(parcelTitles()[0].includes(' · ' + CHECK_FAILED + ' · '), 'the tool tip has the label');
  equal(shownButtons(), ['btnArcCheck'], 'Check status is offered again');
  // A data layer that still says verified beside a failed check: drawn the same.
  server.state.arc = Object.assign({}, server.state.arc, { uploads: [liveItem(1, 'verified', { last_check: differs })] });
  await win.step(60, 100);
  equal([parcels().map((parcel) => parcel.look), text('arcLine')], [['wait'], 'ARC (demo): ' + said]);
  // An upload of the window whose check failed: drawn the same, said without "(demo)", not acted on here.
  const gone = { utc: '2026-11-15T10:09:00.000Z', ok: false, message: 'The folder ' + FOLDER + '/2d_scan_20261115_100002_000 is no longer on ARC.' };
  server.state.arc = Object.assign({}, server.state.arc, { uploads: [failed(2, { demo: false, area: 'real', message: gone.message, last_check: gone, actions: ['check', 'copy_path'] }), failed(1)] });
  await win.step(60, 100);
  equal(text('arcLine'), 'ARC: checked on ' + localWhen(gone.utc) + ': ' + gone.message);
  equal(shownButtons(), []);
  equal(parcels().map((parcel) => parcel.look), ['wait', 'wait']);
  checkLane('live with failed checks', 2);
  expectClean('live checks');
});

await test('live: a check that could not look is a note with what to do; the upload stays on ARC (review REG-01)', async () => {
  const server = standIn();
  const note = { utc: '2026-11-15T10:11:00.000Z', ok: false, looked: false,
    message: 'Demo: something went wrong in the upload program (OSError). Check status could not look.',
    fix: 'Nothing was changed. Press Check status to look again.' };
  server.state.arc.uploads = [liveItem(1, 'verified', { last_check: note })];
  const win = await livePage(server);
  await win.step(60, 100);
  const said = 'checked on ' + localWhen(note.utc) + ': something went wrong in the upload program (OSError). Check status could not look. ' + note.fix;
  equal(text('arcLine'), 'ARC (demo): ' + said);
  equal(parcels().map((parcel) => parcel.look), ['ok'], 'drawn as on ARC, not as a failed check');
  check($('arcLane').innerHTML.includes('class="arcmark"'), 'the check mark stays');
  equal(parcelTitles()[0].split('\n').slice(1), ['Demo: On ARC, checked.', 'C' + said.slice(1)], 'the tool tip says why and what to do');
  equal(shownButtons(), ['btnArcCheck']);
  // A failed check that looked stays what is drawn when a later check could not look (check_failed).
  const differs = { utc: '2026-11-15T10:07:00.000Z', ok: false, looked: true, fix: null, message: 'Demo: the folder in the demo folder differs.' };
  server.state.arc = Object.assign({}, server.state.arc, { uploads: [liveItem(1, 'verified', { display_state: 'check_failed',
    display_label: CHECK_FAILED, message: differs.message, last_check: note })] });
  await win.step(60, 100);
  equal(parcels().map((parcel) => parcel.look), ['wait'], 'check_failed is drawn as not ok');
  expectClean('live check note');
});

await test('live: the word about Check status gives way to the check\'s outcome as soon as it is listed', async () => {
  const server = standIn();
  server.state.arc.uploads = [liveItem(1, 'verified')];
  const win = await livePage(server);
  await win.step(60, 100);
  server.state.answers['/api/arc/check'] = { ok: true, worker: 'detached', message: 'Demo: checking the upload. Nothing leaves this Mac.' };
  // arc-check starts the check worker before it answers, so the list read after the press shows it working.
  const working = { worker: { alive: true, action: 'check', terminal: false }, actions: ['stop'] };
  server.state.arc = Object.assign({}, server.state.arc, { uploads: [liveItem(1, 'verified', working)], active: 1 });
  click('btnArcCheck');
  await win.step(3, 100);
  equal(server.posts('/api/arc/check'), [JSON.stringify({ upload: 'u000000000001' })]);
  equal(text('arcLine'), 'Demo: checking the upload. Nothing leaves this Mac.');
  // The check works: Stop upload is offered, and the line says so once the word has had its 6 s.
  await win.step(20, 100);
  equal([text('arcLine'), shownButtons()], ['Demo: checking the upload. Nothing leaves this Mac.', ['btnArcStop']]);
  await win.step(50, 100);
  equal(text('arcLine'), 'ARC (demo): checking how this upload stands in the demo folder. Nothing is sent.');
  // A second press: the check works for a moment, then fails. Its outcome replaces the word at the
  // next read of the list, long before the word's 6 s are over.
  server.state.arc = Object.assign({}, server.state.arc, { uploads: [liveItem(1, 'verified')], active: 0 });
  await win.step(60, 100);
  equal(text('arcLine'), 'ARC (demo): On ARC, checked.');
  server.state.arc = Object.assign({}, server.state.arc, { uploads: [liveItem(1, 'verified', working)], active: 1 });
  click('btnArcCheck');
  await win.step(18, 100);
  equal(text('arcLine'), 'Demo: checking the upload. Nothing leaves this Mac.');
  const differs = { utc: '2026-11-15T10:07:00.000Z', ok: false, message: 'Demo: the folder on ARC differs from this upload: 1 differ, for example point_0002.json.' };
  server.state.arc = Object.assign({}, server.state.arc, { uploads: [liveItem(1, 'verified', { display_state: 'check_failed', display_label: CHECK_FAILED,
    message: differs.message, last_check: differs })], active: 0 });
  await win.step(15, 100);
  equal(text('arcLine'), 'ARC (demo): checked on ' + localWhen(differs.utc) + ': the folder on ARC differs from this upload: 1 differ, for example point_0002.json.',
    'the outcome, 3.3 s after the press');
  expectClean('live check word');
});

await test('live: a failed or missing /api/arc changes #arcLine only, never #status', async () => {
  const server = standIn();
  server.state.arc.uploads = [liveItem(1, 'verified')];
  const win = await livePage(server);
  await win.step(60, 100);
  const status = text('status');
  check(/^Live: /.test(status), 'the status line of live mode: ' + status);
  checkLane('live with one upload', 1);
  server.state.arc = { ok: false, error: 'The ARC settings are not valid: arc_upload.demo must be true or false.', detail: '',
    kind: 'config', reason: 'settings_invalid', fix: 'Correct them in ARC settings (command line: config --set key=value).' };
  await win.step(60, 100);
  equal(text('arcLine'), 'ARC uploads: The ARC settings are not valid: arc_upload.demo must be true or false. Correct them in ARC settings (command line: config --set key=value).');
  checkLane('live, last list kept', 1);
  server.state.arcDown = true;
  await win.step(60, 100);
  equal(text('arcLine'), 'ARC uploads: the server did not answer.');
  equal([text('status'), text('modeBadge')], [status, 'LIVE'], 'a silent /api/arc is not a silent server');
  server.state.arcDown = false;
  server.state.arc = { ok: false, error: 'not available', detail: "This installation has no 'arc-list' command." };
  await win.step(60, 100);
  equal(text('arcLine'), 'Uploads to ARC are not part of this installation.');
  server.state.arc = { ok: false, error: 'unknown endpoint /api/arc' };
  await win.step(60, 100);
  equal(text('arcLine'), 'ARC uploads: unknown endpoint /api/arc');
  equal(text('status'), status, 'none of it reached #status');
  server.state.arc = { ok: true, uploads: [liveItem(1, 'verified')], active: 0, unfinished: 0, settings: LIVE_SETTINGS };
  await win.step(60, 100);
  equal(text('arcLine'), 'ARC (demo): On ARC, checked.');
  // The answers of POST /api/arc/upload.
  server.state.answers['/api/arc/upload'] = { ok: false, kind: 'not_ready', reason: 'settling', retry_after_seconds: 41.2,
    error: 'This scan ended less than a minute ago, or is still being written.', detail: '', fix: 'Try again in 42 s.' };
  click('btnArcUpload');
  await win.step(3, 100);
  equal(text('arcLine'), 'This session can go in 42 s (the 60-second rule; the Studio cannot tell that the scan has ended).');
  // A session with an earlier upload: the data layer's fix names Recent uploads and --new, which only the
  // window has; the Studio says what can be done here, or that the window does it.
  const verification = { method: 'globus-checksum', algorithm: 'MD5', files: 22, utc: '2026-11-15T10:01:00.000Z' };
  const onArc = (extra) => liveItem(1, 'verified', Object.assign({ session_folder: SHOWN, verification, last_check: null }, extra || {}));
  const refused = (reason, upload) => ({ ok: false, kind: 'conflict', reason, detail: '', upload,
    error: reason === 'already_on_arc' ? SHOWN + ' is already on ARC: ' + FOLDER + '/simulated/' + SHOWN + ' (checked 2026-11-15 10:01 UTC).'
      : 'This session has an earlier upload that ended as ' + upload.display_label + '.',
    fix: reason === 'already_on_arc' ? 'To see that it is still there, select it under Recent uploads and press Check status. To upload it again beside it: --new.'
      : 'Press Retry same request on it, or start a new upload (command line: --new).' });
  const elsewhere = ' use ARC upload in the Data tab of the controller window.';
  const cases = [
    [refused('already_on_arc', onArc()), SHOWN + ' is already in the demo ARC folder (checked on ' + localWhen(verification.utc) +
      '), so it is not uploaded again. Press Check status to see that it is still there.'],
    // The newest check is the date quoted (fix plan I4).
    [refused('already_on_arc', onArc({ last_check: { utc: '2026-11-15T11:30:00.000Z', ok: true, message: 'Demo: still there.' } })),
      SHOWN + ' is already in the demo ARC folder (checked on ' + localWhen('2026-11-15T11:30:00.000Z') +
      '), so it is not uploaded again. Press Check status to see that it is still there.'],
    // A newer check that could not look saw nothing: the date is the verification's, as the data layer and the window
    // quote it (review R2-02).
    [refused('already_on_arc', onArc({ last_check: { utc: '2026-11-15T12:45:00.000Z', ok: false, looked: false,
      message: 'Demo: Globus may not list the copy in the demo folder. Check status could not look.',
      fix: 'Nothing was changed. Press Check status to look again.' } })),
      SHOWN + ' is already in the demo ARC folder (checked on ' + localWhen(verification.utc) +
      '), so it is not uploaded again. Press Check status to see that it is still there.'],
    // Not the newest upload in the strip, so its buttons are not here.
    [refused('already_on_arc', onArc({ upload_id: 'u0000000000a9' })), SHOWN + ' is already in the demo ARC folder (checked on ' +
      localWhen(verification.utc) + '), so it is not uploaded again. To check it, or to upload it again beside it,' + elsewhere],
    [refused('earlier_upload', liveItem(9, 'stopped')), 'This session has an earlier demo upload that ended as Stopped. To start a new upload of it,' + elsewhere]
  ];
  for (const [answer, expected] of cases) {
    server.state.answers['/api/arc/upload'] = answer;
    click('btnArcUpload');
    await win.step(3, 100);
    equal(text('arcLine'), expected, answer.reason);
    check(!/Recent uploads|--new/.test(text('arcLine')), 'the line names nothing the Studio lacks');
  }
  server.state.arc = { ok: true, uploads: [liveItem(9, 'failed')], active: 0, unfinished: 0, settings: LIVE_SETTINGS };
  await win.step(60, 100);
  server.state.answers['/api/arc/upload'] = refused('earlier_upload', liveItem(9, 'failed'));
  click('btnArcUpload');
  await win.step(3, 100);
  equal(text('arcLine'), 'This session has an earlier demo upload that ended as Failed. Press Retry same request to continue it.');
  server.state.answers['/api/arc/upload'] = { ok: false, error: 'not available', detail: "This installation has no 'arc-upload' command." };
  click('btnArcUpload');
  await win.step(3, 100);
  equal(text('arcLine'), 'Uploads to ARC are not part of this installation.');
  equal(text('status'), status, 'the answers of the strip\'s commands stay out of #status');
  expectClean('live failures');
});

await test('live: GET /api/arc every second while an upload works, else every 5 s; none in the demo', async () => {
  const server = standIn();
  server.state.arc = Object.assign({}, server.state.arc, { uploads: [liveItem(1, 'transferring')], active: 1 });
  const win = await livePage(server);
  await win.step(20, 100);
  let before = server.count('GET', '/api/arc');
  await win.step(100, 100);
  const busy = server.count('GET', '/api/arc') - before;
  check(busy >= 8 && busy <= 11, 'about ten reads in 10 s while an upload works, not ' + busy);
  server.state.arc = Object.assign({}, server.state.arc, { uploads: [liveItem(1, 'verified')], active: 0 });
  await win.step(20, 100);
  before = server.count('GET', '/api/arc');
  await win.step(200, 100);
  const idle = server.count('GET', '/api/arc') - before;
  check(idle >= 3 && idle <= 5, 'about four reads in 20 s otherwise, not ' + idle);
  click('btnDemo');
  await win.step(3);
  before = server.count('GET', '/api/arc');
  await win.step(100, 100);
  equal(server.count('GET', '/api/arc'), before, 'the demo reads nothing');
  equal(['arcAuto', 'arcAutoLabel', 'arcDrop', 'arcDropLabel'].map((id) => $(id).hidden), [false, false, false, false]);
  check(parcelTitles().length === 2 && /^ARC: /.test(text('arcLine')), 'the demo\'s own uploads are shown again');
  expectClean('live polling');
});

await test('live: an upload that waits for Terminal or a sign-in, and a button just pressed, are read every second (UI-04)', async () => {
  const server = standIn();
  // A live SSH upload from the window waits for Terminal: no program runs for it yet (active 0).
  const waiting = liveItem(1, 'waiting_for_terminal', { demo: false, needs: 'terminal', worker: { alive: false, action: null, terminal: false } });
  server.state.arc = Object.assign({}, server.state.arc, { uploads: [waiting], active: 0 });
  const win = await livePage(server);
  await win.step(20, 100);
  let before = server.count('GET', '/api/arc');
  await win.step(100, 100);
  let reads = server.count('GET', '/api/arc') - before;
  check(reads >= 8 && reads <= 11, 'about ten reads in 10 s while an upload waits for Terminal, not ' + reads);
  // A Globus upload waiting for its sign-in in the browser after the 2 minutes of display_state: the same.
  server.state.arc = Object.assign({}, server.state.arc, { uploads: [liveItem(2, 'stopped', { display_state: 'stopped', needs: 'browser' })], active: 0 });
  await win.step(60, 100);
  before = server.count('GET', '/api/arc');
  await win.step(100, 100);
  reads = server.count('GET', '/api/arc') - before;
  check(reads >= 8 && reads <= 11, 'about ten reads in 10 s while an upload waits for a sign-in, not ' + reads);
  // Nothing moves: every 5 s.
  server.state.arc = Object.assign({}, server.state.arc, { uploads: [liveItem(4, 'verified')], active: 0 });
  await win.step(60, 100);
  before = server.count('GET', '/api/arc');
  await win.step(200, 100);
  reads = server.count('GET', '/api/arc') - before;
  check(reads >= 3 && reads <= 5, 'about four reads in 20 s when nothing moves, not ' + reads);
  // Check status pressed: every second for the next 2 minutes (120 reads), then every 5 s again.
  click('btnArcCheck');
  await win.step(20, 100);
  before = server.count('GET', '/api/arc');
  await win.step(100, 100);
  reads = server.count('GET', '/api/arc') - before;
  check(reads >= 8 && reads <= 11, 'about ten reads in 10 s after a button, not ' + reads);
  await win.step(1200, 100);
  before = server.count('GET', '/api/arc');
  await win.step(200, 100);
  reads = server.count('GET', '/api/arc') - before;
  check(reads >= 3 && reads <= 5, 'every 5 s again 2 minutes after the button, not ' + reads);
  expectClean('live polling of uploads that wait');
});

await test('live: up to six parcels anywhere in the lane stay inside it and never overlap', async () => {
  const server = standIn();
  // An upload at work: the list is read every second.
  server.state.arc.active = 1;
  const win = await livePage(server);
  const long = 'a_session_folder_with_a_name_far_longer_than_any_the_controller_makes';
  const scenes = {
    'six on the laptop': [1, 2, 3, 4, 5, 6].map((n) => liveItem(n, ['queued', 'preparing', 'waiting_for_terminal', 'not_started', 'queued', 'preparing'][n - 1])),
    'six on the band at one spot': [1, 2, 3, 4, 5, 6].map((n) => liveItem(n, 'transferring', { progress: { files: 22, files_done: 11, bytes: 1, bytes_done: 0 } })),
    'six on the band near its end': [1, 2, 3, 4, 5, 6].map((n) => liveItem(n, 'transferring', { progress: { files: 22, files_done: 22 - n % 2, bytes: 1, bytes_done: 0 } })),
    'six along the band': [1, 2, 3, 4, 5, 6].map((n) => liveItem(n, ['signing_in', 'checking', 'submitting', 'uncertain', 'transferring', 'not_watched'][n - 1],
      { progress: { files: 22, files_done: n * 3, bytes: 1, bytes_done: 0 } })),
    'six on ARC': [1, 2, 3, 4, 5, 6].map((n) => liveItem(n, n % 2 ? 'verified' : 'placing')),
    'five on ARC and one in _incoming': [1, 2, 3, 4, 5, 6].map((n) => liveItem(n, n === 1 ? 'verifying' : 'verified')),
    'three in _incoming and three on ARC': [1, 2, 3, 4, 5, 6].map((n) => liveItem(n, n <= 3 ? 'verifying' : 'verified',
      n === 2 ? { state: 'conflict', display_state: 'conflict', display_label: 'Name already on ARC', progress: { files: 22, files_done: 22, bytes: 1, bytes_done: 1 } } : {})),
    'every place at once': [liveItem(1, 'queued'), liveItem(2, 'transferring', { progress: { files: 22, files_done: 5, bytes: 1, bytes_done: 0 } }),
      liveItem(3, 'failed', { progress: { files: 22, files_done: 11, bytes: 1, bytes_done: 0 }, leftover_on_arc: FOLDER + '/simulated/_incoming/x' }),
      liveItem(4, 'verifying'), liveItem(5, 'verified'), liveItem(6, 'stopped')],
    'eight uploads, six drawn': [1, 2, 3, 4, 5, 6, 7, 8].map((n) => liveItem(n, n % 3 ? 'verified' : 'interrupted')),
    'six on ARC, some with a failed check': [1, 2, 3, 4, 5, 6].map((n) => liveItem(n, 'verified', n % 2 ? {} : { display_state: 'check_failed',
      display_label: CHECK_FAILED, last_check: { utc: '2026-11-15T10:0' + n + ':00.000Z', ok: false, message: 'Demo: the folder is no longer there.' } })),
    'odd states and long names': [liveItem(1, 'unreadable', { session_folder: long }), liveItem(2, 'verified', { session_folder: long }),
      liveItem(3, 'failed', { session_folder: long }), liveItem(4, 'conflict', { session_folder: long }), liveItem(5, 'transferring', { session_folder: long }),
      liveItem(6, 'verified', { session_folder: undefined, upload_id: 'u0000000000ff' })]
  };
  for (const [name, uploads] of Object.entries(scenes)) {
    server.state.arc = { ok: true, uploads, active: 1, unfinished: 1,
      settings: Object.assign({}, LIVE_SETTINGS, { folder: '/projects/pcl_photonics/' + (name === 'odd states and long names' ? long : 'raman_globus_upload') }) };
    await win.step(15, 100);
    checkLane('live, ' + name, Math.min(6, uploads.length));
    if (name === 'six on ARC, some with a failed check') {
      equal(parcels().map((parcel) => parcel.look), ['wait', 'ok', 'wait', 'ok', 'wait', 'ok'], name);
      check(parcels().every((parcel) => parcel.x >= 490), name + ': all in the ARC box');
    }
  }
  check(/>ARC: …\/a_session_folder_wit…<\/text>/.test($('arcLane').innerHTML), 'a long ARC folder is cut to fit');
  expectClean('live lane layout');
});

console.log('\n' + passed + ' passed, ' + failures.length + ' failed');
if (failures.length) { console.log('failed: ' + failures.join('; ')); process.exit(1); }
