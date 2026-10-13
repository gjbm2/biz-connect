// biz-connect: Ask Claude, content script on every page (document_start, all frames).
// Remembers what the user last right-clicked, so "Ask Claude" can act on a link, image or
// attachment, not only on selected text. capture.js reads it (same isolated world).
(() => {
  if (window.__bizcTargetInstalled) return;
  window.__bizcTargetInstalled = true;
  document.addEventListener('contextmenu', (e) => {
    window.__bizcTarget = { el: e.target, at: Date.now() };
  }, true);
})();
