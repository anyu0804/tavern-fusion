/**
 * SillyTavern Pure Client-Side Live Sync Extension (`st-ag-sync`).
 *
 * Operates strictly as a browser UI extension (`extensions/third-party/st-ag-sync`)
 * WITHOUT requiring `enableServerPlugins: true` in SillyTavern's `config.yaml`.
 * Polls standard `/api/chats/get` for the active character chat when the tab is
 * visible and reloads the chat view whenever Antigravity commits a new floor on disk.
 */
(function () {
  const POLL_INTERVAL_MS = 3000;
  let lastKnownFloorSignature = '';
  let autoSyncEnabled = true;

  function getContextSafe() {
    try {
      return window.SillyTavern?.getContext ? window.SillyTavern.getContext() : null;
    } catch {
      return null;
    }
  }

  async function checkDiskChatDelta() {
    if (!autoSyncEnabled || document.hidden) return;
    const ctx = getContextSafe();
    if (!ctx || ctx.characterId === undefined || !ctx.chatId) return;
    const charObj = ctx.characters?.[ctx.characterId];
    if (!charObj?.avatar) return;

    try {
      const headers = typeof ctx.getRequestHeaders === 'function'
        ? ctx.getRequestHeaders()
        : { 'Content-Type': 'application/json' };
      const res = await fetch('/api/chats/get', {
        method: 'POST',
        headers,
        body: JSON.stringify({
          ch_name: charObj.name,
          file_name: String(ctx.chatId).replace(/\.jsonl$/i, ''),
          avatar_url: charObj.avatar,
        }),
      });
      if (!res.ok) return;
      const rows = await res.json();
      if (!Array.isArray(rows) || rows.length < 2) return;
      const tail = rows[rows.length - 1];
      const sig = `${rows.length}:${tail?.send_date || ''}:${tail?.swipe_id || 0}:${(tail?.mes || '').length}`;
      if (lastKnownFloorSignature && sig !== lastKnownFloorSignature) {
        lastKnownFloorSignature = sig;
        if (typeof ctx.reloadCurrentChat === 'function') {
          await ctx.reloadCurrentChat();
        }
      } else {
        lastKnownFloorSignature = sig;
      }
    } catch {
      // Quietly skip when ST backend is busy
    }
  }

  function mountIndicator() {
    const topBar = document.querySelector('#top-bar') || document.querySelector('#top-settings-holder');
    if (!topBar || document.querySelector('#st-ag-sync-pill')) return;
    const pill = document.createElement('div');
    pill.id = 'st-ag-sync-pill';
    pill.className = 'st-ag-sync-pill';
    pill.title = '点击切换 Antigravity Tavern Fusion 磁盘自动同步';
    pill.innerHTML = '<span class="st-ag-sync-dot"></span><span>AG Fusion Sync</span>';
    pill.addEventListener('click', () => {
      autoSyncEnabled = !autoSyncEnabled;
      pill.style.opacity = autoSyncEnabled ? '1' : '0.45';
    });
    topBar.appendChild(pill);
  }

  window.addEventListener('DOMContentLoaded', () => {
    setTimeout(mountIndicator, 1500);
    setInterval(checkDiskChatDelta, POLL_INTERVAL_MS);
  });
})();
