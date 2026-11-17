/**
 * Tavern Stage Frontend Controller (`sidecars/tavern_stage/ui/app.js`).
 *
 * Connects to `main.mjs` via ETag incremental polling (`GET /api/state`) and
 * dispatches stage turns through `window.sidecar.agent.sendMessage`
 * / `startConversation` over the official host Sidecar SDK.
 */

const PLACEHOLDER_SVG =
  'data:image/svg+xml;utf8,' +
  encodeURIComponent(
    '<svg xmlns="http://www.w3.org/2000/svg" width="120" height="150"><rect width="100%" height="100%" fill="#1e2433"/><text x="50%" y="52%" fill="#fbbf24" font-family="sans-serif" font-size="14" text-anchor="middle">Tavern</text></svg>'
  );

let currentEtag = '';
let pollSeq = 0;
let appState = null;
let activeWorkspaceUri = '';
let activeConvId = '';
let editingFloorState = null; // { chatFile, idx, draft, selStart, selEnd, focused }
let expandedBackstageChat = '';
const expandedBackstageIdxs = new Set();
let loadedLorebook = { lorebook_name: '', entries: [], issues: [] };
let loadingLorebookName = '';
let failedLorebookCooldown = { name: '', until: 0 };
let lastLoreCardWorld = '';
let lastRenderedFloorSig = '';
let lastRenderedDoctorSig = '';
let lastRenderedCardsSig = '';
let inspectorDirty = false;
let lastInspectorSessionKey = '';
let localFirstRunAcked = false;

const $ = (id) => document.getElementById(id);

function escapeHtml(raw) {
  return String(raw ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function formatRpProse(raw) {
  let safe = escapeHtml(raw);
  safe = safe.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>');
  safe = safe.replace(/\*([^*\n]+)\*/g, '<span class="rp-action">*$1*</span>');
  safe = safe.replace(/&quot;((?:(?!&quot;)[^\n])+)&quot;/g, '<span class="rp-quote">"$1"</span>');
  safe = safe.replace(/“([^”\n]+)”/g, '<span class="rp-quote">“$1”</span>');
  return safe;
}

function escapeNonAsciiJson(jsonStr) {
  return String(jsonStr).replace(/[\u007f-\uffff]/g, (ch) =>
    '\\u' + ch.charCodeAt(0).toString(16).padStart(4, '0')
  );
}

function serializeAsciiJson(payload) {
  return escapeNonAsciiJson(JSON.stringify(payload));
}

let toastTimer = null;
function showToast(msg, isError = false) {
  const bar = $('toast-bar');
  if (!bar) return;
  if (!msg) {
    bar.classList.add('hidden');
    return;
  }
  bar.textContent = msg;
  bar.classList.toggle('error', Boolean(isError));
  bar.classList.remove('hidden');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => bar.classList.add('hidden'), isError ? 6000 : 4000);
}

let armedConfirm = null;
function clearArmedConfirm() {
  if (!armedConfirm) return;
  clearTimeout(armedConfirm.timer);
  if (armedConfirm.el && armedConfirm.el.isConnected) {
    armedConfirm.el.textContent = armedConfirm.origText;
    armedConfirm.el.classList.remove('confirm-armed');
  }
  armedConfirm = null;
}

function requireInlineConfirm(btnEl, key, confirmLabel = '确认？') {
  if (!btnEl) return true;
  if (armedConfirm && armedConfirm.key === key) {
    clearArmedConfirm();
    return true;
  }
  clearArmedConfirm();
  const origText = btnEl.textContent;
  btnEl.textContent = confirmLabel;
  btnEl.classList.add('confirm-armed');
  const timer = setTimeout(() => clearArmedConfirm(), 3500);
  armedConfirm = { key, timer, el: btnEl, origText };
  return false;
}

function resetEditingFloor() {
  editingFloorState = null;
  lastRenderedFloorSig = '';
}

async function sidecarFetchJson(url, options = {}) {
  const fetcher = window.sidecar?.fetch || fetch;
  const reqOptions = { ...options };
  if (reqOptions.body !== undefined && reqOptions.body !== null) {
    reqOptions.body =
      typeof reqOptions.body === 'string'
        ? escapeNonAsciiJson(reqOptions.body)
        : serializeAsciiJson(reqOptions.body);
  }
  const res = await fetcher(url, reqOptions);
  const data = await res.json().catch(() => ({}));
  if (!res.ok || data.ok === false) {
    throw new Error(data.error || `HTTP ${res.status}`);
  }
  return data;
}

async function callOp(op, args = {}, opts = {}) {
  const res = await sidecarFetchJson('/api/op', {
    method: 'POST',
    body: serializeAsciiJson({ op, args, workspaceUri: activeWorkspaceUri }),
  });
  if (!opts.skipStateRefresh) {
    if (res && res.state && res.etag) {
      pollSeq += 1;
      currentEtag = res.etag;
      appState = res.state;
      pollErrorStreak = 0;
      renderAll();
    } else {
      currentEtag = '';
      await pollState(true);
    }
  }
  if (op === 'byok_turn' && activeStreamStop) {
    activeStreamStop();
  }
  return res.data;
}

function mediaUrl(params) {
  const q = new URLSearchParams(params);
  if (activeWorkspaceUri) q.set('workspaceUri', activeWorkspaceUri);
  const qs = q.toString();
  const vParam = `v=${encodeURIComponent(currentEtag || '0')}`;
  return `/api/media?${qs ? `${qs}&${vParam}` : vParam}`;
}

function isJsonCardFile(filename) {
  return /\.json$/i.test(String(filename || '').trim());
}

function cardAvatarUrl(filename) {
  if (!filename || isJsonCardFile(filename)) return PLACEHOLDER_SVG;
  return mediaUrl({ kind: 'avatar', file: filename });
}

function isFirstRunAcked() {
  if (localFirstRunAcked) return true;
  const pcfg = appState?.plugin_settings;
  if (!pcfg?.first_run_notice_ack_v1) return false;
  const requiredVer = Number(appState?.first_run_notice?.version || 2);
  const ackedVer = Number(pcfg.first_run_notice_ack_version || 1);
  return ackedVer >= requiredVer;
}

function formatNoticeItemHtml(raw) {
  const text = String(raw ?? '');
  const sep = text.indexOf('：');
  if (sep > 0 && sep <= 24) {
    return `<strong>${escapeHtml(text.slice(0, sep))}</strong>：${escapeHtml(text.slice(sep + 1))}`;
  }
  return escapeHtml(text);
}

function isMinorGateBlockingTurn() {
  const g = appState?.minor_risk_gate;
  if (!g) return false;
  if (g.gate_action === 'block_load') return true;
  if (g.gate_action === 'red_screen_warn' && !g.warn_acked) return true;
  return false;
}

// ---------------------------------------------------------------------------
// BYOK (own API key) Turn Dispatcher & Live Streaming Preview
// ---------------------------------------------------------------------------
const BYOK_ACTIONS = new Set(['send_turn', 'swipe_reroll']);
let byokBusy = false;
let hostDispatchInFlight = false;
let cancelInFlight = false;
let userCancelledTurn = false;
let activeStreamStop = null;

let streamDraftText = null;
let streamDraftKind = '';
let streamBaseFloorCount = 0;
let streamBaseSwipeCount = 0;
let lastCharFloorSig = null;
let typewriterTimer = null;

async function dispatchByokTurn(actionKind, extraArgs = {}, meta = {}) {
  if (!BYOK_ACTIONS.has(actionKind)) {
    // Never silently fall back to the host model: that would spend host quota the user opted out of.
    showToast('自有 API Key 后端暂只支持发送与重抽；如需此功能请在配置中切回宿主模型。', true);
    return false;
  }
  if (!appState?.byok?.ready) {
    showToast('自有 API Key 后端未配置完整：请设置 TAVERN_BYOK_API_KEY 并填写模型名。', true);
    return false;
  }
  if (byokBusy || appState?.session?.stage_busy) {
    showToast('上一轮仍在生成中，请稍候或点击停止。', false);
    return false;
  }
  byokBusy = true;
  userCancelledTurn = false;
  renderActionLocks(appState?.session || {});
  showToast(`正在通过自有 API（${appState.byok.model}）生成…`, false);
  const stopPreview = appState.byok.stream === false ? () => {} : startStreamPreview(actionKind);
  activeStreamStop = stopPreview;
  try {
    const res = await callOp('byok_turn', { action_kind: actionKind, ...extraArgs });
    if (res?.gentle_notice) showToast(res.gentle_notice, false);
    else showToast('');
    return true;
  } catch (err) {
    const errMsg = err?.message || String(err);
    if (!userCancelledTurn && !/已取消|cancelled/i.test(errMsg)) {
      showToast(errMsg, true);
    }
    await pollState(true).catch(() => {});
    const curLen = (appState?.messages || []).length;
    const prevLen = meta.prevFloorCount ?? curLen;
    return curLen > prevLen;
  } finally {
    stopPreview();
    if (activeStreamStop === stopPreview) activeStreamStop = null;
    byokBusy = false;
    userCancelledTurn = false;
    renderActionLocks(appState?.session || {});
  }
}

function startStreamPreview(actionKind) {
  let stopped = false;
  const msgs = appState?.messages || [];
  streamDraftText = '';
  streamDraftKind = actionKind;
  streamBaseFloorCount = msgs.length;
  const lastMsg = msgs[msgs.length - 1];
  streamBaseSwipeCount = Array.isArray(lastMsg?.swipes) ? lastMsg.swipes.length : 0;
  renderStreamDraft();
  const stop = () => {
    if (stopped) return;
    stopped = true;
    streamDraftText = null;
    renderStreamDraft();
  };
  const tick = async () => {
    if (stopped) return;
    try {
      const p = await sidecarFetchJson('/api/stream_preview', { method: 'GET' });
      const kindMatches = !p?.action_kind || p.action_kind === actionKind;
      if (!stopped && p?.active && kindMatches && typeof p.text === 'string' && p.text !== streamDraftText) {
        streamDraftText = p.text;
        renderStreamDraft();
      }
    } catch {
      // Cosmetic only; the committed floor arrives through the normal state poll.
    }
    if (!stopped) setTimeout(tick, 150);
  };
  setTimeout(tick, 150);
  return stop;
}

function renderStreamDraft() {
  const stream = $('chat-stream');
  if (!stream) return;
  let el = $('stream-draft');
  if (streamDraftText === null) {
    el?.remove();
    return;
  }
  if (!el) {
    el = document.createElement('article');
    el.id = 'stream-draft';
    el.className = 'floor-card stream-draft';
  }
  const nearBottom = stream.scrollHeight - stream.scrollTop - stream.clientHeight < 90;
  const who = escapeHtml(appState?.session?.character_name || '');
  const label = streamDraftKind === 'swipe_reroll' ? '重演中' : '正在书写';
  el.innerHTML = `
    <div class="floor-hdr"><div><span class="floor-author">${who}</span> <span class="brand-sub">${label}…</span></div></div>
    <div class="floor-body">${formatRpProse(streamDraftText)}<span class="stream-caret"></span></div>`;
  if (el.parentNode !== stream || stream.lastElementChild !== el) stream.appendChild(el);
  if (nearBottom) stream.scrollTop = stream.scrollHeight;
}

function typewriterEnabled() {
  try {
    return localStorage.getItem('tf_typewriter') !== '0';
  } catch {
    return true; // Storage blocked in some sandboxed webviews.
  }
}

function finishTypewriter() {
  if (typewriterTimer) {
    clearInterval(typewriterTimer.id);
    typewriterTimer.body.innerHTML = typewriterTimer.finalHtml;
    typewriterTimer = null;
  }
}

function maybeTypewriteLastFloor(sess, messages, hadLiveStream = false) {
  const last = messages[messages.length - 1];
  const isCharFloor = Boolean(last && !last.is_user && !last.extra?.is_backstage);
  const swipes = Array.isArray(last?.swipes) && last.swipes.length ? last.swipes : [last?.mes || ''];
  const text = isCharFloor ? swipes[Math.min(Math.max(Number(last.swipe_id || 0), 0), swipes.length - 1)] ?? last.mes ?? '' : '';
  const cur = { chat: sess.chat_file, len: messages.length, swipes: swipes.length };
  const prev = lastCharFloorSig;
  lastCharFloorSig = cur;
  const skip = hadLiveStream || streamDraftText !== null;
  if (
    !prev ||
    prev.chat !== cur.chat ||
    !isCharFloor ||
    skip ||
    !typewriterEnabled() ||
    editingFloorState !== null ||
    !$('panel-stage')?.classList.contains('active')
  ) {
    return;
  }
  const arrived = cur.len > prev.len || (cur.len === prev.len && cur.swipes > prev.swipes);
  if (!arrived) return;
  const bodies = $('chat-stream')?.querySelectorAll('.floor-body');
  const body = bodies?.[bodies.length - 1];
  if (!body || !text) return;
  finishTypewriter();
  const chars = Array.from(text);
  const totalMs = Math.min(chars.length * 22, 12000);
  const step = Math.max(1, Math.ceil(chars.length / (totalMs / 30)));
  let shown = 0;
  const state = { id: 0, body, finalHtml: body.innerHTML };
  body.textContent = '';
  state.id = setInterval(() => {
    shown = Math.min(chars.length, shown + step);
    body.innerHTML = formatRpProse(chars.slice(0, shown).join('')) + (shown < chars.length ? '<span class="stream-caret"></span>' : '');
    const stream = $('chat-stream');
    if (stream && stream.scrollHeight - stream.scrollTop - stream.clientHeight < 140) stream.scrollTop = stream.scrollHeight;
    if (shown >= chars.length) finishTypewriter();
  }, 30);
  typewriterTimer = state;
  body.addEventListener('click', finishTypewriter, { once: true });
}

// ---------------------------------------------------------------------------
// Stage Turn Dispatcher
// ---------------------------------------------------------------------------
async function dispatchAgentTurn(actionKind, extraArgs = {}, meta = {}) {
  let userFloorAppended = false;
  try {
    if (!isFirstRunAcked()) {
      $('first-run-modal')?.classList.remove('hidden');
      showToast('请先阅读并确认首次运行须知', false);
      return false;
    }
    if (isMinorGateBlockingTurn()) {
      renderGateModals();
      showToast('当前角色卡触发未成年风险闸，请先在红屏警告中处理', true);
      return false;
    }
    if (appState?.plugin_settings?.generation_backend === 'byok') {
      return await dispatchByokTurn(actionKind, extraArgs, meta);
    }
    if (hostDispatchInFlight || appState?.session?.stage_busy) {
      showToast('上一轮仍在生成中，请稍候或点击停止。', false);
      return false;
    }
    if (!window.sidecar?.agent || window.sidecar.standalone) {
      throw new Error('当前处于独立预览模式，请在右侧舞台面板中发起互动。');
    }

    hostDispatchInFlight = true;
    renderActionLocks(appState?.session || {});
    const boundConvId = activeConvId || appState?.session?.rp_conv_id || '';
    const prep = await callOp('prepare_agent_turn', {
      action_kind: actionKind,
      rp_conv_id: boundConvId,
      ...extraArgs,
    });
    userFloorAppended = actionKind === 'send_turn' || actionKind === 'director_intervention';
    if (prep.gentle_notice) {
      showToast(prep.gentle_notice, false);
    }
    let dispatchedToExisting = false;
    if (boundConvId) {
      try {
        await window.sidecar.agent.sendMessage(prep.prompt, boundConvId);
        activeConvId = boundConvId;
        dispatchedToExisting = true;
      } catch {
        dispatchedToExisting = false;
      }
    }
    if (!dispatchedToExisting) {
      const started = await window.sidecar.agent.startConversation(
        prep.prompt,
        `RP · ${prep.character_name}`
      );
      const createdId =
        started?.conversationId ||
        started?.response?.conversationId ||
        started?.response?.conversation_id ||
        '';
      if (createdId) {
        activeConvId = createdId;
        await callOp('bind_rp_conv', { rp_conv_id: createdId }).catch(() => {});
      }
    }
    return true;
  } catch (err) {
    showToast(err.message || String(err), true);
    return userFloorAppended;
  } finally {
    hostDispatchInFlight = false;
    renderActionLocks(appState?.session || {});
  }
}

// ---------------------------------------------------------------------------
// State Polling & Rendering
// ---------------------------------------------------------------------------
let pollErrorStreak = 0;

async function pollState(force = false) {
  const mySeq = ++pollSeq;
  try {
    const q = new URLSearchParams();
    if (!force && currentEtag) q.set('etag', currentEtag);
    if (activeWorkspaceUri) q.set('workspaceUri', activeWorkspaceUri);
    const resp = await sidecarFetchJson(`/api/state?${q.toString()}`, { method: 'GET' });
    if (mySeq !== pollSeq) return;
    pollErrorStreak = 0;
    if (resp.unchanged) return;
    currentEtag = resp.etag || '';
    appState = resp.state;
    renderAll();
  } catch (err) {
    if (mySeq !== pollSeq) return;
    pollErrorStreak += 1;
    if (pollErrorStreak === 1) console.warn('[Tavern Stage poll]', err);
  }
}

function nextPollDelay() {
  if (pollErrorStreak > 0) {
    const cap = Math.min(30000, 1000 * 2 ** Math.min(pollErrorStreak, 5));
    return cap / 2 + Math.random() * (cap / 2);
  }
  return appState?.session?.stage_busy || byokBusy ? 1100 : 2500;
}

function renderActionLocks(sess) {
  const isBusy = Boolean(sess?.stage_busy) || byokBusy || hostDispatchInFlight;
  const isByok = appState?.plugin_settings?.generation_backend === 'byok';
  const sendBtn = $('btn-send-turn');
  if (sendBtn) sendBtn.disabled = isBusy;

  const cancelBtn = $('btn-cancel-turn');
  if (cancelBtn) {
    const showCancel = byokBusy || Boolean(sess?.stage_busy);
    cancelBtn.classList.toggle('hidden', !showCancel);
    cancelBtn.disabled = cancelInFlight;
  }

  const sugBtn = $('btn-suggest-replies');
  if (sugBtn) {
    sugBtn.disabled = isBusy || isByok;
    sugBtn.title = isByok ? '自有 API 模式暂不支持回复灵感（切回宿主模型可用）' : '构思 3 条贴合人设的回应灵感';
  }
  const expBtn = $('btn-expand-intent');
  if (expBtn) {
    expBtn.disabled = isBusy || isByok;
    expBtn.title = isByok ? '自有 API 模式暂不支持意图扩写（切回宿主模型可用）' : '将输入框内的简短意图扩写为 3 版细腻 RP 文本';
  }

  document.querySelectorAll('[data-reroll-swipe]').forEach((b) => {
    b.disabled = isBusy;
  });
  document.querySelectorAll('[data-kartoittaja]').forEach((b) => {
    b.disabled = isBusy || isByok;
    b.title = isByok ? '自有 API 模式暂不支持三向支线（切回宿主模型可用）' : '推演 3 条不同走向的平行剧情支线';
  });
}

function renderRecoveryBanner(sess) {
  const banner = $('recovery-banner');
  if (!banner) return;
  const perr = sess?.last_persistence_error;
  const hasUncommitted = Boolean(perr && perr.uncommitted_turn);
  banner.classList.toggle('hidden', !hasUncommitted);
  if (hasUncommitted && $('recovery-text')) {
    $('recovery-text').textContent = '上一轮回复因文件占用尚未写入存档，关闭外部占用后可补写。';
  }
}

function renderAll() {
  if (!appState) return;
  const sess = appState.session || {};
  if (sess.rp_conv_id) {
    activeConvId = sess.rp_conv_id;
  } else if (!sess.active) {
    activeConvId = '';
  }
  const isBusy = Boolean(sess.stage_busy) || byokBusy;
  if ($('data-profile-badge')) $('data-profile-badge').textContent = appState.data_root_name || 'default-user';
  $('busy-dot')?.classList.toggle('busy', isBusy);
  $('busy-banner')?.classList.toggle('hidden', !sess.stage_busy);

  const charLabel = sess.character_name || '对方';
  const turnMode = sess.pending_turn_mode || 'normal';
  if ($('busy-text')) {
    if (turnMode === 'suggestions') {
      $('busy-text').textContent = '正在为你构思贴合心境的台词灵感…';
    } else if (turnMode === 'kartoittaja') {
      $('busy-text').textContent = `${charLabel} 的多条命运支线正在展开…`;
    } else if (turnMode === 'swipe') {
      $('busy-text').textContent = `${charLabel} 正在酝酿另一种回应…`;
    } else if (turnMode === 'director') {
      $('busy-text').textContent = '🎬 正在执行后台导演穿梭指令…';
    } else {
      $('busy-text').textContent = `${charLabel} 正在落笔回应…`;
    }
  }

  renderRecoveryBanner(sess);
  renderHero(sess);
  renderDimensionMixer(sess);
  renderVariables(appState.variables || {});
  renderChatStream(sess, appState.messages || []);
  renderSuggestions(appState.suggestions || {});
  renderCardsAndDoctor(false);
  renderInspector(sess);
  syncLorebookDropdown();
  renderGateModals();
  renderActionLocks(sess);
}

function renderGateModals() {
  const firstModal = $('first-run-modal');
  const acked = isFirstRunAcked();
  if (firstModal) {
    const noticeItems = appState?.first_run_notice?.items;
    if (!acked && Array.isArray(noticeItems) && noticeItems.length > 0 && $('first-run-items')) {
      $('first-run-items').innerHTML = noticeItems.map((t) => `<li>${formatNoticeItemHtml(t)}</li>`).join('');
    }
    firstModal.classList.toggle('hidden', acked);
  }

  const minorModal = $('minor-risk-modal');
  if (!minorModal) return;
  const gate = appState?.minor_risk_gate;
  const showMinor =
    acked &&
    gate &&
    (gate.gate_action === 'block_load' || (gate.gate_action === 'red_screen_warn' && !gate.warn_acked));
  minorModal.classList.toggle('hidden', !showMinor);
  if (showMinor) {
    const isHardBlock = gate.gate_action === 'block_load';
    $('minor-risk-title').textContent = isHardBlock
      ? '🛑 Card Doctor 未成年风险闸 · 默认拒绝装载'
      : '🛑 Card Doctor 未成年风险闸 · 强制红屏警告';
    $('minor-risk-lead').textContent = isHardBlock
      ? `角色卡「${gate.character_name || gate.card_file}」命中未成年高危特征，已被拒绝装载：`
      : `角色卡「${gate.character_name || gate.card_file}」检出疑似未成年启发式特征，默认暂停舞台交互：`;
    $('minor-risk-reasons').innerHTML = (gate.reasons || [])
      .map((r) => `<li>${escapeHtml(r)}</li>`)
      .join('');
    $('btn-minor-ack-adult').classList.toggle('hidden', isHardBlock);
  }
}

function syncSelectValue(selEl, targetVal) {
  if (!selEl || !targetVal) return;
  if (document.activeElement !== selEl && selEl.value !== targetVal) {
    selEl.value = targetVal;
  }
}

function renderHero(sess) {
  const charSel = $('char-select');
  const cards = appState.cards || [];
  const selectedCardFile =
    sess.card_file || cards.find((c) => c.name === sess.character_name)?.file || '';
  if (charSel) {
    const charOptionsHtml = cards
      .map((c) => {
        const blocked = c.minor_gate_action === 'block_load';
        const prefix = blocked ? '[⛔ 拒载] ' : c.minor_gate_action === 'red_screen_warn' ? '[⚠️ 待核] ' : '';
        return `<option value="${escapeHtml(c.file)}" ${blocked ? 'disabled' : ''} ${
          c.file === selectedCardFile ? 'selected' : ''
        }>${prefix}${escapeHtml(c.name)} (${escapeHtml(c.spec)})</option>`;
      })
      .join('');
    if (charSel.innerHTML !== charOptionsHtml) {
      charSel.innerHTML = charOptionsHtml || '<option value="">无可用角色卡</option>';
    }
    syncSelectValue(charSel, selectedCardFile);
  }

  if ($('mode-toggle-btn')) {
    $('mode-toggle-btn').textContent = sess.mode === 'chat' ? '💬 纯对白' : '🎭 剧场演绎';
  }
  if ($('scenario-line')) {
    $('scenario-line').textContent =
      appState.active_card?.scenario ||
      appState.active_card?.personality ||
      '在下方直接发送台词，或唤起玩家导演灵感。';
  }

  const chatSel = $('chat-select');
  const chats = appState.chats || [];
  if (chatSel) {
    const chatOptionsHtml = chats
      .map((ch) => {
        const displayLabel = String(ch.file || '').replace(/\.jsonl$/i, '');
        return `<option value="${escapeHtml(ch.file)}" ${
          ch.file === sess.chat_file ? 'selected' : ''
        }>${escapeHtml(displayLabel)}</option>`;
      })
      .join('');
    if (chatSel.innerHTML !== chatOptionsHtml) {
      chatSel.innerHTML = chatOptionsHtml || '<option value="">新建对话</option>';
    }
    syncSelectValue(chatSel, sess.chat_file || '');
  }

  const portrait = $('stage-portrait');
  const expr = appState.current_expression;
  const exprBadge = $('expr-badge');
  if (!portrait || !exprBadge) return;

  if (expr && (sess.character_name || sess.card_file)) {
    const targetSrc = mediaUrl({
      kind: 'expression',
      char: sess.character_name || '',
      card: sess.card_file || '',
      expr,
    });
    if (portrait.dataset.srcKey !== targetSrc) {
      portrait.dataset.srcKey = targetSrc;
      portrait.onerror = () => {
        portrait.onerror = null;
        portrait.dataset.srcKey = '';
        exprBadge.classList.add('hidden');
        portrait.src = cardAvatarUrl(sess.card_file);
      };
      portrait.src = targetSrc;
    }
    exprBadge.textContent = expr;
    exprBadge.classList.remove('hidden');
  } else if (sess.card_file && !isJsonCardFile(sess.card_file)) {
    const avatarSrc = cardAvatarUrl(sess.card_file);
    if (portrait.dataset.srcKey !== avatarSrc) {
      portrait.dataset.srcKey = avatarSrc;
      portrait.onerror = () => {
        portrait.onerror = null;
        portrait.dataset.srcKey = '';
        portrait.src = PLACEHOLDER_SVG;
      };
      portrait.src = avatarSrc;
    }
    exprBadge.classList.add('hidden');
  } else {
    portrait.onerror = null;
    portrait.dataset.srcKey = '';
    if (portrait.src !== PLACEHOLDER_SVG) {
      portrait.src = PLACEHOLDER_SVG;
    }
    exprBadge.classList.add('hidden');
  }
}

function renderDimensionMixer(sess) {
  const epLevel = Number(sess.epistemic_level ?? 0);
  const agenticOn = Boolean(sess.agentic_hands);
  const directorOn = Boolean(sess.director_mode);
  const isByok = appState?.plugin_settings?.generation_backend === 'byok';

  document.querySelectorAll('#mixer-epistemic-group .mixer-seg-btn').forEach((btn) => {
    const val = Number(btn.dataset.epistemic);
    btn.classList.toggle('active', val === epLevel);
  });

  const agBtn = $('mixer-agentic-btn');
  if (agBtn) {
    agBtn.textContent = agenticOn ? '🖐️ 权能释放' : '🔒 纯对白';
    agBtn.classList.toggle('active-agentic', agenticOn);
    agBtn.disabled = isByok && !agenticOn;
  }

  const dirBtn = $('mixer-director-btn');
  if (dirBtn) {
    dirBtn.textContent = directorOn ? '🎬 穿梭中 (Alt+D)' : '🎬 导演穿梭';
    dirBtn.classList.toggle('active-director', directorOn);
    dirBtn.disabled = isByok && !directorOn;
  }

  const composerEl = document.querySelector('.composer');
  const ta = $('composer-input');
  const sendBtn = $('btn-send-turn');
  if (composerEl && ta && sendBtn) {
    composerEl.classList.toggle('director-active', directorOn);
    if (directorOn) {
      ta.placeholder = '🎬 导演穿梭模式：输入改剧本/调设定/写代码指令（自动打上 is_backstage 场外隔离标记，Alt+D 切回演绎）…';
      sendBtn.textContent = '执行导演指令 ⚡';
    } else {
      ta.placeholder = '输入行动 (*...*) 或台词 ("...")，Enter 发送，Shift+Enter 换行，Alt+D 导演穿梭…';
      sendBtn.textContent = '发送推进 ▶';
    }
  }
}

function renderVariables(vars) {
  const listEl = $('vars-list');
  if (!listEl) return;
  const entries = Object.entries(vars);
  if (entries.length === 0) {
    listEl.innerHTML = '<span class="muted-label">暂无场景状态记录</span>';
    return;
  }
  listEl.innerHTML = entries
    .map(([k, v]) => {
      const displayVal = v !== null && typeof v === 'object' ? JSON.stringify(v) : v;
      return `
      <span class="var-chip">
        <strong>${escapeHtml(k)}:</strong> <span>${escapeHtml(displayVal)}</span>
        <button data-var-inc="${escapeHtml(k)}" title="+1">+1</button>
        <button data-var-del="${escapeHtml(k)}" title="移除状态">×</button>
      </span>`;
    })
    .join('');
}

function captureFloorEditSnapshot() {
  if (!editingFloorState) return;
  const ta = $(`edit-floor-ta-${editingFloorState.idx}`);
  if (!ta) return;
  editingFloorState.draft = ta.value;
  editingFloorState.focused = document.activeElement === ta;
  editingFloorState.selStart = typeof ta.selectionStart === 'number' ? ta.selectionStart : null;
  editingFloorState.selEnd = typeof ta.selectionEnd === 'number' ? ta.selectionEnd : null;
}

function restoreFloorEditSnapshot() {
  if (!editingFloorState) return;
  const ta = $(`edit-floor-ta-${editingFloorState.idx}`);
  if (!ta) return;
  if (ta.value !== editingFloorState.draft) {
    ta.value = editingFloorState.draft;
  }
  if (editingFloorState.focused) {
    ta.focus();
    if (editingFloorState.selStart !== null && editingFloorState.selEnd !== null) {
      try {
        ta.setSelectionRange(editingFloorState.selStart, editingFloorState.selEnd);
      } catch {
        // Ignore selection errors in headless environments
      }
    }
  }
}

function renderChatStream(sess, messages) {
  const stream = $('chat-stream');
  if (!stream) return;

  if (editingFloorState && (editingFloorState.chatFile !== (sess.chat_file || '') || editingFloorState.idx >= messages.length)) {
    editingFloorState = null;
  }
  if (expandedBackstageChat !== (sess.chat_file || '')) {
    expandedBackstageChat = sess.chat_file || '';
    expandedBackstageIdxs.clear();
  } else {
    stream.querySelectorAll('details.backstage-floor[data-floor-idx]').forEach((det) => {
      const i = Number(det.dataset.floorIdx);
      if (det.open) expandedBackstageIdxs.add(i);
      else expandedBackstageIdxs.delete(i);
    });
  }

  let hadLiveStream = streamDraftText !== null;
  if (hadLiveStream && activeStreamStop && messages.length > 0) {
    const lastMsg = messages[messages.length - 1];
    const lastSwipes = Array.isArray(lastMsg?.swipes) ? lastMsg.swipes.length : 0;
    const replyArrived =
      !lastMsg?.is_user &&
      ((streamDraftKind === 'swipe_reroll' && lastSwipes > streamBaseSwipeCount) ||
        (streamDraftKind !== 'swipe_reroll' && messages.length > streamBaseFloorCount));
    if (replyArrived) {
      activeStreamStop();
    }
  }

  const editIdx = editingFloorState ? editingFloorState.idx : null;
  const sig = JSON.stringify({
    edit: editIdx,
    chat: sess.chat_file,
    ep: sess.epistemic_level,
    dir: sess.director_mode,
    msgs: messages.map((m) => [m.mes, m.swipe_id, (m.swipes || []).length, Boolean(m.extra?.is_backstage)]),
  });
  if (sig === lastRenderedFloorSig) {
    renderStreamDraft();
    return;
  }

  captureFloorEditSnapshot();
  clearArmedConfirm();
  const wasNearBottom = stream.scrollHeight - stream.scrollTop - stream.clientHeight < 90;
  lastRenderedFloorSig = sig;
  finishTypewriter();

  if (messages.length === 0) {
    stream.innerHTML = '<div class="muted-label">剧场帷幕已启，发送第一句台词开始演绎。</div>';
    renderStreamDraft();
    return;
  }

  stream.innerHTML = messages
    .map((m, idx) => {
      const isUser = Boolean(m.is_user);
      const swipes = Array.isArray(m.swipes) && m.swipes.length > 0 ? m.swipes : [m.mes || ''];
      const swipeId = Math.min(Math.max(Number(m.swipe_id || 0), 0), swipes.length - 1);
      const activeText = swipes[swipeId] ?? m.mes ?? '';
      const isLastCharFloor = !isUser && idx === messages.length - 1;
      const exprTag = m.extra?.expression
        ? `<span class="brand-sub">${escapeHtml(m.extra.expression)}</span>`
        : '';

      if (editIdx === idx) {
        const draftVal = editingFloorState?.draft ?? activeText;
        return `
          <article class="floor-card ${isUser ? 'user-floor' : ''}">
            <div class="floor-hdr">
              <span class="floor-author">#${idx} ${escapeHtml(m.name || (isUser ? '你' : sess.character_name))} · 编辑中</span>
            </div>
            <textarea id="edit-floor-ta-${idx}" data-edit-floor-ta="${idx}" class="composer-textarea" rows="4">${escapeHtml(draftVal)}</textarea>
            <div class="btn-row end">
              <button class="mini-btn" data-floor-cancel="${idx}">取消</button>
              <button class="mini-btn accent" data-floor-save="${idx}">保存修改</button>
            </div>
          </article>`;
      }

      const swipeControls =
        swipes.length > 1
          ? `<div class="swipe-bar">
               <button class="tool-icon-btn" data-swipe-floor="${idx}" data-swipe-to="${
              (swipeId - 1 + swipes.length) % swipes.length
            }" title="上一分支">◀</button>
               <span>${swipeId + 1} / ${swipes.length}</span>
               <button class="tool-icon-btn" data-swipe-floor="${idx}" data-swipe-to="${
              (swipeId + 1) % swipes.length
            }" title="下一分支">▶</button>
               <button class="tool-icon-btn" data-prune-floor="${idx}" title="定稿此版回应并收起其余分支">✂️定稿</button>
             </div>`
          : '<span></span>';

      const lastCharActions = isLastCharFloor
        ? `<div class="swipe-bar">
             <button class="tool-icon-btn" data-reroll-swipe="1" title="重新演绎这一幕">🔄 重演</button>
             <button class="tool-icon-btn" data-kartoittaja="1" title="推演 3 条不同走向的平行剧情支线">🔀 三向支线</button>
           </div>`
        : '';

      const showFooter = swipes.length > 1 || isLastCharFloor;
      const isBackstage = Boolean(m.extra?.is_backstage);
      const quarantineIsolated = isBackstage && Number(sess.epistemic_level ?? 0) < 100 && !sess.director_mode;

      if (isBackstage) {
        const isOpen = expandedBackstageIdxs.has(idx) ? 'open' : '';
        return `
          <details class="floor-card backstage-floor ${isUser ? 'user-floor' : ''}" data-floor-idx="${idx}" ${isOpen}>
            <summary class="backstage-summary">
              <span>🎬 后台导演便签 #${idx} · ${escapeHtml(m.name || (isUser ? '你' : sess.character_name))} ${
          quarantineIsolated ? '（已从深潜上下文隔离）' : '（双轨/穿梭可见）'
        }</span>
              <span class="floor-tools">
                <button class="tool-icon-btn" data-floor-backstage="${idx}" title="取消场外隔离标记，并入正史">🔓并入正史</button>
                <button class="tool-icon-btn" data-floor-del="${idx}" title="删除此便签">🗑️</button>
              </span>
            </summary>
            <div class="floor-body">${formatRpProse(activeText)}</div>
          </details>`;
      }

      return `
        <article class="floor-card ${isUser ? 'user-floor' : ''}">
          <div class="floor-hdr">
            <div>
              <span class="floor-author">${escapeHtml(m.name || (isUser ? '你' : sess.character_name))}</span>
              <span>#${idx}</span>
              ${exprTag}
            </div>
            <div class="floor-tools">
              <button class="tool-icon-btn" data-floor-backstage="${idx}" title="标记为后台导演便签 (extra.is_backstage: true)，在 0%/50% 深潜时自动隔离">🎬场外</button>
              <button class="tool-icon-btn" data-floor-edit="${idx}" title="润色此幕台词">✏️</button>
              <button class="tool-icon-btn" data-floor-fork="${idx}" title="从此幕开辟新故事线">🌿分叉</button>
              <button class="tool-icon-btn" data-floor-del="${idx}" title="撤回此幕">🗑️</button>
            </div>
          </div>
          <div class="floor-body">${formatRpProse(activeText)}</div>
          ${showFooter ? `<div class="floor-footer">${swipeControls}${lastCharActions}</div>` : ''}
        </article>`;
    })
    .join('');

  restoreFloorEditSnapshot();
  if (wasNearBottom && editIdx === null) {
    stream.scrollTop = stream.scrollHeight;
  }
  renderStreamDraft();
  maybeTypewriteLastFloor(sess, messages, hadLiveStream);
}

function renderSuggestions(sugObj) {
  const items = Array.isArray(sugObj.suggestions) ? sugObj.suggestions : [];
  const box = $('suggestions-box');
  if (!box) return;
  if (items.length === 0) {
    box.classList.add('hidden');
    return;
  }
  box.classList.remove('hidden');
  $('suggestions-title').textContent =
    sugObj.kind === 'expanded_intent'
      ? '🪄 意图扩写候选（点击填入输入框，Shift+点击直接发送）'
      : '✨ 玩家回复灵感（点击填入输入框，Shift+点击直接发送）';
  $('suggestions-list').innerHTML = items
    .map(
      (txt, idx) =>
        `<button class="suggestion-chip" data-sug-idx="${idx}">${formatRpProse(txt)}</button>`
    )
    .join('');
}

function renderIssueItemsHtml(issues) {
  const allowedSev = new Set(['critical', 'error', 'warning']);
  return issues
    .map((iss) => {
      const sev = allowedSev.has(iss.severity) ? iss.severity : 'warning';
      return `
        <div class="doctor-item ${sev}">
          <div><strong>[${escapeHtml(iss.scope || '体检')} · ${escapeHtml(iss.code)}]</strong></div>
          <div>${escapeHtml(iss.message)}</div>
          ${iss.fix_hint ? `<div class="muted-label">💡 修复建议：${escapeHtml(iss.fix_hint)}</div>` : ''}
        </div>`;
    })
    .join('');
}

function renderCardsAndDoctor(forceRender = false) {
  const doc = appState?.doctor || { total_issues: 0, card_issues: [], lorebook_issues: [] };
  const badge = $('doctor-badge');
  if (badge) {
    badge.textContent = String(doc.total_issues || 0);
    badge.classList.toggle('hidden', !doc.total_issues);
  }

  const allIssues = [
    ...(doc.card_issues || []).map((i) => ({ ...i, scope: '角色卡' })),
    ...(doc.lorebook_issues || []).map((i) => ({ ...i, scope: '世界书' })),
  ];
  const docList = $('doctor-list');
  const doctorSig = JSON.stringify(allIssues);
  if (docList && (forceRender || doctorSig !== lastRenderedDoctorSig)) {
    lastRenderedDoctorSig = doctorSig;
    docList.innerHTML =
      allIssues.length === 0
        ? '<div class="muted-label">✅ 当前角色卡与关联世界书未检出结构隐患。</div>'
        : renderIssueItemsHtml(allIssues);
  }

  const isCardsActive = forceRender || Boolean($('panel-cards')?.classList.contains('active'));
  if (!isCardsActive) return;

  const grid = $('cards-grid');
  if (!grid) return;
  const q = ($('card-search-input')?.value || '').trim().toLowerCase();
  const cardsSig = JSON.stringify({ etag: currentEtag, q, cards: appState?.cards || [] });
  if (!forceRender && cardsSig === lastRenderedCardsSig) return;
  lastRenderedCardsSig = cardsSig;

  const filtered = (appState?.cards || []).filter(
    (c) =>
      !q ||
      c.name.toLowerCase().includes(q) ||
      (c.tags || []).some((t) => String(t).toLowerCase().includes(q))
  );
  if (filtered.length === 0) {
    grid.innerHTML = '<div class="muted-label">未找到匹配的角色卡。</div>';
    return;
  }
  grid.innerHTML = filtered
    .map((c) => {
      const blocked = c.minor_gate_action === 'block_load';
      const warn = c.minor_gate_action === 'red_screen_warn';
      const actionHtml = blocked
        ? '<span class="badge-blocked" title="Card Doctor 未成年风险闸默认拒绝装载">⛔ 未成年风险拒载</span>'
        : `<button class="mini-btn accent" data-boot-card="${escapeHtml(c.file)}">登台演绎</button>${
            warn ? ' <span class="badge-warn-gate">⚠️ 待核年龄</span>' : ''
          }`;
      return `
      <div class="gallery-card">
        <img class="gallery-thumb" src="${escapeHtml(cardAvatarUrl(c.file))}" loading="lazy" decoding="async" alt="${escapeHtml(c.name)}" />
        <div class="gallery-meta">
          <div class="gallery-name">${escapeHtml(c.name)}</div>
          <div class="muted-label">${escapeHtml(c.spec)} · 差分 ${c.expressions_count || 0} 张</div>
          ${c.world ? `<div class="muted-label">🌍 ${escapeHtml(c.world)}</div>` : ''}
          <div>${actionHtml}</div>
        </div>
      </div>`;
    })
    .join('');
}

function renderInspector(sess) {
  const dump = appState.dump || {};
  const ratio = Number(dump.prefix_cache_ratio_pct || 0);
  if ($('cache-ratio-label')) $('cache-ratio-label').textContent = `${ratio}% Static Prefix`;
  if ($('cache-meter-bar')) $('cache-meter-bar').style.width = `${Math.min(100, Math.max(0, ratio))}%`;
  if ($('token-stats-line')) {
    const qCount = Number(dump.quarantined_backstage_count || 0);
    const qSuffix = qCount > 0 ? ` · 隔离便签: ${qCount}` : '';
    $('token-stats-line').textContent = `Static: ${dump.static_tokens || 0} tok · Dynamic: ${
      dump.dynamic_tokens || 0
    } tok · Lore: ${dump.lore_used_tokens || 0}/${dump.lore_budget || 2048} tok${qSuffix}`;
  }
  if ($('dump-report-pre')) $('dump-report-pre').textContent = dump.dump_report || '暂无透视详情';

  if ($('cfg-byok-status')) {
    const b = appState.byok || {};
    let statusText = b.key_present
      ? `已检测到 API Key（来源：${b.key_source === 'env' ? '环境变量' : '状态目录文件'}）${b.model ? '' : '，尚未填写模型名'}`
      : '未检测到 API Key：请设置环境变量 TAVERN_BYOK_API_KEY（插件不会保存或回显密钥）';
    const u = b.last_usage;
    if (u && u.prompt_tokens) {
      statusText += u.cached_tokens != null
        ? ` | 上一回合缓存命中 ${u.cached_tokens} / ${u.prompt_tokens} 输入 tokens（${Math.round((u.cached_tokens / u.prompt_tokens) * 100)}%）`
        : ` | 上一回合输入 ${u.prompt_tokens} tokens（接口未返回缓存命中数）`;
    }
    $('cfg-byok-status').textContent = statusText;
  }

  const sessKey = `${sess.card_file || ''}|${sess.chat_file || ''}`;
  if (sessKey !== lastInspectorSessionKey) {
    lastInspectorSessionKey = sessKey;
    inspectorDirty = false;
  }
  if ($('cfg-byok-base-url')) {
    $('cfg-byok-base-url').placeholder = appState.byok?.base_url || '';
  }

  const presetSel = $('cfg-preset-select');
  const presets = appState.presets || ['Default'];
  if (presetSel) {
    const presetHtml = presets
      .map(
        (p) =>
          `<option value="${escapeHtml(p)}" ${
            p === sess.preset_name ? 'selected' : ''
          }>${escapeHtml(p)}</option>`
      )
      .join('');
    if (presetSel.innerHTML !== presetHtml) {
      presetSel.innerHTML = presetHtml;
    }
  }

  if (inspectorDirty) return;

  const activeEl = document.activeElement;
  if (presetSel) syncSelectValue(presetSel, sess.preset_name || 'Default');

  const pcfg = appState.plugin_settings || {};
  const policyMode = pcfg.policy_mode || 'host_gentle';
  if ($('cfg-policy-select') && activeEl !== $('cfg-policy-select')) {
    $('cfg-policy-select').value = policyMode;
  }
  if ($('cfg-backend-select') && activeEl !== $('cfg-backend-select')) {
    $('cfg-backend-select').value = pcfg.generation_backend === 'byok' ? 'byok' : 'host';
  }
  if ($('cfg-byok-base-url') && activeEl !== $('cfg-byok-base-url')) {
    $('cfg-byok-base-url').value = pcfg.byok_base_url || '';
  }
  if ($('cfg-byok-model') && activeEl !== $('cfg-byok-model')) {
    $('cfg-byok-model').value = pcfg.byok_model || '';
  }
  if ($('cfg-byok-cache-mode') && activeEl !== $('cfg-byok-cache-mode')) {
    $('cfg-byok-cache-mode').value = pcfg.byok_cache_mode === 'prefix_pinned' ? 'prefix_pinned' : 'off';
  }
  if ($('cfg-byok-cache-control') && activeEl !== $('cfg-byok-cache-control')) {
    $('cfg-byok-cache-control').checked = Boolean(pcfg.byok_cache_control);
  }
  if ($('cfg-byok-stream') && activeEl !== $('cfg-byok-stream')) {
    $('cfg-byok-stream').checked = pcfg.byok_stream !== false;
  }
  if ($('cfg-typewriter') && activeEl !== $('cfg-typewriter')) {
    $('cfg-typewriter').checked = typewriterEnabled();
  }
  if ($('cfg-an-depth') && activeEl !== $('cfg-an-depth')) {
    $('cfg-an-depth').value = String(sess.an_depth ?? 4);
  }
  if ($('cfg-an-text') && activeEl !== $('cfg-an-text')) {
    $('cfg-an-text').value = sess.authors_note || '';
  }
}

// ---------------------------------------------------------------------------
// World Studio Logic
// ---------------------------------------------------------------------------
function syncLorebookDropdown() {
  const sel = $('lorebook-select');
  if (!sel) return;
  const books = appState.lorebooks || [];
  const cardWorld = String(appState.active_card?.world || '').trim();
  if (cardWorld !== lastLoreCardWorld) {
    lastLoreCardWorld = cardWorld;
    if (cardWorld && books.includes(cardWorld) && loadedLorebook.lorebook_name !== cardWorld) {
      loadedLorebook = { lorebook_name: '', entries: [], issues: [] };
    }
  }

  const validLoaded = loadedLorebook.lorebook_name && books.includes(loadedLorebook.lorebook_name)
    ? loadedLorebook.lorebook_name
    : '';
  const targetBook = validLoaded || (cardWorld && books.includes(cardWorld) ? cardWorld : '') || books[0] || '';
  const html = books
    .map(
      (b) =>
        `<option value="${escapeHtml(b)}" ${b === targetBook ? 'selected' : ''}>${escapeHtml(b)}</option>`
    )
    .join('');
  if (sel.innerHTML !== html) {
    sel.innerHTML = html || '<option value="">暂无世界书</option>';
  }
  syncSelectValue(sel, targetBook);
  const onCooldown =
    failedLorebookCooldown.name === targetBook && Date.now() < failedLorebookCooldown.until;
  if (targetBook && !loadedLorebook.lorebook_name && !loadingLorebookName && !onCooldown) {
    loadLorebookDetail(targetBook);
  }
}

async function loadLorebookDetail(bookName) {
  if (!bookName || loadingLorebookName === bookName) return;
  loadingLorebookName = bookName;
  try {
    const detail = await callOp(
      'read_lorebook',
      { lorebook_name: bookName },
      { skipStateRefresh: true }
    );
    loadedLorebook = {
      lorebook_name: detail?.lorebook_name || bookName,
      entries: Array.isArray(detail?.entries) ? detail.entries : [],
      issues: Array.isArray(detail?.issues) ? detail.issues : [],
    };
    failedLorebookCooldown = { name: '', until: 0 };
    syncLorebookDropdown();
    if ($('lorebook-select') && loadedLorebook.lorebook_name) {
      $('lorebook-select').value = loadedLorebook.lorebook_name;
    }
    renderLoreEntries();
  } catch (err) {
    failedLorebookCooldown = { name: bookName, until: Date.now() + 15000 };
    showToast(err.message, true);
  } finally {
    if (loadingLorebookName === bookName) {
      loadingLorebookName = '';
    }
  }
}

function renderLoreEntries() {
  const entries = loadedLorebook.entries || [];
  const issues = loadedLorebook.issues || [];
  if ($('lore-count-label')) $('lore-count-label').textContent = `${entries.length} 条`;

  const issuesEl = $('lore-issues-list');
  if (issuesEl) {
    issuesEl.classList.toggle('hidden', issues.length === 0);
    issuesEl.innerHTML = issues.length > 0
      ? renderIssueItemsHtml(issues.map((i) => ({ ...i, scope: loadedLorebook.lorebook_name || '世界书' })))
      : '';
  }

  const list = $('lore-entries-list');
  if (!list) return;
  if (entries.length === 0) {
    list.innerHTML = '<div class="muted-label">当前世界书为空，点击「+ 新增条目」创建。</div>';
    return;
  }
  list.innerHTML = entries
    .map(
      (e) => `
      <div class="lore-item">
        <div class="section-hdr">
          <strong>#${escapeHtml(e.uid)} ${escapeHtml(e.comment || '(无标题)')}</strong>
          <div class="btn-row">
            <button class="mini-btn" data-lore-toggle="${escapeHtml(e.uid)}">${e.enabled ? '🟢 已启用' : '⚪ 已停用'}</button>
            <button class="mini-btn" data-lore-edit="${escapeHtml(e.uid)}">编辑</button>
          </div>
        </div>
        <div class="muted-label">Keys: ${escapeHtml((e.keys || []).join(', ') || '(constant)')} · ${escapeHtml(
        e.position
      )} ${e.position === 'at_depth' ? `@D${escapeHtml(e.depth)}` : ''} · Order ${escapeHtml(e.order)}</div>
      </div>`
    )
    .join('');
}

function openLoreEditor(entry = null) {
  clearArmedConfirm();
  $('lore-editor-card').classList.remove('hidden');
  $('lore-editor-title').textContent = entry ? `编辑条目 #${entry.uid}` : '新增世界书条目';
  $('le-uid').value = entry ? String(entry.uid) : '';
  $('le-comment').value = entry?.comment || '';
  $('le-keys').value = (entry?.keys || []).join(', ');
  $('le-keys-sec').value = (entry?.keys_secondary || []).join(', ');
  $('le-sec-logic').value = String(entry?.selective_logic ?? 0);
  const posVal = entry?.position || 'before_char';
  const posSel = $('le-position');
  if (posSel) {
    if (!Array.from(posSel.options).some((o) => o.value === posVal)) {
      const opt = document.createElement('option');
      opt.value = posVal;
      opt.textContent = posVal;
      posSel.appendChild(opt);
    }
    posSel.value = posVal;
  }
  $('le-depth').value = String(entry?.depth ?? 4);
  $('le-order').value = String(entry?.order ?? 100);
  $('le-selective').checked = Boolean(entry?.selective);
  $('le-constant').checked = Boolean(entry?.constant);
  $('le-enabled').checked = entry ? Boolean(entry.enabled) : true;
  $('le-content').value = entry?.content || '';
  $('btn-delete-lore').classList.toggle('hidden', !entry);
}

// ---------------------------------------------------------------------------
// Event Listeners Wiring
// ---------------------------------------------------------------------------
function bindWorkbenchEvents() {
  document.querySelectorAll('.tab-btn').forEach((btn) => {
    btn.addEventListener('click', () => {
      const target = btn.dataset.tab;
      document.querySelectorAll('.tab-btn').forEach((b) => {
        const isActive = b === btn;
        b.classList.toggle('active', isActive);
        b.setAttribute('aria-selected', isActive ? 'true' : 'false');
      });
      document.querySelectorAll('.tab-panel').forEach((p) => {
        p.classList.toggle('active', p.id === `panel-${target}`);
      });
      if (target === 'cards') {
        renderCardsAndDoctor(true);
      } else if (target === 'lore' && !loadedLorebook.lorebook_name && $('lorebook-select')?.value) {
        loadLorebookDetail($('lorebook-select').value);
      }
    });
  });

  $('char-select')?.addEventListener('change', (e) => {
    if (e.target.value) {
      resetEditingFloor();
      callOp('bootstrap', { character_name: e.target.value, mode: appState?.session?.mode || 'play' }).catch((err) => {
        showToast(err.message, true);
        if (appState?.session) renderHero(appState.session);
      });
    }
  });

  $('chat-select')?.addEventListener('change', (e) => {
    if (e.target.value && appState?.session?.character_name) {
      resetEditingFloor();
      callOp('bootstrap', {
        character_name: appState.session.character_name,
        chat_file: e.target.value,
        mode: appState.session.mode || 'play',
      }).catch((err) => {
        showToast(err.message, true);
        if (appState?.session) renderHero(appState.session);
      });
    }
  });

  $('new-chat-btn')?.addEventListener('click', () => {
    if (!appState?.session?.character_name) return;
    resetEditingFloor();
    callOp('bootstrap', {
      character_name: appState.session.character_name,
      new_chat: true,
      mode: appState.session.mode || 'play',
    })
      .then(() => showToast('已开启新的故事线'))
      .catch((err) => showToast(err.message, true));
  });

  $('mode-toggle-btn')?.addEventListener('click', () => {
    const nextMode = appState?.session?.mode === 'chat' ? 'play' : 'chat';
    callOp('switch_profile', { mode: nextMode }).catch((err) => showToast(err.message, true));
  });

  // Dimension Mixer Events (Switch A: Epistemic | Switch B: Agentic Hands | Switch C: Director Shuttle)
  $('mixer-epistemic-group')?.addEventListener('click', (e) => {
    const btn = e.target.closest?.('[data-epistemic]');
    if (!btn) return;
    const nextLevel = Number(btn.dataset.epistemic);
    callOp('switch_profile', { epistemic_level: nextLevel })
      .then(() => {
        const labels = {
          0: '0% 深潜封闭（四重维度锁全开 · 后台便签已隔离）',
          50: '50% 隐秘默契（诗意共鸣 · 后台便签已隔离）',
          100: '100% 双轨化身（全知视野已解锁）',
        };
        showToast(`认知透光度已切换至：${labels[nextLevel] || nextLevel}`);
      })
      .catch((err) => showToast(err.message, true));
  });

  $('mixer-agentic-btn')?.addEventListener('click', () => {
    const nextAgentic = !Boolean(appState?.session?.agentic_hands);
    callOp('switch_profile', { agentic_hands: nextAgentic })
      .then(() =>
        showToast(
          nextAgentic
            ? '🖐️ 已开启 Agentic 权能释放：允许角色自主更新状态与演化世界书'
            : '🔒 已切回纯对白守序模式：工具列表已物理拔除'
        )
      )
      .catch((err) => showToast(err.message, true));
  });

  $('mixer-director-btn')?.addEventListener('click', toggleDirectorShuttle);

  // Variables Events
  $('add-var-btn')?.addEventListener('click', () => {
    $('var-editor-row')?.classList.toggle('hidden');
    $('var-key-input')?.focus();
  });
  $('var-cancel-btn')?.addEventListener('click', () => $('var-editor-row')?.classList.add('hidden'));
  $('var-save-btn')?.addEventListener('click', saveInlineVariable);
  const handleVarEnter = (e) => {
    if (e.key === 'Enter' && !e.isComposing) {
      e.preventDefault();
      saveInlineVariable();
    }
  };
  $('var-key-input')?.addEventListener('keydown', handleVarEnter);
  $('var-val-input')?.addEventListener('keydown', handleVarEnter);

  $('vars-list')?.addEventListener('click', (e) => {
    const btn = e.target.closest?.('button') || e.target;
    const incKey = btn?.dataset?.varInc;
    const delKey = btn?.dataset?.varDel;
    if (incKey !== undefined && incKey !== '') {
      callOp('manage_variables', { scope: 'chat', op: 'inc', key: incKey, value: 1 }).catch((err) =>
        showToast(err.message, true)
      );
    } else if (delKey !== undefined && delKey !== '') {
      callOp('manage_variables', { scope: 'chat', op: 'del', key: delKey }).catch((err) =>
        showToast(err.message, true)
      );
    }
  });

  // Delegated Chat Stream Floor Operations & Draft Sync
  $('chat-stream')?.addEventListener('input', (e) => {
    const idxAttr = e.target?.dataset?.editFloorTa;
    if (idxAttr !== undefined && editingFloorState && Number(idxAttr) === editingFloorState.idx) {
      editingFloorState.draft = e.target.value;
    }
  });

  $('chat-stream')?.addEventListener('click', async (e) => {
    const btn = e.target.closest?.('button') || e.target;
    const d = btn?.dataset || {};
    try {
      if (d.floorEdit !== undefined) {
        const idx = Number(d.floorEdit);
        const msg = appState?.messages?.[idx];
        const swipes = Array.isArray(msg?.swipes) && msg.swipes.length > 0 ? msg.swipes : [msg?.mes || ''];
        const swipeId = Math.min(Math.max(Number(msg?.swipe_id || 0), 0), swipes.length - 1);
        editingFloorState = {
          chatFile: appState?.session?.chat_file || '',
          idx,
          draft: swipes[swipeId] ?? msg?.mes ?? '',
          selStart: null,
          selEnd: null,
          focused: true,
        };
        lastRenderedFloorSig = '';
        renderChatStream(appState.session, appState.messages);
      } else if (d.floorCancel !== undefined) {
        resetEditingFloor();
        renderChatStream(appState.session, appState.messages);
      } else if (d.floorSave !== undefined) {
        const idx = Number(d.floorSave);
        const newText = $(`edit-floor-ta-${idx}`)?.value ?? editingFloorState?.draft ?? '';
        resetEditingFloor();
        await callOp('floor_op', { operation: 'edit_floor', floor_index: idx, new_text: newText });
        showToast(`第 #${idx} 幕已保存`);
      } else if (d.floorBackstage !== undefined) {
        e.preventDefault();
        const res = await callOp('floor_op', {
          operation: 'toggle_backstage',
          floor_index: Number(d.floorBackstage),
        });
        showToast(res.is_backstage ? '已标记为后台导演便签（深潜时自动隔离）' : '已解除隔离并入故事正史');
      } else if (d.floorDel !== undefined) {
        e.preventDefault();
        const delIdx = Number(d.floorDel);
        if (!requireInlineConfirm(btn, `floor-del-${delIdx}`, '确认撤回？')) return;
        if (editingFloorState && editingFloorState.chatFile === (appState?.session?.chat_file || '')) {
          captureFloorEditSnapshot();
          if (delIdx === editingFloorState.idx) {
            editingFloorState = null;
          } else if (delIdx < editingFloorState.idx) {
            editingFloorState.idx -= 1;
          }
        }
        await callOp('floor_op', { operation: 'delete_floor', floor_index: delIdx });
        showToast('已撤回该幕内容');
      } else if (d.floorFork !== undefined) {
        resetEditingFloor();
        await callOp('floor_op', {
          operation: 'fork_from_floor',
          floor_index: Number(d.floorFork),
          activate_fork: true,
        });
        showToast('已从此处开辟新故事线');
      } else if (d.swipeFloor !== undefined) {
        await callOp('floor_op', {
          operation: 'switch_swipe',
          floor_index: Number(d.swipeFloor),
          swipe_id: Number(d.swipeTo),
        });
      } else if (d.pruneFloor !== undefined) {
        const pruneIdx = Number(d.pruneFloor);
        if (!requireInlineConfirm(btn, `floor-prune-${pruneIdx}`, '确认定稿？')) return;
        await callOp('floor_op', { operation: 'prune_swipes', floor_index: pruneIdx });
        showToast('已定稿此版回应并收起其余分支');
      } else if (d.rerollSwipe) {
        await dispatchAgentTurn('swipe_reroll');
      } else if (d.kartoittaja) {
        await dispatchAgentTurn('kartoittaja_branches');
      }
    } catch (err) {
      showToast(err.message, true);
    }
  });

  // Uncommitted Turn Recovery & Turn Cancellation
  $('btn-retry-uncommitted')?.addEventListener('click', async () => {
    const btn = $('btn-retry-uncommitted');
    if (btn) btn.disabled = true;
    try {
      await callOp('retry_uncommitted_turn', {});
      showToast('未落盘回复已补写至存档');
    } catch (err) {
      showToast(err.message || String(err), true);
    } finally {
      if (btn) btn.disabled = false;
    }
  });

  $('btn-cancel-turn')?.addEventListener('click', async () => {
    if (cancelInFlight) return;
    cancelInFlight = true;
    const wasByok = byokBusy;
    if (wasByok) userCancelledTurn = true;
    if (activeStreamStop) activeStreamStop();
    finishTypewriter();
    renderActionLocks(appState?.session || {});
    try {
      await callOp('cancel_turn', wasByok ? {} : { force_reset: true });
      showToast(wasByok ? '已停止当前生成' : '已重置舞台忙碌状态');
    } catch (err) {
      showToast(err.message || String(err), true);
    } finally {
      cancelInFlight = false;
      renderActionLocks(appState?.session || {});
    }
  });

  // Suggestions & Composer
  $('clear-suggestions-btn')?.addEventListener('click', () => {
    callOp('clear_suggestions').catch((err) => showToast(err.message, true));
  });

  $('suggestions-list')?.addEventListener('click', async (e) => {
    const btn = e.target.closest?.('[data-sug-idx]');
    if (!btn) return;
    const idx = Number(btn.dataset.sugIdx);
    const text = appState?.suggestions?.suggestions?.[idx];
    if (!text) return;
    if (e.shiftKey) {
      const prevFloorCount = (appState?.messages || []).length;
      const ok = await dispatchAgentTurn('send_turn', { user_text: text }, { prevFloorCount });
      if (ok) {
        await callOp('clear_suggestions').catch(() => {});
      }
    } else {
      $('composer-input').value = text;
      $('composer-input').focus();
    }
  });

  $('btn-send-turn')?.addEventListener('click', submitComposerTurn);
  $('composer-input')?.addEventListener('keydown', (e) => {
    if (e.altKey && (e.key === 'd' || e.key === 'D')) {
      e.preventDefault();
      toggleDirectorShuttle();
      return;
    }
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      submitComposerTurn();
    }
  });

  $('btn-suggest-replies')?.addEventListener('click', () => dispatchAgentTurn('suggest_replies'));
  $('btn-expand-intent')?.addEventListener('click', () => {
    const text = $('composer-input').value.trim();
    dispatchAgentTurn('expand_intent', { user_text: text });
  });

  // Cards Import & Doctor
  const dropzone = $('card-dropzone');
  const fileInput = $('card-file-input');
  if (dropzone && fileInput) {
    dropzone.addEventListener('click', () => fileInput.click());
    dropzone.addEventListener('dragover', (e) => {
      e.preventDefault();
      dropzone.classList.add('dragover');
    });
    dropzone.addEventListener('dragleave', () => dropzone.classList.remove('dragover'));
    dropzone.addEventListener('drop', (e) => {
      e.preventDefault();
      dropzone.classList.remove('dragover');
      const file = e.dataTransfer?.files?.[0];
      if (file) importCardFile(file);
    });
    fileInput.addEventListener('change', (e) => {
      const file = e.target.files?.[0];
      if (file) importCardFile(file);
      fileInput.value = '';
    });
  }

  $('card-search-input')?.addEventListener('input', () => renderCardsAndDoctor(true));
  $('cards-grid')?.addEventListener('click', (e) => {
    const btn = e.target.closest?.('button') || e.target;
    const file = btn?.dataset?.bootCard;
    if (!file) return;
    resetEditingFloor();
    callOp('bootstrap', { character_name: file, mode: appState?.session?.mode || 'play' })
      .then(() => {
        document.querySelector('[data-tab="stage"]')?.click();
      })
      .catch((err) => showToast(err.message, true));
  });

  $('btn-run-lint')?.addEventListener('click', async () => {
    try {
      const res = await callOp('lint', {});
      showToast(res.healthy ? '角色卡与世界书状态完好' : `发现 ${res.total_issues} 项可优化的设定细节`);
    } catch (err) {
      showToast(err.message, true);
    }
  });

  $('btn-seppa-fix')?.addEventListener('click', async () => {
    if (!isFirstRunAcked()) {
      $('first-run-modal')?.classList.remove('hidden');
      showToast('请先阅读并确认首次运行须知', false);
      return;
    }
    const charName = appState?.session?.character_name || $('char-select')?.value;
    if (!charName) {
      showToast('请先选择需要体检修复的角色卡', true);
      return;
    }
    if (!window.sidecar?.agent?.startConversation || window.sidecar.standalone) {
      showToast('当前处于独立预览模式，请在右侧舞台面板中唤起自动修复。', true);
      return;
    }
    const prompt = `/card-forge 请运行 st_lint_card_and_lore 检查角色「${charName}」及其关联世界书，自动修复检出的兼容项（如补齐 CCv2+CCv3 双规范数据、补齐 {{original}} 或修正 selective 次级词门控）并保存写回。`;
    try {
      await window.sidecar.agent.startConversation(prompt, `卡片工坊修复 · ${charName}`);
      showToast('已在独立工坊会话中启动兼容项修复');
    } catch (err) {
      showToast(err.message || String(err), true);
    }
  });

  // World Studio Events
  $('lorebook-select')?.addEventListener('change', (e) => loadLorebookDetail(e.target.value));
  $('btn-new-entry')?.addEventListener('click', () => openLoreEditor(null));
  $('btn-close-lore-editor')?.addEventListener('click', () => {
    clearArmedConfirm();
    $('lore-editor-card')?.classList.add('hidden');
  });
  $('le-keys-sec')?.addEventListener('input', (e) => {
    if (e.target.value.trim() && $('le-selective')) {
      $('le-selective').checked = true;
    }
  });

  $('lore-entries-list')?.addEventListener('click', async (e) => {
    const btn = e.target.closest?.('button') || e.target;
    const toggleUid = btn?.dataset?.loreToggle;
    const editUid = btn?.dataset?.loreEdit;
    const book = loadedLorebook.lorebook_name || $('lorebook-select')?.value;
    if (toggleUid !== undefined && book) {
      try {
        await callOp('evolve_lore', {
          lorebook_name: book,
          operation: 'toggle',
          entry_payload: { uid: Number(toggleUid) },
        });
        await loadLorebookDetail(book);
      } catch (err) {
        showToast(err.message, true);
      }
    } else if (editUid !== undefined) {
      const found = (loadedLorebook.entries || []).find((x) => Number(x.uid) === Number(editUid));
      if (found) openLoreEditor(found);
    }
  });

  $('btn-save-lore')?.addEventListener('click', async () => {
    const book =
      $('new-lorebook-input')?.value.trim() ||
      loadedLorebook.lorebook_name ||
      $('lorebook-select')?.value ||
      'CustomWorld';
    const uidRaw = $('le-uid').value.trim();
    const secKeys = $('le-keys-sec').value.split(',').map((s) => s.trim()).filter(Boolean);
    const payload = {
      comment: $('le-comment').value.trim(),
      keys: $('le-keys').value.split(',').map((s) => s.trim()).filter(Boolean),
      keys_secondary: secKeys,
      selective_logic: Number($('le-sec-logic').value),
      position: $('le-position').value || 'before_char',
      depth: Number($('le-depth').value || 4),
      order: Number($('le-order').value || 100),
      selective: $('le-selective').checked,
      constant: $('le-constant').checked,
      enabled: $('le-enabled').checked,
      content: $('le-content').value,
    };
    if (uidRaw !== '') payload.uid = Number(uidRaw);
    try {
      await callOp('evolve_lore', { lorebook_name: book, operation: 'upsert', entry_payload: payload });
      $('new-lorebook-input').value = '';
      $('lore-editor-card').classList.add('hidden');
      await loadLorebookDetail(book);
      showToast(`已保存至《${book}》`);
    } catch (err) {
      showToast(err.message, true);
    }
  });

  $('btn-delete-lore')?.addEventListener('click', async () => {
    const delBtn = $('btn-delete-lore');
    const book = loadedLorebook.lorebook_name || $('lorebook-select')?.value;
    const uidRaw = $('le-uid').value.trim();
    if (!book || uidRaw === '') return;
    if (!requireInlineConfirm(delBtn, `lore-del-${book}-${uidRaw}`, '确认删除？')) return;
    try {
      await callOp('evolve_lore', {
        lorebook_name: book,
        operation: 'delete',
        entry_payload: { uid: Number(uidRaw) },
      });
      $('lore-editor-card').classList.add('hidden');
      await loadLorebookDetail(book);
      showToast('词条已删除');
    } catch (err) {
      showToast(err.message, true);
    }
  });

  $('btn-sim-lore')?.addEventListener('click', runLoreSimulation);
  $('lore-sim-input')?.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.isComposing) {
      e.preventDefault();
      runLoreSimulation();
    }
  });

  // Inspector Config Dirty Tracking & Save
  const cfgInputIds = [
    'cfg-preset-select',
    'cfg-policy-select',
    'cfg-an-depth',
    'cfg-backend-select',
    'cfg-byok-base-url',
    'cfg-byok-model',
    'cfg-byok-cache-mode',
    'cfg-byok-cache-control',
    'cfg-byok-stream',
    'cfg-typewriter',
    'cfg-an-text',
  ];
  cfgInputIds.forEach((id) => {
    const el = $(id);
    if (!el) return;
    el.addEventListener('input', () => {
      inspectorDirty = true;
    });
    el.addEventListener('change', () => {
      inspectorDirty = true;
    });
  });

  $('btn-save-config')?.addEventListener('click', async () => {
    try {
      try {
        localStorage.setItem('tf_typewriter', $('cfg-typewriter')?.checked === false ? '0' : '1');
      } catch {
        // Storage blocked: the typewriter simply stays on.
      }
      inspectorDirty = false;
      await callOp('switch_profile', {
        preset_name: $('cfg-preset-select').value,
        policy_mode: $('cfg-policy-select').value,
        an_depth: Number($('cfg-an-depth').value || 4),
        authors_note: $('cfg-an-text').value,
        generation_backend: $('cfg-backend-select')?.value || 'host',
        byok_base_url: $('cfg-byok-base-url')?.value ?? '',
        byok_model: $('cfg-byok-model')?.value ?? '',
        byok_cache_mode: $('cfg-byok-cache-mode')?.value || 'off',
        byok_cache_control: Boolean($('cfg-byok-cache-control')?.checked),
        byok_stream: $('cfg-byok-stream') ? Boolean($('cfg-byok-stream').checked) : true,
      });
      showToast("文风预设、Author's Note 与叙事边界已保存");
    } catch (err) {
      inspectorDirty = true;
      showToast(err.message, true);
    }
  });

  // One-Time First-Run Notice & Minor Risk Red-Screen Gate Handlers
  $('btn-ack-first-run')?.addEventListener('click', async () => {
    try {
      await callOp('ack_first_run_notice', { version: appState?.first_run_notice?.version || 2 });
      localFirstRunAcked = true;
      $('first-run-modal')?.classList.add('hidden');
      renderGateModals();
    } catch (err) {
      showToast(err.message, true);
    }
  });

  $('btn-minor-switch-card')?.addEventListener('click', () => {
    $('minor-risk-modal')?.classList.add('hidden');
    document.querySelector('[data-tab="cards"]')?.click();
  });

  $('btn-minor-ack-adult')?.addEventListener('click', async () => {
    const cardFile = appState?.minor_risk_gate?.card_file;
    if (!cardFile) return;
    try {
      await callOp('ack_minor_risk_warn', { card_file: cardFile });
      showToast('已记录成年角色核实确认');
    } catch (err) {
      showToast(err.message, true);
    }
  });
}

async function toggleDirectorShuttle() {
  const isByok = appState?.plugin_settings?.generation_backend === 'byok';
  const curDirector = Boolean(appState?.session?.director_mode);
  if (isByok && !curDirector) {
    showToast('自有 API 后端暂不支持导演穿梭，请先切回宿主模型。', true);
    return;
  }
  const nextDirector = !curDirector;
  try {
    await callOp('switch_profile', { director_mode: nextDirector });
    showToast(
      nextDirector
        ? '🎬 已进入导演穿梭模式：跨次元指令将标记为后台便签 (is_backstage)'
        : '🎭 已返回故事流：后台导演便签已自动折叠并隔离'
    );
  } catch (err) {
    showToast(err.message, true);
  }
}

async function saveInlineVariable() {
  const key = $('var-key-input')?.value.trim() || '';
  const rawVal = $('var-val-input')?.value.trim() || '';
  if (!key) return;
  const numVal = Number(rawVal);
  const value = rawVal !== '' && !Number.isNaN(numVal) ? numVal : rawVal;
  try {
    await callOp('manage_variables', { scope: 'chat', op: 'set', key, value });
    $('var-key-input').value = '';
    $('var-val-input').value = '';
    $('var-editor-row').classList.add('hidden');
  } catch (err) {
    showToast(err.message, true);
  }
}

async function submitComposerTurn() {
  const ta = $('composer-input');
  if (!ta) return;
  const text = ta.value.trim();
  if (!text) return;
  const prevFloorCount = (appState?.messages || []).length;
  const isDirector = Boolean(appState?.session?.director_mode);
  ta.value = '';
  const accepted = await dispatchAgentTurn(
    isDirector ? 'director_intervention' : 'send_turn',
    { user_text: text },
    { prevFloorCount }
  );
  if (!accepted && !ta.value) {
    ta.value = text;
  }
}

function importCardFile(file) {
  const reader = new FileReader();
  reader.onload = async () => {
    try {
      resetEditingFloor();
      const res = await callOp('import_card', {
        filename: file.name,
        base64_data: String(reader.result || ''),
      });
      showToast(`成功导入并激活角色卡：${res.character_name}`);
    } catch (err) {
      showToast(err.message, true);
    }
  };
  reader.readAsDataURL(file);
}

async function runLoreSimulation() {
  const queryText = $('lore-sim-input')?.value.trim() || '';
  const book = loadedLorebook.lorebook_name || $('lorebook-select')?.value || '';
  if (!queryText) return;
  try {
    const res = await callOp(
      'query_lore',
      {
        lorebook_names: book ? [book] : [],
        query_text: queryText,
      },
      { skipStateRefresh: true }
    );
    const box = $('lore-sim-results');
    if (!box) return;
    const act = res.active_entries || [];
    const evict = res.evicted_entries || [];
    const evictHint = evict.length > 0 ? ` · 门控/预算过滤 ${evict.length} 条` : '';
    box.innerHTML =
      `<div class="badge-ok">命中 ${act.length} 条 · 占用 ${res.used_tokens} tok${evictHint}</div>` +
      act
        .map(
          (a) =>
            `<div class="lore-item"><strong>#${escapeHtml(a.uid)} ${escapeHtml(a.comment)}</strong> <span class="muted-label">触发路径：${escapeHtml(
              a.trigger || a.reason || 'matched'
            )} · 位置：${escapeHtml(a.position)} · ${escapeHtml(a.tokens ?? 0)} tok</span></div>`
        )
        .join('');
  } catch (err) {
    showToast(err.message, true);
  }
}

// ---------------------------------------------------------------------------
// Bootstrap & Adaptive Polling Loop
// ---------------------------------------------------------------------------
async function initWorkbench() {
  bindWorkbenchEvents();
  if (window.sidecar?.getWorkspaceUris) {
    try {
      const uris = await Promise.race([
        window.sidecar.getWorkspaceUris(),
        new Promise((r) => setTimeout(() => r([]), 400)),
      ]);
      if (Array.isArray(uris) && uris.length > 0) {
        activeWorkspaceUri = uris[0];
      }
      window.sidecar.onWorkspaceChange?.((nextUris) => {
        const next = Array.isArray(nextUris) && nextUris[0] ? nextUris[0] : '';
        if (next && next !== activeWorkspaceUri) {
          activeWorkspaceUri = next;
          currentEtag = '';
          pollState(true);
        }
      });
    } catch {
      // Ignore outside host
    }
  }

  await pollState(true);

  const scheduleNext = () => {
    const delay = nextPollDelay();
    setTimeout(async () => {
      await pollState(false);
      scheduleNext();
    }, delay);
  };
  scheduleNext();
}

if (typeof globalThis !== 'undefined' && globalThis.__TAVERN_STAGE_HEADLESS_TEST__) {
  bindWorkbenchEvents();
  globalThis.__TAVERN_STAGE_TEST__ = {
    PLACEHOLDER_SVG,
    escapeHtml,
    formatRpProse,
    escapeNonAsciiJson,
    serializeAsciiJson,
    sidecarFetchJson,
    mediaUrl,
    cardAvatarUrl,
    isFirstRunAcked,
    formatNoticeItemHtml,
    pollState,
    callOp,
    renderAll,
    renderChatStream,
    renderCardsAndDoctor,
    renderInspector,
    submitComposerTurn,
    requireInlineConfirm,
    clearArmedConfirm,
    getInternalState: () => ({
      currentEtag,
      pollSeq,
      appState,
      activeConvId,
      byokBusy,
      hostDispatchInFlight,
      editingFloorState,
      inspectorDirty,
      loadedLorebook,
      loadingLorebookName,
      streamDraftText,
      typewriterActive: typewriterTimer !== null,
    }),
    setInternalState: (patch) => {
      if ('currentEtag' in patch) currentEtag = patch.currentEtag;
      if ('pollSeq' in patch) pollSeq = patch.pollSeq;
      if ('appState' in patch) appState = patch.appState;
      if ('activeWorkspaceUri' in patch) activeWorkspaceUri = patch.activeWorkspaceUri;
      if ('activeConvId' in patch) activeConvId = patch.activeConvId;
      if ('byokBusy' in patch) byokBusy = patch.byokBusy;
      if ('localFirstRunAcked' in patch) localFirstRunAcked = patch.localFirstRunAcked;
      if ('inspectorDirty' in patch) inspectorDirty = patch.inspectorDirty;
      if ('editingFloorState' in patch) editingFloorState = patch.editingFloorState;
      if ('loadedLorebook' in patch) loadedLorebook = patch.loadedLorebook;
      if ('lastLoreCardWorld' in patch) lastLoreCardWorld = patch.lastLoreCardWorld;
      if ('failedLorebookCooldown' in patch) failedLorebookCooldown = patch.failedLorebookCooldown;
      if ('lastRenderedFloorSig' in patch) lastRenderedFloorSig = patch.lastRenderedFloorSig;
      if ('lastRenderedCardsSig' in patch) lastRenderedCardsSig = patch.lastRenderedCardsSig;
    },
  };
} else {
  initWorkbench();
}
