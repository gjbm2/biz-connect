// biz-connect: Ask Claude, content script (injected on demand, in the extension's isolated world).
// capture(): the selection, its surroundings, and a stable anchor for the site.
// tool(name, args): the page tools Claude calls through the native host. They act only on the
// selection captured here. They write text only: no click, submit or navigate.
(() => {
  if (window.__bizc) return;

  const MAX_SEL = 20000;
  const MAX_AROUND = 2000;
  const state = { saved: null };

  function siteOf(loc) {
    const h = loc.hostname;
    if (h === 'notion.so' || h === 'notion.com' || /\.notion\.(so|com|site)$/.test(h)) return 'notion';
    if (h === 'docs.google.com' && loc.pathname.startsWith('/document/')) return 'google-doc';
    return 'web';
  }

  function elementOf(node) {
    return node && (node.nodeType === 1 ? node : node.parentElement);
  }

  function editableHost(node) {
    let el = elementOf(node);
    if (!el || !el.isContentEditable) return null;
    while (el.parentElement && el.parentElement.isContentEditable) el = el.parentElement;
    return el;
  }

  function blockOf(node) {
    const el = elementOf(node);
    return el && el.closest('[data-block-id],p,li,td,th,blockquote,pre,h1,h2,h3,h4,h5,h6,article,section,div');
  }

  function isTextField(el) {
    if (!el) return false;
    if (el.tagName === 'TEXTAREA') return true;
    return el.tagName === 'INPUT' && /^(text|search|email|url|tel|)$/i.test(el.type || '');
  }

  function quote(range, block) {
    try {
      const root = block || document.body;
      const pre = document.createRange();
      pre.setStart(root, 0);
      pre.setEnd(range.startContainer, range.startOffset);
      const post = document.createRange();
      post.setStart(range.endContainer, range.endOffset);
      post.setEnd(root, root.childNodes.length);
      return { prefix: pre.toString().slice(-48), suffix: post.toString().slice(0, 48) };
    } catch (e) {
      return {};
    }
  }

  function notionAnchor(range) {
    const params = new URLSearchParams(location.search);
    const pageMatch = (params.get('p') || location.pathname).match(/([0-9a-f]{32})(?:$|[^0-9a-f])/i);
    const out = {};
    if (pageMatch) out.notion_page_id = pageMatch[1];
    let blocks = [];
    if (range) {
      const el = elementOf(range.startContainer);
      const b = el && el.closest('[data-block-id]');
      if (b) blocks.push(b);
    } else {
      // Notion's block selection (blue highlight) leaves the text selection empty.
      blocks = [...document.querySelectorAll('.notion-selectable-halo')]
        .map((h) => h.closest('[data-block-id]')).filter(Boolean);
    }
    if (blocks.length) {
      out.notion_block_ids = [...new Set(blocks.map((b) => b.getAttribute('data-block-id')))];
      if (out.notion_page_id) {
        // The page's own URL plus #block, as Notion's "Copy link to block" gives it (a peeked
        // page, ?p=<id>, links by id instead).
        const base = params.get('p') ? `${location.origin}/${out.notion_page_id}` : `${location.origin}${location.pathname}`;
        out.notion_block_link = `${base}#${out.notion_block_ids[0].replace(/-/g, '')}`;
      }
      if (!range) out.block_text = blocks.map((b) => b.innerText).join('\n').slice(0, MAX_SEL);
    }
    return out;
  }

  function fileNameFrom(text, url) {
    const m = String(text || '').match(/[^\s\\/:*?"<>|]+\.[A-Za-z0-9]{2,5}\b/);
    if (m) return m[0];
    try {
      const last = decodeURIComponent(new URL(url).pathname.split('/').pop() || '');
      if (/\.[A-Za-z0-9]{2,5}$/.test(last)) return last;
    } catch (e) { /* not a URL */ }
    return '';
  }

  // What the user right-clicked (recorded by target.js): a link, image, media or attachment.
  function describeTarget() {
    const t = window.__bizcTarget;
    if (!t || !t.el || !t.el.isConnected || Date.now() - t.at > 120000) return null;
    const el = t.el;
    const out = {};
    // Gmail attachment chips (and other drag-out downloads) carry download_url="mime:name:url".
    const dl = el.closest('[download_url]');
    const dm = dl && dl.getAttribute('download_url').match(/^([^:]+):([^:]+):(https?:.+)$/);
    if (dm) Object.assign(out, { kind: 'file', mime: dm[1], name: dm[2], url: dm[3] });
    const a = el.closest('a[href]');
    const media = el.closest('img,video,audio');
    if (!out.url && a && !/^javascript:/i.test(a.getAttribute('href'))) {
      Object.assign(out, { kind: 'link', url: a.href });
      if (a.getAttribute('download')) out.name = a.getAttribute('download');
    } else if (!out.url && media) {
      Object.assign(out, { kind: media.tagName.toLowerCase() === 'img' ? 'image' : media.tagName.toLowerCase(),
        url: media.currentSrc || media.src, name: media.getAttribute('alt') || '' });
    }
    const host = dl || a || el;
    const label = host.getAttribute('aria-label') || host.getAttribute('title') || '';
    out.text = ((host.innerText || '').trim() || label).replace(/\s+/g, ' ').slice(0, 300);
    if (!out.name) out.name = fileNameFrom(`${label} ${out.text}`, out.url);
    if (out.kind === 'link' && (out.name || /[?&](view=att|attid=)/.test(out.url))) out.kind = 'file';
    return out.url || out.text ? out : null;
  }

  function capture() {
    const site = siteOf(location);
    const ae = document.activeElement;
    let selection = '';
    let around = '';
    let editable = false;
    let anchor = {};
    let range = null;
    state.saved = null;

    if (isTextField(ae) && ae.selectionEnd > ae.selectionStart) {
      const { selectionStart: start, selectionEnd: end } = ae;
      selection = ae.value.slice(start, end);
      around = ae.value.slice(Math.max(0, start - 1000), end + 1000);
      editable = !ae.readOnly && !ae.disabled;
      state.saved = { kind: 'field', el: ae, start, end, text: selection };
    } else {
      const sel = window.getSelection();
      if (sel && sel.rangeCount && !sel.isCollapsed) {
        range = sel.getRangeAt(0).cloneRange();
        selection = sel.toString();
        const host = editableHost(range.commonAncestorContainer);
        editable = !!host;
        const block = blockOf(range.commonAncestorContainer);
        around = block ? block.innerText.slice(0, MAX_AROUND) : '';
        anchor = quote(range, block);
        state.saved = { kind: 'range', range, host, text: selection };
      }
    }
    if (site === 'notion') {
      anchor = { ...anchor, ...notionAnchor(range) };
      if (!selection && anchor.block_text) selection = anchor.block_text;
    }
    if (site === 'google-doc') {
      const m = location.pathname.match(/\/document\/d\/([^/]+)/);
      if (m) anchor.doc_id = m[1];
    }
    const target = describeTarget();
    window.__bizcTarget = null;              // one right-click, one capture
    return {
      url: location.href, title: document.title, site,
      selection: selection.slice(0, MAX_SEL), around, editable, anchor, target,
    };
  }

  // ------------------------------------------------------------------------ page tools
  function currentText(s) {
    if (!s) return '';
    if (s.kind === 'field') return s.el.value.slice(s.start, s.end);
    return s.range ? s.range.toString() : '';
  }

  function checkSaved() {
    const s = state.saved;
    if (!s) throw new Error('Nothing is selected. Ask the user to select the passage and try again.');
    if (s.kind === 'field' && !s.el.isConnected) throw new Error('The text field is gone from the page.');
    if (s.kind === 'range' && (!s.range || !s.range.startContainer.isConnected)) {
      throw new Error('The page changed since the selection was made. Ask the user to select the passage again.');
    }
    if (currentText(s) !== s.text) {
      throw new Error('The selected text changed since it was captured (someone edited it). Ask the user to select it again.');
    }
    return s;
  }

  function typeInto(text) {
    // Typing through execCommand fires the input events editors listen for and lands on the
    // browser's own undo stack (Ctrl+Z).
    return document.execCommand('insertText', false, text);
  }

  function reselectBackward(len) {
    const sel = window.getSelection();
    for (let i = 0; i < len; i += 1) sel.modify('extend', 'backward', 'character');
    return sel.rangeCount ? sel.getRangeAt(0).cloneRange() : null;
  }

  function replaceSelection(text) {
    const s = checkSaved();
    const before = s.text;
    if (s.kind === 'field') {
      const el = s.el;
      if (el.readOnly || el.disabled) throw new Error('That field is read-only.');
      el.focus();
      el.setSelectionRange(s.start, s.end);
      if (!typeInto(text) || el.value.slice(s.start, s.start + text.length) !== text) {
        el.setRangeText(text, s.start, s.end, 'end');
        el.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText', data: text }));
      }
      s.end = s.start + text.length;
      s.text = text;
      el.setSelectionRange(s.start, s.end);
      return { replaced: true, before, after: text };
    }
    if (!s.host) {
      throw new Error('The selection is not in an editable area (this part of the page is read-only). Give the user the text in your reply instead.');
    }
    s.host.focus();
    const sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(s.range);
    if (!typeInto(text)) throw new Error('The editor refused the edit. Give the user the text in your reply instead.');
    const again = reselectBackward(text.length);
    if (again && again.toString() === text) {
      s.range = again;
      s.text = text;
    } else {
      state.saved = null;                // can't track it; a further edit needs a new selection
    }
    return { replaced: true, before, after: text, note: 'The user can undo with Ctrl+Z in the page.' };
  }

  function insertAfterSelection(text, newParagraph) {
    const s = checkSaved();
    if (s.kind === 'field') {
      const el = s.el;
      if (el.readOnly || el.disabled) throw new Error('That field is read-only.');
      const t = (newParagraph && el.tagName === 'TEXTAREA' ? '\n\n' : '') + text;
      el.focus();
      el.setSelectionRange(s.end, s.end);
      if (!typeInto(t)) {
        el.setRangeText(t, s.end, s.end, 'end');
        el.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText', data: t }));
      }
      return { inserted: true, text: t };
    }
    if (!s.host) {
      throw new Error('The selection is not in an editable area. Give the user the text in your reply instead.');
    }
    s.host.focus();
    const r = s.range.cloneRange();
    r.collapse(false);
    const sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(r);
    if (newParagraph) document.execCommand('insertParagraph');
    if (!typeInto(text)) throw new Error('The editor refused the edit. Give the user the text in your reply instead.');
    return { inserted: true, text, note: 'The user can undo with Ctrl+Z in the page.' };
  }

  async function tool(name, args) {
    args = args || {};
    try {
      switch (name) {
        case 'read_selection': {
          const s = state.saved;
          return {
            ok: true,
            result: {
              selection: currentText(s), captured: s ? s.text : '', still_selected: !!s && currentText(s) === s.text,
              editable: !!s && (s.kind === 'field' || !!s.host),
              around: s && s.kind === 'range' ? (blockOf(s.range.commonAncestorContainer) || {}).innerText || '' : '',
            },
          };
        }
        case 'read_page': {
          const max = Math.min(Math.max(parseInt(args.max_chars, 10) || 50000, 1000), 200000);
          const text = (document.body ? document.body.innerText : '') || '';
          return { ok: true, result: { url: location.href, title: document.title, text: text.slice(0, max), truncated: text.length > max } };
        }
        case 'replace_selection':
          return { ok: true, result: replaceSelection(String(args.text ?? '')) };
        case 'insert_after_selection':
          return { ok: true, result: insertAfterSelection(String(args.text ?? ''), !!args.new_paragraph) };
        default:
          return { ok: false, error: `unknown page tool ${name}` };
      }
    } catch (e) {
      return { ok: false, error: String((e && e.message) || e) };
    }
  }

  window.__bizc = { capture, tool };
})();
