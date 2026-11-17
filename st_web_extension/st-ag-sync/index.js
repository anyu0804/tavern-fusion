/**
 * SillyTavern Pure Client-Side Live Sync Extension (`st-ag-sync`).
 *
 * Operates strictly as a browser UI extension (`extensions/third-party/st-ag-sync`)
 * WITHOUT requiring `enableServerPlugins: true` in SillyTavern's `config.yaml`.
 * Reloads the open chat view whenever Antigravity / Tavern Stage commits an external
 * floor, Swipe, variable, or in-place edit on disk.
 *
 * Sync & Performance Design:
 * - Primary probe uses `POST /api/chats/recent` (`max: 0, metadata: true, pinned: [...]`)
 *   so SillyTavern reads ONLY the single active `.jsonl` file and returns
 *   `chat_metadata.integrity` (rotated by Tavern Fusion on every write).
 * - Compares disk `chat_metadata.integrity` against in-memory `ctx.chatMetadata.integrity`:
 *   in-app ST edits match in-memory integrity and never trigger redundant DOM reloads,
 *   while any external Tavern Stage edit (including earlier floors or variables) rotates
 *   integrity and reloads immediately before stale saves can hit HTTP 400 `integrity`.
 * - Gracefully falls back to `POST /api/chats/search` on older SillyTavern builds.
 */
(function () {
  const FAST_INTERVAL_MS = 3000;
  const IDLE_INTERVAL_MS = 15000;
  const IDLE_AFTER_MS = 60000;
  const MAX_ERROR_BACKOFF_MS = 60000;
  const FETCH_TIMEOUT_MS = 8000;

  function normalizeChatStem(raw) {
    return String(raw ?? '').trim().replace(/\.jsonl$/i, '');
  }

  function getPreviewTail(rawMes, maxLen = 400) {
    const s = String(rawMes ?? '');
    if (!s) return '';
    return s.length > maxLen ? '...' + s.substring(s.length - maxLen) : s;
  }

  function buildDiskChatSignature(row) {
    if (!row || typeof row !== 'object') return '';
    const integrity = String(row.chat_metadata?.integrity || '').trim();
    const count = Number(row.chat_items ?? row.message_count ?? 0);
    const lastMes = String(row.last_mes ?? '');
    const fileSize = String(row.file_size ?? '');
    const preview =
      row.preview_message !== undefined
        ? String(row.preview_message || '')
        : getPreviewTail(row.mes || '');
    return `${integrity}|${count}:${lastMes}:${fileSize}:${preview}`;
  }

  function evaluateSyncDecision({
    row,
    prevSignature = '',
    memIntegrity = '',
    memChatLength = null,
    memLastMes = '',
    localDirtySinceLastPoll = false,
  } = {}) {
    if (!row || typeof row !== 'object') {
      return { shouldReload: false, nextSignature: prevSignature || '', reason: 'no_row' };
    }
    const nextSignature = buildDiskChatSignature(row);
    const diskIntegrity = String(row.chat_metadata?.integrity || '').trim();
    const curMemIntegrity = String(memIntegrity || '').trim();

    if (diskIntegrity) {
      // Exact cryptographic UUID match -> disk state was saved by this ST tab itself.
      if (curMemIntegrity && diskIntegrity === curMemIntegrity) {
        return { shouldReload: false, nextSignature, reason: 'in_memory_integrity_match' };
      }
      // Disk integrity differs from ST's in-memory integrity -> external write occurred!
      // Guard with `nextSignature !== prevSignature` so an unrefreshed state never loops.
      if (curMemIntegrity && diskIntegrity !== curMemIntegrity) {
        if (!prevSignature || nextSignature !== prevSignature) {
          return { shouldReload: true, nextSignature, reason: 'external_integrity_rotated' };
        }
        return { shouldReload: false, nextSignature, reason: 'already_attempted_signature' };
      }
      // ST memory has no integrity yet; reload only on observed signature transition.
      if (prevSignature && nextSignature !== prevSignature) {
        return { shouldReload: true, nextSignature, reason: 'signature_changed_no_mem_integrity' };
      }
      return { shouldReload: false, nextSignature, reason: 'initial_baseline' };
    }

    // Legacy fallback path (`/api/chats/search` without `chat_metadata`):
    if (!prevSignature) {
      return { shouldReload: false, nextSignature, reason: 'initial_baseline' };
    }
    if (nextSignature === prevSignature) {
      return { shouldReload: false, nextSignature, reason: 'unchanged' };
    }
    const diskCount = Number(row.chat_items ?? row.message_count ?? -1);
    const diskPreview =
      row.preview_message !== undefined
        ? String(row.preview_message || '')
        : getPreviewTail(row.mes || '');
    const memPreview = getPreviewTail(memLastMes || '');
    if (
      localDirtySinceLastPoll &&
      typeof memChatLength === 'number' &&
      diskCount === memChatLength &&
      diskPreview === memPreview
    ) {
      return { shouldReload: false, nextSignature, reason: 'local_mutation_matched' };
    }
    return { shouldReload: true, nextSignature, reason: 'fallback_signature_changed' };
  }

  function computeBackoffDelay({
    errorStreak = 0,
    now = Date.now(),
    lastChangeAt = now,
    randomFn = Math.random,
  } = {}) {
    if (errorStreak > 0) {
      const exp = Math.min(errorStreak, 6);
      const base = Math.min(MAX_ERROR_BACKOFF_MS, FAST_INTERVAL_MS * 2 ** exp);
      return base / 2 + randomFn() * (base / 2);
    }
    const idle = now - lastChangeAt > IDLE_AFTER_MS;
    return idle ? IDLE_INTERVAL_MS : FAST_INTERVAL_MS;
  }

  globalThis.__stAgSyncInternals = {
    normalizeChatStem,
    getPreviewTail,
    buildDiskChatSignature,
    evaluateSyncDecision,
    computeBackoffDelay,
  };

  if (typeof window === 'undefined' || typeof document === 'undefined') {
    return;
  }
  if (window.__stAgSyncLoaded) return;
  window.__stAgSyncLoaded = true;

  let lastKnownSignature = '';
  let lastChatIdentity = '';
  let autoSyncEnabled = true;
  let lastChangeAt = Date.now();
  let errorStreak = 0;
  let timer = null;
  let checking = false;
  let pendingImmediateCheck = false;
  let localDirtySinceLastPoll = false;
  let recentEndpointSupported = true;
  let boundEventSource = null;

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

  function syncPillVisualState() {
    const pill = document.getElementById('st-ag-sync-pill');
    if (!pill) return;
    pill.classList.toggle('is-disabled', !autoSyncEnabled);
    pill.classList.toggle('has-error', autoSyncEnabled && errorStreak > 0);
    if (!autoSyncEnabled) {
      pill.title = '磁盘自动同步已暂停（点击重新开启）';
    } else if (errorStreak > 0) {
      pill.title = `同步连接退避重试中（连续失败 ${errorStreak} 次，点击暂停）`;
    } else {
      pill.title = '点击切换 Antigravity Tavern Fusion 磁盘自动同步';
    }
  }

  function ensureContextBindings(ctx) {
    const evSrc = ctx?.eventSource;
    const evTypes = ctx?.eventTypes || ctx?.event_types;
    if (!evSrc || !evTypes || typeof evSrc.on !== 'function' || boundEventSource === evSrc) {
      return;
    }
    boundEventSource = evSrc;

    if (evTypes.CHAT_CHANGED) {
      evSrc.on(evTypes.CHAT_CHANGED, () => {
        lastKnownSignature = '';
        localDirtySinceLastPoll = false;
        lastChangeAt = Date.now();
        if (autoSyncEnabled) schedule(250);
      });
    }

    const localMutationEvents = [
      evTypes.MESSAGE_SENT,
      evTypes.MESSAGE_RECEIVED,
      evTypes.MESSAGE_EDITED,
      evTypes.MESSAGE_DELETED,
      evTypes.MESSAGE_SWIPED,
      evTypes.CHAT_SAVED,
    ].filter(Boolean);

    for (const evtName of new Set(localMutationEvents)) {
      evSrc.on(evtName, () => {
        localDirtySinceLastPoll = true;
        lastChangeAt = Date.now();
      });
    }
  }

  async function fetchWithTimeout(url, options) {
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), FETCH_TIMEOUT_MS);
    try {
      return await fetch(url, { ...options, signal: controller.signal });
    } finally {
      clearTimeout(timeoutId);
    }
  }

  async function fetchDiskChatRow(headers, avatar, fileName) {
    if (recentEndpointSupported) {
      const res = await fetchWithTimeout('/api/chats/recent', {
        method: 'POST',
        headers,
        body: JSON.stringify({
          max: 0,
          metadata: true,
          pinned: [{ file_name: `${fileName}.jsonl`, avatar }],
        }),
      });
      if (res.status === 404 || res.status === 405) {
        recentEndpointSupported = false;
      } else {
        if (!res.ok) {
          throw new Error(`HTTP ${res.status}`);
        }
        const rows = await res.json();
        if (!Array.isArray(rows)) return null;
        return (
          rows.find(
            (r) =>
              normalizeChatStem(r?.file_id || r?.file_name) === fileName &&
              (!r?.avatar || r.avatar === avatar)
          ) || null
        );
      }
    }

    const fallbackRes = await fetchWithTimeout('/api/chats/search', {
      method: 'POST',
      headers,
      body: JSON.stringify({ query: '', avatar_url: avatar }),
    });
    if (!fallbackRes.ok) {
      throw new Error(`HTTP ${fallbackRes.status}`);
    }
    const rows = await fallbackRes.json();
    if (!Array.isArray(rows)) return null;
    return rows.find((r) => normalizeChatStem(r?.file_name) === fileName) || null;
  }

  function nextDelay() {
    return computeBackoffDelay({
      errorStreak,
      now: Date.now(),
      lastChangeAt,
    });
  }

  function stopSchedule() {
    if (timer !== null) {
      clearTimeout(timer);
      timer = null;
    }
  }

  function schedule(delay = nextDelay()) {
    stopSchedule();
    if (!autoSyncEnabled) return;
    if (checking && delay === 0) {
      pendingImmediateCheck = true;
      return;
    }
    timer = setTimeout(tick, delay);
  }

  async function tick() {
    timer = null;
    if (!autoSyncEnabled) return;
    await checkDiskChatDelta();
    if (!autoSyncEnabled) {
      stopSchedule();
      return;
    }
    if (pendingImmediateCheck) {
      pendingImmediateCheck = false;
      schedule(0);
      return;
    }
    schedule(nextDelay());
  }

  async function checkDiskChatDelta() {
    if (checking || !autoSyncEnabled || document.hidden) return;
    const ctx = getContextSafe();
    if (!ctx) return;
    ensureContextBindings(ctx);

    if (ctx.characterId === undefined || !ctx.chatId || ctx.groupId) return;
    if (isGenerating(ctx)) return;
    const charObj = ctx.characters?.[ctx.characterId];
    if (!charObj?.avatar) return;

    const fileName = normalizeChatStem(ctx.chatId);
    if (!fileName) return;
    const chatIdentity = `${charObj.avatar}:${fileName}`;
    if (chatIdentity !== lastChatIdentity) {
      lastChatIdentity = chatIdentity;
      lastKnownSignature = '';
      localDirtySinceLastPoll = false;
    }

    checking = true;
    try {
      const headers =
        typeof ctx.getRequestHeaders === 'function'
          ? ctx.getRequestHeaders()
          : { 'Content-Type': 'application/json' };
      const row = await fetchDiskChatRow(headers, charObj.avatar, fileName);
      if (errorStreak !== 0) {
        errorStreak = 0;
        syncPillVisualState();
      }
      if (!row) return;

      const memChat = Array.isArray(ctx.chat) ? ctx.chat : null;
      const memLastMes =
        memChat && memChat.length > 0 ? String(memChat[memChat.length - 1]?.mes || '') : '';
      const memIntegrity = String((ctx.chatMetadata || ctx.chat_metadata)?.integrity || '');

      const decision = evaluateSyncDecision({
        row,
        prevSignature: lastKnownSignature,
        memIntegrity,
        memChatLength: memChat ? memChat.length : null,
        memLastMes,
        localDirtySinceLastPoll,
      });

      lastKnownSignature = decision.nextSignature;
      localDirtySinceLastPoll = false;

      if (decision.shouldReload) {
        lastChangeAt = Date.now();
        if (typeof ctx.reloadCurrentChat === 'function') {
          await ctx.reloadCurrentChat();
        }
      }
    } catch {
      errorStreak += 1;
      syncPillVisualState();
    } finally {
      checking = false;
    }
  }

  function mountIndicator() {
    const topBar =
      document.querySelector('#top-bar') || document.querySelector('#top-settings-holder');
    if (!topBar || document.querySelector('#st-ag-sync-pill')) {
      syncPillVisualState();
      return Boolean(topBar);
    }
    const pill = document.createElement('div');
    pill.id = 'st-ag-sync-pill';
    pill.className = 'st-ag-sync-pill';
    pill.innerHTML = '<span class="st-ag-sync-dot"></span><span>AG Fusion Sync</span>';
    pill.addEventListener('click', () => {
      autoSyncEnabled = !autoSyncEnabled;
      syncPillVisualState();
      if (autoSyncEnabled) {
        lastChangeAt = Date.now();
        schedule(0);
      } else {
        pendingImmediateCheck = false;
        stopSchedule();
      }
    });
    topBar.appendChild(pill);
    syncPillVisualState();
    return true;
  }

  function init() {
    if (!mountIndicator()) {
      let tries = 0;
      const retry = setInterval(() => {
        tries += 1;
        if (mountIndicator() || tries > 20) clearInterval(retry);
      }, 500);
    }
    document.addEventListener('visibilitychange', () => {
      if (!document.hidden && autoSyncEnabled) {
        lastChangeAt = Date.now();
        schedule(0);
      }
    });
    const ctx = getContextSafe();
    if (ctx) ensureContextBindings(ctx);
    schedule(FAST_INTERVAL_MS);
  }

  const ctx0 = getContextSafe();
  const appReady = ctx0?.eventTypes?.APP_READY || ctx0?.event_types?.APP_READY;
  if (ctx0?.eventSource && appReady) {
    let started = false;
    const start = () => {
      if (started) return;
      started = true;
      const unbind = ctx0.eventSource.removeListener || ctx0.eventSource.off;
      if (typeof unbind === 'function') {
        try {
          unbind.call(ctx0.eventSource, appReady, start);
        } catch {
          // ignore
        }
      }
      init();
    };
    if (typeof ctx0.eventSource.once === 'function') {
      ctx0.eventSource.once(appReady, start);
    } else if (typeof ctx0.eventSource.on === 'function') {
      ctx0.eventSource.on(appReady, start);
    }
    setTimeout(start, 3000);
  } else if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init, { once: true });
  } else {
    init();
  }
})();
