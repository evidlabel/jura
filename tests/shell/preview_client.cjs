const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../../src/workbench/write/page.html'), 'utf8');
const between = (start, end) => source.slice(source.indexOf(start), source.indexOf(end, source.indexOf(start)));
const PV_IDLE = Number(/const PV_IDLE = (\d+);/.exec(source)[1]);  // the typing pause the preview waits for
const PV_STUCK = Number(/const PV_STUCK = (\d+);/.exec(source)[1]);  // how long a compile status is waited for
const nodes = new Map(), requests = [], timers = new Map(), messages = [];
let now = 0, nextTimer = 0, text = '', connections = 0;
const W = {panes: {preview: true}, pv: {}}, S = {file: {path: 'a.typ', kind: 'text'}};
const context = vm.createContext({
  W, S, document: {hidden:false}, clearTimeout: id => timers.delete(id), CFG: {tinymist: true},
  $: selector => { if (!nodes.has(selector)) nodes.set(selector, {}); return nodes.get(selector); },
  PV_IDLE, PV_STUCK, performance: {now: () => now}, editor: null,
  isTyp: p => p.endsWith('.typ'), pvLive: ws => ws && ws.readyState <= 1,
  pvMsg: message => messages.push(message), pvError: () => {},
  pvConnect: () => { connections++; W.pv.ws = {readyState: 1}; },
  absPath: p => '/case/' + p, docText: () => text,
  api: () => new Promise((resolve, reject) => requests.push({resolve, reject})),
  setTimeout: (fn, delay) => { const id = ++nextTimer; timers.set(id, {fn, at: now + delay}); return id; },
});
vm.runInContext(between('async function syncPreview()', 'function pvConnect()') +
  between('function pvSendBuffer(force)', 'const PV_IDLE') +
  between('function pvScrollLater(force)', 'function mdSend()') +
  between('function pvCompileDone(ws)', "$('#pv-refresh-mode').value = load") +
  between('function pvScheduleBuffer()', "editor.addEventListener('input'"), context);
const response = id => ({data: 'http://' + id, control: 'ws://' + id});
function advance(ms) {
  const end = now + ms;
  while (true) {
    const next = [...timers].sort((a, b) => a[1].at - b[1].at)[0];
    if (!next || next[1].at > end) break;
    now = next[1].at; timers.delete(next[0]); next[1].fn();
  }
  now = end;
}
(async () => {
  context.$('#pv-refresh-mode').value = 'typing';
  const a = context.syncPreview();
  await context.syncPreview();
  assert.equal(requests.length, 1, 'concurrent opens share startup');
  S.file.path = 'b.typ'; const b = context.syncPreview();
  S.file.path = 'a.typ'; const again = context.syncPreview();
  requests[0].resolve(response('old-a')); await a;
  assert.equal(connections, 0, 'old A response cannot replace a newer A request');
  requests[1].reject(new Error('obsolete B')); await b;
  assert.equal(W.pv.path, 'a.typ', 'obsolete failures cannot reset current preview');
  requests[2].resolve(response('new-a')); await again;
  assert.equal(connections, 1);
  assert.equal(nodes.get('#pvframe').src, 'http://new-a');
  await context.syncPreview();
  assert.equal(requests.length, 3, 'ready preview is reused');
  S.file.path = 'c.typ'; const failed = context.syncPreview();
  requests[3].reject(new Error('failed')); await failed;
  const retry = context.syncPreview();
  requests[4].resolve(response('c')); await retry;
  assert.equal(nodes.get('#pvframe').src, 'http://c');

  const sent = [];
  W.pv.ws = {readyState: 1, path: 'c.typ', send: msg => sent.push({at: now, ...JSON.parse(msg)})};
  for (let i = 0; i < 40; i++) {
    text = 'edit ' + i; context.pvScheduleBuffer(); advance(50);
  }
  assert.equal(sent.length, 0, 'typing must not continuously compile and repaint');
  advance(PV_IDLE);
  assert.equal(sent.length, 1);
  assert.equal(sent.at(-1).files['/case/c.typ'], 'edit 39', 'latest edit is delivered after the pause');
  text = 'while compiling'; context.pvScheduleBuffer(); advance(PV_IDLE);
  assert.equal(sent.length, 1, 'only one compilation is in flight');
  text = 'newest while compiling'; context.pvSendBuffer();
  context.pvCompileDone(W.pv.ws); advance(PV_IDLE);
  assert.equal(sent.length, 2);
  assert.equal(sent.at(-1).files['/case/c.typ'], text, 'intermediate edits are coalesced');
  context.pvCompileDone(W.pv.ws);
  const count = sent.length; context.pvScheduleBuffer(); advance(PV_IDLE);
  assert.equal(sent.length, count, 'unchanged text does not trigger recompilation');
  W.panes.preview = false; text = 'hidden edit'; context.pvScheduleBuffer(); advance(1000); context.pvSendBuffer();
  assert.equal(sent.length, count, 'hidden preview does no compilation work');
  W.panes.preview = true; context.pvSendBuffer();
  assert.equal(sent.length, count + 1, 'reopening catches up to the current buffer');
  context.pvCompileDone(W.pv.ws);
  context.$('#pv-refresh-mode').value = 'save';
  const beforeSaveMode = sent.length;
  context.pvSendBuffer();
  assert.equal(sent.at(-1).event, 'removeMemoryFiles', 'save mode releases memory so disk saves compile');
  text = 'unsaved large bibliography edit'; context.pvScheduleBuffer(); advance(2000); context.pvSendBuffer();
  assert.equal(sent.length, beforeSaveMode + 1, 'save mode sends no unsaved text');
  context.pvSendBuffer(true);
  assert.equal(sent.at(-1).files['/case/c.typ'], text, 'explicit refresh still previews unsaved changes');
  context.pvCompileDone(W.pv.ws);
  S.file.path = 'other.typ'; context.pvSendBuffer();
  assert.equal(sent.at(-1).event, 'removeMemoryFiles', 'leaving a file releases its memory override');
  // tinymist does not answer a first copy that equals the disk: that must not stall later edits
  context.$('#pv-refresh-mode').value = 'typing'; S.file.path = 'd.typ'; S.file.original = 'on disk';
  W.pv.ws = {readyState: 1, path: 'd.typ', send: msg => sent.push({at: now, ...JSON.parse(msg)})};
  text = 'on disk'; context.pvSendBuffer();
  assert.equal(W.pv.ws.compiling, false, 'an unchanged first copy is not waited for');
  text = 'changed'; context.pvScheduleBuffer(); advance(PV_IDLE);
  assert.equal(sent.at(-1).files['/case/d.typ'], 'changed', 'the next edit still goes over');
  assert.equal(W.pv.ws.compiling, true);
  text = 'changed again'; context.pvScheduleBuffer(); advance(PV_IDLE);
  assert.equal(sent.at(-1).files['/case/d.typ'], 'changed', 'waits while compiling');
  advance(PV_STUCK);
  assert.equal(sent.at(-1).files['/case/d.typ'], 'changed again', 'a lost compile status does not stall the preview');
  delete S.file.original; context.pvCompileDone(W.pv.ws);
  // scroll sync: each scroll costs the preview a render, so not while typing, and once per line
  context.$('#pv-refresh-mode').value = 'typing'; S.file.path = W.pv.path = 'c.typ';
  W.pv.ws.compiling = false; clearTimeout(W.pv.sendT); W.pv.sendT = null;
  let caret = 0; const doc = 'one\ntwo\nthree';
  context.editor = {get selectionStart() { return caret; }, view: {state: {doc: {lineAt: at => {
    const from = doc.lastIndexOf('\n', at - 1) + 1; return {number: doc.slice(0, from).split('\n').length, from}; }}}}};
  const scrolls = () => sent.filter(m => m.event === 'panelScrollTo').map(m => m.line);
  for (let i = 0; i < 10; i++) { W.pv.typedAt = now; context.pvScrollLater(false); advance(300); }
  assert.deepEqual(scrolls(), [], 'no preview scroll while typing');
  advance(PV_IDLE);
  assert.deepEqual(scrolls(), [0], 'one scroll after the pause');
  context.pvScrollLater(false); advance(PV_IDLE);
  assert.deepEqual(scrolls(), [0], 'the same line is not scrolled to again');
  caret = 5; context.pvScrollLater(false); advance(PV_IDLE);
  assert.deepEqual(scrolls(), [0, 1], 'a new line scrolls');
  context.pvScrollLater(true); advance(300);
  assert.deepEqual(scrolls(), [0, 1, 1], 'a click always syncs');
  console.log('Preview startup races and continuous edit delivery passed.');
})().catch(error => { console.error(error); process.exitCode = 1; });
