// biz-connect: Ask Claude, side panel (or popup window where the side panel API is missing).
// Holds one native-messaging connection to the host for as long as it is open. Shows the captured
// selection, runs Claude Code on it, streams the work, answers page-tool calls against the tab the
// selection came from, and puts permission prompts in front of the user.

const HOST = 'com.bizconnect.browser';
const PROTOCOL = 1;
const WRITE_TOOLS = new Set(['replace_selection', 'insert_after_selection']);
const $ = (id) => document.getElementById(id);

const S = {
  port: null, hello: null, ctx: null, itemId: null, lastAt: 0,
  session: false, running: false, sessionId: null, contextChanged: false,
  autoRun: false, windowId: null, popup: location.search.includes('popup=1'),
};

// ------------------------------------------------------------------------ rendering
function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function inline(s) {
  return esc(s)
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<b>$1</b>')
    .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
}

function md(src) {
  let html = '';
  String(src).split(/```[^\n]*\n?/).forEach((part, i) => {
    if (i % 2) {
      html += `<pre><code>${esc(part.replace(/\n$/, ''))}</code></pre>`;
      return;
    }
    for (const block of part.split(/\n{2,}/)) {
      const b = block.trim();
      if (!b) continue;
      const lines = b.split('\n');
      const bullet = /^\s*([-*]|\d+\.)\s+/;
      if (lines.every((l) => bullet.test(l))) {
        html += `<ul>${lines.map((l) => `<li>${inline(l.replace(bullet, ''))}</li>`).join('')}</ul>`;
      } else {
        html += `<p>${lines.map((l) => (/^#+\s/.test(l) ? `<b>${inline(l.replace(/^#+\s/, ''))}</b>` : inline(l))).join('<br>')}</p>`;
      }
    }
  });
  return html;
}

function add(el) {
  const log = $('log');
  const near = log.scrollHeight - log.scrollTop - log.clientHeight < 80;
  log.appendChild(el);
  if (near) log.scrollTop = log.scrollHeight;
  return el;
}

function div(cls, html) {
  const d = document.createElement('div');
  d.className = cls;
  d.innerHTML = html;
  return d;
}

const note = (level, text) => add(div(`note ${level}`, inline(text)));
const activity = (cls, name, detail) => add(div(`act ${cls}`, `<b>${esc(name)}</b><span title="${esc(detail || '')}">${esc(detail || '')}</span>`));

function banner(html) {
  $('banner').hidden = !html;
  $('banner').innerHTML = html || '';
}

function siteLabel(ctx) {
  if (ctx.site === 'notion') return 'Notion';
  if (ctx.site === 'google-doc') return 'Google Doc';
  try { return new URL(ctx.url).hostname.replace(/^www\./, ''); } catch (e) { return 'Page'; }
}

function renderContext() {
  const ctx = S.ctx;
  $('context').hidden = !ctx;
  $('empty').hidden = !!ctx;
  if (!ctx) return;
  $('site').textContent = siteLabel(ctx);
  $('site').className = `badge ${ctx.site || 'web'}`;
  $('page').textContent = ctx.title || ctx.url || '';
  $('page').title = ctx.url || '';
  const sel = $('selection');
  sel.classList.remove('open');
  if (ctx.selection) {
    sel.textContent = ctx.selection;
    sel.classList.remove('none');
  } else {
    sel.textContent = 'Nothing selected: Claude gets the page.';
    sel.classList.add('none');
  }
  const needPaste = !ctx.selection && ctx.site === 'google-doc';
  $('paste').hidden = !needPaste;
  if (needPaste) $('paste').value = '';
  const a = ctx.anchor || {};
  let anchor = '';
  if (ctx.site === 'notion') {
    anchor = a.notion_block_link ? `Block: ${a.notion_block_link}` : 'No Notion block found under the selection; Claude will find the passage by its text.';
  } else if (ctx.site === 'google-doc') {
    anchor = a.doc_id ? `Doc ${a.doc_id}: edits go through biz-connect, in place.` : '';
  } else if (ctx.selection) {
    anchor = ctx.editable ? 'Editable: Claude can write into this selection.' : 'Read-only here: Claude will reply with text.';
  }
  if (ctx.capture_error) anchor = `Couldn't read this page (${ctx.capture_error}).`;
  $('anchor').textContent = anchor;
}

function renderChips() {
  const chips = $('chips');
  chips.innerHTML = '';
  for (const it of (S.hello && S.hello.menu) || []) {
    if (it.sites && S.ctx && !it.sites.includes(S.ctx.site || 'web') && !it.sites.some((s) => s.includes('://'))) continue;
    const c = document.createElement('button');
    c.className = `chip${S.itemId === it.id ? ' on' : ''}`;
    c.textContent = it.label;
    c.title = it.skill ? `Runs the ${it.skill} skill` : it.prompt || '';
    c.onclick = () => { S.itemId = S.itemId === it.id ? null : it.id; renderChips(); };
    chips.appendChild(c);
  }
}

function renderState() {
  const dot = $('dot');
  dot.className = `dot ${!S.port ? 'off' : S.running ? 'busy' : 'on'}`;
  dot.title = !S.port ? 'Bridge not connected' : S.running ? 'Working' : 'Ready';
  $('send').textContent = S.session && !S.contextChanged ? 'Send' : 'Run';
  $('send').disabled = S.running || !S.port;
  $('stop').hidden = !S.running;
  const has = !!S.sessionId;
  $('vscode').hidden = !has;
  $('copysid').hidden = !has;
  $('newrun').hidden = !S.session || S.running;
  $('input').placeholder = S.session && !S.contextChanged ? 'Follow up…' : 'What should Claude do with this?';
}

// ------------------------------------------------------------------------ host link
function connect() {
  try {
    S.port = chrome.runtime.connectNative(HOST);
  } catch (e) {
    S.port = null;
    hostMissing(String(e.message || e));
    return;
  }
  S.port.onMessage.addListener(onHost);
  S.port.onDisconnect.addListener(() => {
    const err = chrome.runtime.lastError && chrome.runtime.lastError.message;
    S.port = null;
    const wasRunning = S.running;
    S.running = false;
    S.session = false;
    if (err && /not found|forbidden/i.test(err)) hostMissing(err);
    else {
      banner(`The bridge closed${err ? ` (${esc(err)})` : ''}. <button class="link" id="reconnect">Reconnect</button>`);
      $('reconnect').onclick = () => { banner(''); connect(); };
      if (wasRunning) note('error', 'The run was cut off. Check ~/.config/biz-connect/browser/host.log.');
    }
    renderState();
  });
  S.port.postMessage({ type: 'hello' });
  renderState();
}

function hostMissing(err) {
  banner(`The biz-connect bridge isn't installed for this browser (${esc(err)}).<br>`
    + 'In your repo, run <code>bizconnect browser install</code>, then reopen this panel.');
}

function send(obj) {
  if (!S.port) {
    note('error', 'The bridge is not connected.');
    return false;
  }
  S.port.postMessage(obj);
  return true;
}

async function onHost(msg) {
  switch (msg.type) {
    case 'hello': {
      S.hello = msg;
      const sel = $('workspace');
      sel.innerHTML = '';
      for (const w of msg.workspaces || []) {
        const o = document.createElement('option');
        o.value = w.name;
        o.textContent = w.name;
        o.title = w.path;
        o.selected = w.name === msg.workspace;
        sel.appendChild(o);
      }
      const probs = [];
      if (msg.protocol !== PROTOCOL) probs.push('The extension and the bridge are different versions: run <code>bizconnect browser install</code> and reload the extension.');
      if (!(msg.workspaces || []).length) probs.push('No workspace yet: in your repo, run <code>bizconnect browser add</code>.');
      for (const p of msg.problems || []) probs.push(`connections.yaml: ${esc(p)}`);
      banner(probs.join('<br>'));
      chrome.runtime.sendMessage({ type: 'set-menu', items: msg.menu || [] }).catch(() => {});
      renderChips();
      renderState();
      if (S.autoRun) {
        S.autoRun = false;
        run();
      }
      break;
    }
    case 'status':
      activity('', 'status', msg.text);
      break;
    case 'started':
      S.sessionId = msg.session_id;
      $('meta').textContent = [msg.model, msg.permission_mode && `${msg.permission_mode} mode`].filter(Boolean).join(' · ');
      break;
    case 'text':
      add(div('msg assistant', md(msg.text)));
      break;
    case 'tool':
      if (!msg.name.startsWith('mcp__page__')) {           // page tools get their own line below
        activity('', msg.name.replace(/^mcp__/, '').replace(/__/g, ' › '), msg.detail);
      }
      break;
    case 'notice':
      note(msg.level || 'warn', msg.text);
      break;
    case 'page_tool':
      await pageTool(msg);
      break;
    case 'permission':
      permissionCard(msg);
      break;
    case 'result': {
      S.running = false;
      S.sessionId = msg.session_id || S.sessionId;
      const secs = Math.round((msg.duration_ms || 0) / 1000);
      if (msg.is_error) note('error', `Claude stopped: ${(msg.errors || []).join('; ') || msg.subtype}`);
      activity('', 'done', `${secs}s · ${msg.turns} turn${msg.turns === 1 ? '' : 's'}`);
      break;
    }
    case 'error':
      note('error', msg.text);
      S.running = false;
      break;
    case 'ended':
      S.session = false;
      S.sessionId = null;
      break;
    default:
      break;
  }
  renderState();
}

// ------------------------------------------------------------------------ page tools
function stripHash(u) {
  return String(u || '').split('#')[0];
}

async function pageTool(msg) {
  const write = WRITE_TOOLS.has(msg.name);
  const label = { read_selection: 'read selection', read_page: 'read page', replace_selection: 'replace selection', insert_after_selection: 'insert text' }[msg.name] || msg.name;
  activity('page', `page › ${label}`, write ? (msg.args && msg.args.text) || '' : '');
  let res;
  try {
    if (!S.ctx || S.ctx.tabId === undefined) throw new Error('No tab is attached to this session.');
    const tab = await chrome.tabs.get(S.ctx.tabId);
    if (stripHash(tab.url) !== stripHash(S.ctx.url)) throw new Error('The tab has moved to another page since the selection was captured.');
    const [r] = await chrome.scripting.executeScript({
      target: { tabId: S.ctx.tabId, frameIds: [S.ctx.frameId || 0] },
      func: (name, args) => (window.__bizc ? window.__bizc.tool(name, args)
        : { ok: false, error: 'The page was reloaded since the selection was captured. Ask the user to select the passage again.' }),
      args: [msg.name, msg.args || {}],
    });
    res = (r && r.result) || { ok: false, error: 'No answer from the page.' };
  } catch (e) {
    res = { ok: false, error: String((e && e.message) || e) };
  }
  send({ type: 'page_result', id: msg.id, ok: !!res.ok, result: res.result, error: res.error });
  if (write) {
    if (res.ok) note('ok', 'Edited the page. Ctrl+Z in the page undoes it.');
    else note('warn', `Page edit not applied: ${res.error}`);
  }
}

function permissionCard(msg) {
  const card = add(div('perm', `<div class="t">${esc(msg.title || `Allow ${msg.tool}?`)}</div>`
    + `${msg.detail ? `<div class="d">${esc(msg.detail)}</div>` : ''}`
    + '<div class="row"><button class="ok">Allow</button><button class="secondary no">Deny</button></div>'));
  const answer = (allow) => {
    send({ type: 'permission_reply', id: msg.id, allow });
    card.classList.add('done');
    card.querySelector('.row').innerHTML = `<span class="meta">${allow ? 'Allowed' : 'Denied'}</span>`;
  };
  card.querySelector('.ok').onclick = () => answer(true);
  card.querySelector('.no').onclick = () => answer(false);
}

// ------------------------------------------------------------------------------ runs
function run() {
  const text = $('input').value.trim();
  if (S.running) return;
  if (!S.hello) {
    S.autoRun = true;                    // run as soon as the bridge says hello
    return;
  }
  const fresh = !S.session || S.contextChanged;
  const item = ((S.hello && S.hello.menu) || []).find((m) => m.id === S.itemId);
  if (fresh) {
    if (!text && !item) {
      $('input').focus();
      return;
    }
    const ctx = { ...(S.ctx || {}) };
    if (!$('paste').hidden && $('paste').value.trim()) ctx.selection = $('paste').value.trim();
    if (S.session) $('log').innerHTML = '';
    if (!send({ type: 'run', workspace: $('workspace').value || undefined, context: ctx, instruction: text, item_id: item ? item.id : undefined })) return;
    add(div('msg user', esc([item && `[${item.label}]`, text].filter(Boolean).join(' '))));
    S.session = true;
    S.contextChanged = false;
    S.sessionId = null;
  } else {
    if (!text) return;
    if (!send({ type: 'followup', text })) return;
    add(div('msg user', esc(text)));
  }
  S.running = true;
  $('input').value = '';
  renderState();
}

function applyPending(p) {
  if (!p || !p.ctx || p.at <= S.lastAt) return;
  if (!S.popup && S.windowId !== null && p.ctx.windowId !== S.windowId) return;
  S.lastAt = p.at;
  S.ctx = p.ctx;
  S.itemId = p.itemId || null;
  if (S.session) S.contextChanged = true;
  renderContext();
  renderChips();
  renderState();
  if (S.itemId && !S.running) run();
  else $('input').focus();
}

// ---------------------------------------------------------------------------- wiring
$('send').onclick = run;
$('input').addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
    e.preventDefault();
    run();
  }
});
$('stop').onclick = () => send({ type: 'stop' });
$('newrun').onclick = () => {
  send({ type: 'end' });
  $('log').innerHTML = '';
  S.session = false;
  S.sessionId = null;
  S.contextChanged = false;
  renderState();
};
$('vscode').onclick = () => send({ type: 'open_vscode', session_id: S.sessionId, workspace: $('workspace').value });
$('copysid').onclick = async () => {
  try {
    await navigator.clipboard.writeText(`claude --resume ${S.sessionId}`);
    note('ok', 'Copied. Run it in a terminal in the workspace folder.');
  } catch (e) {
    note('warn', `claude --resume ${S.sessionId}`);
  }
};
$('workspace').onchange = () => send({ type: 'hello', workspace: $('workspace').value });
$('selection').onclick = () => $('selection').classList.toggle('open');
$('recapture').onclick = async () => {
  let windowId = S.windowId;
  if (S.popup) {
    const w = await chrome.windows.getLastFocused({ windowTypes: ['normal'] }).catch(() => null);
    windowId = w ? w.id : (S.ctx && S.ctx.windowId);
  }
  chrome.runtime.sendMessage({ type: 'recapture', windowId }).catch(() => {});
};

(async () => {
  try {
    if (!S.popup) S.windowId = (await chrome.windows.getCurrent()).id;
  } catch (e) { /* popup or detached */ }
  chrome.storage.onChanged.addListener((changes, area) => {
    if (area === 'session' && changes.pending) applyPending(changes.pending.newValue);
  });
  connect();
  const { pending } = await chrome.storage.session.get('pending');
  if (pending && Date.now() - pending.at < 60000) applyPending(pending);
  renderContext();
  renderState();
})();
