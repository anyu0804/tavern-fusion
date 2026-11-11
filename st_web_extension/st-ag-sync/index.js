/**
 * SillyTavern Pure Client-Side Live Sync Extension (`st-ag-sync`).
 *
 * Operates strictly as a browser UI extension (`extensions/third-party/st-ag-sync`)
 * WITHOUT requiring `enableServerPlugins: true` in SillyTavern's `config.yaml`.
 * Reloads the open chat view whenever Antigravity commits a new floor on disk.
 *
 * Notes:
 * - ST injects third-party extensions after the page has loaded, so `DOMContentLoaded`
 *   has usually already fired: initialisation runs immediately (or on ST's APP_READY).
 * - Change detection uses `/api/chats/search` (per-file message count, last date and a
 *   400-char preview) instead of downloading the whole chat every few seconds.
 * - Polling is adaptive: fast right after a change, slower when idle, paused while the
 *   tab is hidden or ST is generating, and re-checked immediately when the tab becomes
 *   visible again (before the user can edit a stale in-memory chat).
 * - Tavern Fusion rotates `chat_metadata.integrity` on every write, so even if ST tries
 *   to save a stale chat, ST's own integrity check refuses it instead of erasing floors.
 */
(function () {
  if (window.__stAgSyncLoaded) return;
  window.__stAgSyncLoaded = true;

  const FAST_INTERVAL_MS = 3000;
  const IDLE_INTERVAL_MS = 15000;
  const IDLE_AFTER_MS = 60000;
  const MAX_ERROR_BACKOFF_MS = 60000;

  let lastKnownSignature = '';
  let lastChatIdentity = '';
  let autoSyncEnabled = true;
  let lastChangeAt = Date.now();
  let errorStreak = 0;
  let timer = null;
  let checking = false;

  function getContextSafe() {
    try {
      return window.SillyTavern?.getContext ? window.SillyTavern.getContext() : null;
    } catch {
      return null;
    }
  }

  function isGenerating(ctx) {
    try {
      if (ctx?.streamingProcessor && !ctx.streamingProcessor.isFinished) return true;
    } catch {
      // ignore
    }
    const stopBtn = document.getElementById('mes_stop');
    return Boolean(stopBtn && stopBtn.offsetParent !== null);
  }

  function nextDelay() {
    if (errorStreak > 0) {
      const base = Math.min(MAX_ERROR_BACKOFF_MS, FAST_INTERVAL_MS * 2 ** errorStreak);
      return base / 2 + Math.random() * (base / 2);
    }
    const idle = Date.now() - lastChangeAt > IDLE_AFTER_MS;
    return idle ? IDLE_INTERVAL_MS : FAST_INTERVAL_MS;
  }

  function schedule(delay = nextDelay()) {
    clearTimeout(timer);
    timer = setTimeout(tick, delay);
  }

  async function tick() {
    await checkDiskChatDelta();
    schedule();
  }

  async function checkDiskChatDelta() {
    if (checking || !autoSyncEnabled || document.hidden) return;
    const ctx = getContextSafe();
    if (!ctx || ctx.characterId === undefined || !ctx.chatId || ctx.groupId) return;
    if (isGenerating(ctx)) return;
    const charObj = ctx.characters?.[ctx.characterId];
    if (!charObj?.avatar) return;

    const fileName = String(ctx.chatId).replace(/\.jsonl$/i, '');
    const chatIdentity = `${charObj.avatar}:${fileName}`;
    if (chatIdentity !== lastChatIdentity) {
      lastChatIdentity = chatIdentity;
      lastKnownSignature = '';
    }

    checking = true;
    try {
      const headers = typeof ctx.getRequestHeaders === 'function'
        ? ctx.getRequestHeaders()
        : { 'Content-Type': 'application/json' };
      const res = await fetch('/api/chats/search', {
        method: 'POST',
        headers,
        body: JSON.stringify({ query: '', avatar_url: charObj.avatar }),
      });
      if (!res.ok) {
        errorStreak += 1;
        return;
      }
      errorStreak = 0;
      const rows = await res.json();
      if (!Array.isArray(rows)) return;
      const row = rows.find((r) => String(r?.file_name || '').replace(/\.jsonl$/i, '') === fileName);
      if (!row) return;
      const sig = `${row.message_count}:${row.last_mes}:${row.file_size}:${row.preview_message || ''}`;
      if (lastKnownSignature && sig !== lastKnownSignature) {
        lastKnownSignature = sig;
        lastChangeAt = Date.now();
        if (typeof ctx.reloadCurrentChat === 'function') {
          await ctx.reloadCurrentChat();
        }
      } else {
        lastKnownSignature = sig;
      }
    } catch {
      errorStreak += 1;
    } finally {
      checking = false;
    }
  }

  function mountIndicator() {
    const topBar = document.querySelector('#top-bar') || document.querySelector('#top-settings-holder');
    if (!topBar || document.querySelector('#st-ag-sync-pill')) return Boolean(topBar);
    const pill = document.createElement('div');
    pill.id = 'st-ag-sync-pill';
    pill.className = 'st-ag-sync-pill';
    pill.title = '点击切换 Antigravity Tavern Fusion 磁盘自动同步';
    pill.innerHTML = '<span class="st-ag-sync-dot"></span><span>AG Fusion Sync</span>';
    pill.addEventListener('click', () => {
      autoSyncEnabled = !autoSyncEnabled;
      pill.style.opacity = autoSyncEnabled ? '1' : '0.45';
      if (autoSyncEnabled) schedule(0);
    });
    topBar.appendChild(pill);
    return true;
  }

  function init() {
    if (!mountIndicator()) {
      // Top bar not rendered yet: retry a few times without blocking sync.
      let tries = 0;
      const retry = setInterval(() => {
        tries += 1;
        if (mountIndicator() || tries > 20) clearInterval(retry);
      }, 500);
    }
    document.addEventListener('visibilitychange', () => {
      if (!document.hidden) {
        lastChangeAt = Date.now();
        schedule(0);
      }
    });
    const ctx = getContextSafe();
    const chatChanged = ctx?.eventTypes?.CHAT_CHANGED || ctx?.event_types?.CHAT_CHANGED;
    if (ctx?.eventSource && chatChanged) {
      ctx.eventSource.on(chatChanged, () => {
        lastKnownSignature = '';
        lastChangeAt = Date.now();
      });
    }
    schedule(FAST_INTERVAL_MS);
  }

  const ctx0 = getContextSafe();
  const appReady = ctx0?.eventTypes?.APP_READY || ctx0?.event_types?.APP_READY;
  if (ctx0?.eventSource && appReady) {
    // If APP_READY already fired before this script loaded, the timer starts us anyway.
    let started = false;
    const start = () => {
      if (!started) {
        started = true;
        init();
      }
    };
    ctx0.eventSource.once ? ctx0.eventSource.once(appReady, start) : ctx0.eventSource.on(appReady, start);
    setTimeout(start, 3000);
  } else if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init, { once: true });
  } else {
    init();
  }
})();
