// biz-connect: Ask Claude, service worker.
// Owns the right-click menu, the keyboard shortcut and the toolbar button. Each trigger opens the
// side panel (inside the user gesture, before any await), captures the selection from the tab,
// and leaves it in storage.session for the panel to pick up. The panel talks to the native host.

const HOST = 'com.bizconnect.browser';
const ASK_ID = 'bizc-ask';
const SITE_PATTERNS = {
  notion: ['*://*.notion.so/*', '*://notion.so/*', '*://*.notion.com/*', '*://notion.com/*', '*://*.notion.site/*'],
  'google-doc': ['*://docs.google.com/document/*'],
};

// ------------------------------------------------------------------- right-click menu
async function buildMenu(items) {
  if (!items) {
    try {
      const reply = await chrome.runtime.sendNativeMessage(HOST, { type: 'menu' });
      items = (reply && reply.menu) || [];
    } catch (e) {
      items = [];                       // host not installed yet: the panel explains how
    }
  }
  await chrome.contextMenus.removeAll();
  const contexts = ['selection', 'page', 'editable', 'link', 'image', 'video', 'audio'];
  chrome.contextMenus.create({ id: ASK_ID, title: 'Ask Claude…', contexts });
  for (const it of items) {
    const props = { id: 'bizc-item:' + it.id, title: it.label, contexts };
    const pats = (it.sites || []).flatMap((s) => SITE_PATTERNS[s] || (s.includes('://') ? [s] : []));
    if (pats.length) props.documentUrlPatterns = pats;
    try {
      chrome.contextMenus.create(props);
    } catch (e) {
      console.warn('menu item skipped', it, e);
    }
  }
}

chrome.runtime.onInstalled.addListener(() => buildMenu());
chrome.runtime.onStartup.addListener(() => buildMenu());

// --------------------------------------------------------------------------- triggers
function openPanel(tab) {
  // Must run synchronously inside the user gesture. Browsers without the side panel API get a
  // popup window instead.
  if (chrome.sidePanel && chrome.sidePanel.open) {
    return chrome.sidePanel.open({ windowId: tab.windowId }).catch(() => openPopup());
  }
  return openPopup();
}

async function openPopup() {
  const { popupId } = await chrome.storage.session.get('popupId');
  if (popupId) {
    try {
      await chrome.windows.update(popupId, { focused: true });
      return;
    } catch (e) { /* closed */ }
  }
  const w = await chrome.windows.create({
    url: chrome.runtime.getURL('panel.html?popup=1'), type: 'popup', width: 460, height: 780,
  });
  await chrome.storage.session.set({ popupId: w.id });
}

async function inject(tabId, allFrames) {
  const target = allFrames ? { tabId, allFrames: true } : { tabId, frameIds: [0] };
  await chrome.scripting.executeScript({ target, files: ['capture.js'] });
  return chrome.scripting.executeScript({ target, func: () => (window.__bizc ? window.__bizc.capture() : null) });
}

// What the browser says was right-clicked: the fallback when target.js isn't in the page yet
// (a tab opened before the extension was installed or reloaded).
function targetFromInfo(info) {
  if (!info) return null;
  const url = info.srcUrl || info.linkUrl;
  if (!url) return null;
  const kind = info.mediaType || 'link';
  return { kind, url, name: '', text: info.linkText || '' };
}

async function capture(tab, itemId, selectionText, info) {
  let ctx = null;
  try {
    let results;
    try {
      results = await inject(tab.id, true);
    } catch (e) {
      results = await inject(tab.id, false);
    }
    const ok = results.filter((r) => r && r.result);
    const pick = ok.find((r) => r.result.selection && r.frameId === 0)
      || ok.find((r) => r.result.selection)
      || ok.find((r) => r.frameId === 0) || ok[0];
    if (pick) ctx = { ...pick.result, frameId: pick.frameId };
  } catch (e) {
    ctx = { capture_error: String((e && e.message) || e) };
  }
  ctx = { url: tab.url, title: tab.title, selection: '', ...(ctx || {}) };
  if (!ctx.selection && selectionText) ctx.selection = selectionText;
  if (!ctx.target) ctx.target = targetFromInfo(info);
  ctx.tabId = tab.id;
  ctx.windowId = tab.windowId;
  await chrome.storage.session.set({ pending: { ctx, itemId: itemId || null, at: Date.now() } });
}

function trigger(tab, itemId, selectionText, info) {
  if (!tab || tab.id === undefined) return;
  openPanel(tab);
  capture(tab, itemId, selectionText, info);
}

chrome.contextMenus.onClicked.addListener((info, tab) => {
  const id = String(info.menuItemId);
  const itemId = id.startsWith('bizc-item:') ? id.slice('bizc-item:'.length) : null;
  trigger(tab, itemId, info.selectionText, info);
});

chrome.commands.onCommand.addListener((command, tab) => {
  if (command === 'ask-claude') trigger(tab, null, '');
});

chrome.action.onClicked.addListener((tab) => trigger(tab, null, ''));

// ------------------------------------------------------------------- panel requests
chrome.runtime.onMessage.addListener((msg, sender, reply) => {
  if (msg && msg.type === 'set-menu') {
    buildMenu(msg.items || []).then(() => reply({ ok: true }));
    return true;
  }
  if (msg && msg.type === 'recapture') {
    chrome.tabs.query({ active: true, windowId: msg.windowId }).then(([tab]) => {
      if (tab) capture(tab, null, '').then(() => reply({ ok: true }));
      else reply({ ok: false });
    });
    return true;
  }
  return false;
});
