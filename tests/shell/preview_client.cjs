const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../../src/workbench/write/page.html'), 'utf8');
const between = (start, end) => source.slice(source.indexOf(start), source.indexOf(end, source.indexOf(start)));
const nodes = new Map(), requests = [], timers = new Map(), messages = [];
let now = 0, nextTimer = 0, text = '', connections = 0;
const W = {panes: {preview: true}, pv: {}}, S = {file: {path: 'a.typ', kind: 'text'}};
const context = vm.createContext({
  W, S, document: {hidden:false}, clearTimeout: id => timers.delete(id), CFG: {tinymist: true},
  $: selector => { if (!nodes.has(selector)) nodes.set(selector, {}); return nodes.get(selector); },
  isTyp: p => p.endsWith('.typ'), pvLive: ws => ws && ws.readyState <= 1,
  pvMsg: message => messages.push(message), pvError: () => {},
  pvConnect: () => { connections++; W.pv.ws = {readyState: 1}; },
  absPath: p => '/case/' + p, docText: () => text,
  api: () => new Promise((resolve, reject) => requests.push({resolve, reject})),
  setTimeout: (fn, delay) => { const id = ++nextTimer; timers.set(id, {fn, at: now + delay}); return id; },
});
vm.runInContext(between('async function syncPreview()', 'function pvConnect()') +
  between('function pvSendBuffer(force)', 'function pvScroll()') +
  between('function pvCompileDone(ws)', "$('#pv-refresh-mode').value = load") +
  between('function pvScheduleBuffer()', "editor.addEventListener('input', pvScheduleBuffer)"), context);
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
  advance(400);
  assert.equal(sent.length, 1);
  assert.equal(sent.at(-1).files['/case/c.typ'], 'edit 39', 'latest edit is delivered after the pause');
  text = 'while compiling'; context.pvScheduleBuffer(); advance(400);
  assert.equal(sent.length, 1, 'only one compilation is in flight');
  text = 'newest while compiling'; context.pvSendBuffer();
  context.pvCompileDone(W.pv.ws); advance(400);
  assert.equal(sent.length, 2);
  assert.equal(sent.at(-1).files['/case/c.typ'], text, 'intermediate edits are coalesced');
  context.pvCompileDone(W.pv.ws);
  const count = sent.length; context.pvScheduleBuffer(); advance(400);
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
  console.log('Preview startup races and continuous edit delivery passed.');
})().catch(error => { console.error(error); process.exitCode = 1; });
