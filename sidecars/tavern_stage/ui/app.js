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
let appState = null;
let activeWorkspaceUri = '';
let activeConvId = '';
let editingFloorIdx = null;
let loadedLorebook = { lorebook_name: '', entries: [] };
let lastRenderedFloorSig = '';
let localFirstRunAcked = false;

const $ = (id) => document.getElementById(id);

function escapeHtml(raw) {
  return String(raw ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function formatRpProse(raw) {
  let safe = escapeHtml(raw);
  safe = safe.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>');
  safe = safe.replace(/\*([^*\n]+)\*/g, '<span class="rp-action">*$1*</span>');
  safe = safe.replace(/&quot;([^&\n]+)&quot;/g, '<span class="rp-quote">"$1"</span>');
  safe = safe.replace(/“([^”\n]+)”/g, '<span class="rp-quote">“$1”</span>');
  return safe;
}

let toastTimer = null;
function showToast(msg, isError = false) {
  const bar = $('toast-bar');
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

async function sidecarFetchJson(url, options = {}) {
  const fetcher = window.sidecar?.fetch || fetch;
  const res = await fetcher(url, options);
  const data = await res.json().catch(() => ({}));
  if (!res.ok || data.ok === false) {
    throw new Error(data.error || `HTTP ${res.status}`);
  }
  return data;
}

async function callOp(op, args = {}) {
  const res = await sidecarFetchJson('/api/op', {
    method: 'POST',
    body: JSON.stringify({ op, args, workspaceUri: activeWorkspaceUri }),
  });
  currentEtag = '';
  await pollState(true);
  return res.data;
}

function mediaUrl(params) {
  const q = new URLSearchParams(params);
  if (activeWorkspaceUri) q.set('workspaceUri', activeWorkspaceUri);
  if (currentEtag) q.set('v', currentEtag);
  return `/api/media?${q.toString()}`;
}

function isFirstRunAcked() {
  return localFirstRunAcked || Boolean(appState?.plugin_settings?.first_run_notice_ack_v1);
}

function isMinorGateBlockingTurn() {
  const g = appState?.minor_risk_gate;
  if (!g) return false;
  if (g.gate_action === 'block_load') return true;
  if (g.gate_action === 'red_screen_warn' && !g.warn_acked) return true;
  return false;
}

// ---------------------------------------------------------------------------
// BYOK (own API key) Turn Dispatcher
// ---------------------------------------------------------------------------
const BYOK_ACTIONS = new Set(['send_turn', 'swipe_reroll']);
let byokInFlight = false;

async function dispatchByokTurn(actionKind, extraArgs = {}) {
  if (!BYOK_ACTIONS.has(actionKind)) {
    // Never silently fall back to the host model: that would spend host quota the user opted out of.
    showToast('自有 API Key 后端暂只支持发送与重抽；如需此功能请在配置中切回宿主模型。', true);
    return;
  }
  if (!appState?.byok?.ready) {
    showToast('自有 API Key 后端未配置完整：请设置 TAVERN_BYOK_API_KEY 并填写模型名。', true);
    return;
  }
  if (byokInFlight) {
    showToast('上一轮仍在生成中，请稍候。', false);
    return;
  }
  byokInFlight = true;
  showToast(`正在通过自有 API（${appState.byok.model}）生成…`, false);
  try {
    const res = await callOp('byok_turn', { action_kind: actionKind, ...extraArgs });
    if (res?.gentle_notice) showToast(res.gentle_notice, false);
    else showToast('');
  } finally {
    byokInFlight = false;
  }
}

// ---------------------------------------------------------------------------
// Stage Turn Dispatcher
// ---------------------------------------------------------------------------
async function dispatchAgentTurn(actionKind, extraArgs = {}) {
  try {
    if (!isFirstRunAcked()) {
      $('first-run-modal')?.classList.remove('hidden');
      showToast('请先阅读并确认首次运行须知', false);
      return;
    }
    if (isMinorGateBlockingTurn()) {
      renderGateModals();
      showToast('当前角色卡触发未成年风险闸，请先在红屏警告中处理', true);
      return;
    }
    if (appState?.plugin_settings?.generation_backend === 'byok') {
      await dispatchByokTurn(actionKind, extraArgs);
      return;
    }
    const boundConvId = activeConvId || appState?.session?.rp_conv_id || '';
    const prep = await callOp('prepare_agent_turn', {
      action_kind: actionKind,
      rp_conv_id: boundConvId,
      ...extraArgs,
    });
    if (prep.gentle_notice) {
      showToast(prep.gentle_notice, false);
    }
    if (!window.sidecar?.agent) {
      throw new Error('当前处于独立预览模式，请在侧栏舞台中发起互动。');
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
  } catch (err) {
    showToast(err.message || String(err), true);
  }
}

// ---------------------------------------------------------------------------
// State Polling & Rendering
// ---------------------------------------------------------------------------
let pollErrorStreak = 0;

async function pollState(force = false) {
  try {
    const q = new URLSearchParams();
    if (!force && currentEtag) q.set('etag', currentEtag);
    if (activeWorkspaceUri) q.set('workspaceUri', activeWorkspaceUri);
    const resp = await sidecarFetchJson(`/api/state?${q.toString()}`, { method: 'GET' });
    if (resp.unchanged) return;
    currentEtag = resp.etag || '';
    appState = resp.state;
    pollErrorStreak = 0;
    renderAll();
  } catch (err) {
    pollErrorStreak += 1;
    // Log once per outage instead of on every retry.
    if (pollErrorStreak === 1) console.warn('[Tavern Stage poll]', err);
  }
}


function nextPollDelay() {
  if (pollErrorStreak > 0) {
    // Exponential back-off with full jitter while the control plane is unreachable.
    const cap = Math.min(30000, 1000 * 2 ** Math.min(pollErrorStreak, 5));
    return cap / 2 + Math.random() * (cap / 2);
  }
  return appState?.session?.stage_busy ? 1100 : 2500;
}

function renderAll() {
  if (!appState) return;
  const sess = appState.session || {};
  if (sess.rp_conv_id) {
    activeConvId = sess.rp_conv_id;
  } else if (!sess.active) {
    activeConvId = '';
  }
  $('data-profile-badge').textContent = appState.data_root_name || 'default-user';
  $('busy-dot').classList.toggle('busy', Boolean(sess.stage_busy));
  $('busy-banner').classList.toggle('hidden', !sess.stage_busy);

  const charLabel = sess.character_name || '对方';
  const turnMode = sess.pending_turn_mode || 'normal';
  if (turnMode === 'suggestions') {
    $('busy-text').textContent = '正在为你构思贴合心境的台词灵感…';
  } else if (turnMode === 'kartoittaja') {
    $('busy-text').textContent = `${charLabel} 的多条命运支线正在展开…`;
  } else if (turnMode === 'swipe') {
    $('busy-text').textContent = `${charLabel} 正在酝酿另一种回应…`;
  } else {
    $('busy-text').textContent = `${charLabel} 正在落笔回应…`;
  }

  renderHero(sess);
  renderDimensionMixer(sess);
  renderVariables(appState.variables || {});
  renderChatStream(sess, appState.messages || []);
  renderSuggestions(appState.suggestions || {});
  renderCardsAndDoctor();
  renderInspector(sess);
  syncLorebookDropdown();
  renderGateModals();
}

function renderGateModals() {
  const firstModal = $('first-run-modal');
  const acked = isFirstRunAcked();
  if (firstModal) {
    const noticeItems = appState?.first_run_notice?.items;
    if (!acked && Array.isArray(noticeItems) && noticeItems.length > 0) {
      $('first-run-items').innerHTML = noticeItems.map((t) => `<li>${escapeHtml(t)}</li>`).join('');
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

function renderHero(sess) {
  const charSel = $('char-select');
  const cards = appState.cards || [];
  const charOptionsHtml = cards
    .map((c) => {
      const blocked = c.minor_gate_action === 'block_load';
      const prefix = blocked ? '[⛔ 拒载] ' : c.minor_gate_action === 'red_screen_warn' ? '[⚠️ 待核] ' : '';
      return `<option value="${escapeHtml(c.file)}" ${blocked ? 'disabled' : ''} ${
        c.name === sess.character_name || c.file === sess.card_file ? 'selected' : ''
      }>${prefix}${escapeHtml(c.name)} (${escapeHtml(c.spec)})</option>`;
    })
    .join('');
  if (charSel.innerHTML !== charOptionsHtml) {
    charSel.innerHTML = charOptionsHtml || '<option value="">无可用角色卡</option>';
  }

  $('mode-toggle-btn').textContent = sess.mode === 'chat' ? '💬 纯对白' : '🎭 剧场演绎';
  $('scenario-line').textContent =
    appState.active_card?.scenario ||
    appState.active_card?.personality ||
    '在下方直接发送台词，或唤起玩家导演灵感。';

  const chatSel = $('chat-select');
  const chats = appState.chats || [];
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

  const portrait = $('stage-portrait');
  const expr = appState.current_expression;
  const exprBadge = $('expr-badge');
  if (expr && sess.character_name) {
    const targetSrc = mediaUrl({ kind: 'expression', char: sess.character_name, expr });
    if (portrait.dataset.srcKey !== targetSrc) {
      portrait.dataset.srcKey = targetSrc;
      portrait.src = targetSrc;
      portrait.onerror = () => {
        portrait.onerror = null;
        portrait.src = sess.card_file
          ? mediaUrl({ kind: 'avatar', file: sess.card_file })
          : PLACEHOLDER_SVG;
      };
    }
    exprBadge.textContent = expr;
    exprBadge.classList.remove('hidden');
  } else if (sess.card_file) {
    const avatarSrc = mediaUrl({ kind: 'avatar', file: sess.card_file });
    if (portrait.dataset.srcKey !== avatarSrc) {
      portrait.dataset.srcKey = avatarSrc;
      portrait.src = avatarSrc;
      portrait.onerror = () => {
        portrait.src = PLACEHOLDER_SVG;
      };
    }
    exprBadge.classList.add('hidden');
  } else {
    portrait.src = PLACEHOLDER_SVG;
    exprBadge.classList.add('hidden');
  }
}

function renderDimensionMixer(sess) {
  const epLevel = Number(sess.epistemic_level ?? 0);
  const agenticOn = Boolean(sess.agentic_hands);
  const directorOn = Boolean(sess.director_mode);

  document.querySelectorAll('#mixer-epistemic-group .mixer-seg-btn').forEach((btn) => {
    const val = Number(btn.dataset.epistemic);
    btn.classList.toggle('active', val === epLevel);
  });

  const agBtn = $('mixer-agentic-btn');
  if (agBtn) {
    agBtn.textContent = agenticOn ? '🖐️ 权能释放' : '🔒 纯对白';
    agBtn.classList.toggle('active-agentic', agenticOn);
  }

  const dirBtn = $('mixer-director-btn');
  if (dirBtn) {
    dirBtn.textContent = directorOn ? '🎬 穿梭中 (Alt+D)' : '🎬 导演穿梭';
    dirBtn.classList.toggle('active-director', directorOn);
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
      ta.placeholder = '输入行动 (*...*) 或台词 ("...")，Enter 发送，Alt+D 切换导演穿梭…';
      sendBtn.textContent = '发送推进 ▶';
    }
  }
}

function renderVariables(vars) {
  const listEl = $('vars-list');
  const entries = Object.entries(vars);
  if (entries.length === 0) {
    listEl.innerHTML = '<span class="muted-label">暂无场景状态记录</span>';
    return;
  }
  listEl.innerHTML = entries
    .map(
      ([k, v]) => `
      <span class="var-chip">
        <strong>${escapeHtml(k)}:</strong> <span>${escapeHtml(v)}</span>
        <button data-var-inc="${escapeHtml(k)}" title="+1">+1</button>
        <button data-var-del="${escapeHtml(k)}" title="移除状态">×</button>
      </span>`
    )
    .join('');
}

function renderChatStream(sess, messages) {
  const stream = $('chat-stream');
  const sig = JSON.stringify({
    edit: editingFloorIdx,
    chat: sess.chat_file,
    ep: sess.epistemic_level,
    dir: sess.director_mode,
    msgs: messages.map((m) => [m.mes, m.swipe_id, (m.swipes || []).length, Boolean(m.extra?.is_backstage)]),
  });
  if (sig === lastRenderedFloorSig) return;
  const wasNearBottom = stream.scrollHeight - stream.scrollTop - stream.clientHeight < 90;
  lastRenderedFloorSig = sig;

  if (messages.length === 0) {
    stream.innerHTML = '<div class="muted-label">剧场帷幕已启，发送第一句台词开始演绎。</div>';
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

      if (editingFloorIdx === idx) {
        return `
          <article class="floor-card ${isUser ? 'user-floor' : ''}">
            <div class="floor-hdr">
              <span class="floor-author">#${idx} ${escapeHtml(m.name || (isUser ? '你' : sess.character_name))} · 编辑中</span>
            </div>
            <textarea id="edit-floor-ta-${idx}" class="composer-textarea" rows="4">${escapeHtml(activeText)}</textarea>
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
        return `
          <details class="floor-card backstage-floor ${isUser ? 'user-floor' : ''}">
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

  if (wasNearBottom) {
    stream.scrollTop = stream.scrollHeight;
  }
}

function renderSuggestions(sugObj) {
  const items = Array.isArray(sugObj.suggestions) ? sugObj.suggestions : [];
  const box = $('suggestions-box');
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

function renderCardsAndDoctor() {
  const doc = appState.doctor || { total_issues: 0, card_issues: [], lorebook_issues: [] };
  const badge = $('doctor-badge');
  badge.textContent = String(doc.total_issues || 0);
  badge.classList.toggle('hidden', !doc.total_issues);

  const allIssues = [
    ...(doc.card_issues || []).map((i) => ({ ...i, scope: '角色卡' })),
    ...(doc.lorebook_issues || []).map((i) => ({ ...i, scope: '世界书' })),
  ];
  const docList = $('doctor-list');
  if (allIssues.length === 0) {
    docList.innerHTML = '<div class="muted-label">✅ 当前角色卡与关联世界书未检出结构隐患。</div>';
  } else {
    docList.innerHTML = allIssues
      .map(
        (iss) => `
        <div class="doctor-item ${escapeHtml(iss.severity || 'warning')}">
          <div><strong>[${escapeHtml(iss.scope)} · ${escapeHtml(iss.code)}]</strong></div>
          <div>${escapeHtml(iss.message)}</div>
          ${iss.fix_hint ? `<div class="muted-label">💡 修复建议：${escapeHtml(iss.fix_hint)}</div>` : ''}
        </div>`
      )
      .join('');
  }

  const q = ($('card-search-input').value || '').trim().toLowerCase();
  const filtered = (appState.cards || []).filter(
    (c) =>
      !q ||
      c.name.toLowerCase().includes(q) ||
      (c.tags || []).some((t) => String(t).toLowerCase().includes(q))
  );
  $('cards-grid').innerHTML = filtered
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
        <img class="gallery-thumb" src="${mediaUrl({ kind: 'avatar', file: c.file })}" alt="${escapeHtml(c.name)}" />
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
  $('cache-ratio-label').textContent = `${ratio}% Static Prefix`;
  $('cache-meter-bar').style.width = `${Math.min(100, Math.max(0, ratio))}%`;
  $('token-stats-line').textContent = `Static: ${dump.static_tokens || 0} tok · Dynamic: ${
    dump.dynamic_tokens || 0
  } tok · Lore: ${dump.lore_used_tokens || 0}/${dump.lore_budget || 2048} tok`;
  $('dump-report-pre').textContent = dump.dump_report || '暂无透视详情';

  const presetSel = $('cfg-preset-select');
  const presets = appState.presets || ['Default'];
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

  const activeEl = document.activeElement;
  const policyMode = appState.plugin_settings?.policy_mode || 'host_gentle';
  if (activeEl !== $('cfg-policy-select')) {
    $('cfg-policy-select').value = policyMode;
  }
  const pcfg = appState.plugin_settings || {};
  if ($('cfg-backend-select') && activeEl !== $('cfg-backend-select')) {
    $('cfg-backend-select').value = pcfg.generation_backend === 'byok' ? 'byok' : 'host';
  }
  if ($('cfg-byok-base-url') && activeEl !== $('cfg-byok-base-url')) {
    $('cfg-byok-base-url').value = pcfg.byok_base_url || '';
    $('cfg-byok-base-url').placeholder = appState.byok?.base_url || '';
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
  if (activeEl !== $('cfg-an-depth')) {
    $('cfg-an-depth').value = String(sess.an_depth ?? 4);
  }
  if (activeEl !== $('cfg-an-text')) {
    $('cfg-an-text').value = sess.authors_note || '';
  }
}

// ---------------------------------------------------------------------------
// World Studio Logic
// ---------------------------------------------------------------------------
function syncLorebookDropdown() {
  const sel = $('lorebook-select');
  const books = appState.lorebooks || [];
  const targetBook = loadedLorebook.lorebook_name || appState.active_card?.world || books[0] || '';
  const html = books
    .map(
      (b) =>
        `<option value="${escapeHtml(b)}" ${b === targetBook ? 'selected' : ''}>${escapeHtml(b)}</option>`
    )
    .join('');
  if (sel.innerHTML !== html) {
    sel.innerHTML = html || '<option value="">暂无世界书</option>';
    if (targetBook && !loadedLorebook.lorebook_name) {
      loadLorebookDetail(targetBook);
    }
  }
}

async function loadLorebookDetail(bookName) {
  if (!bookName) return;
  try {
    loadedLorebook = await callOp('read_lorebook', { lorebook_name: bookName });
    renderLoreEntries();
  } catch (err) {
    showToast(err.message, true);
  }
}

function renderLoreEntries() {
  const entries = loadedLorebook.entries || [];
  $('lore-count-label').textContent = `${entries.length} 条`;
  const list = $('lore-entries-list');
  if (entries.length === 0) {
    list.innerHTML = '<div class="muted-label">当前世界书为空，点击「+ 新增条目」创建。</div>';
    return;
  }
  list.innerHTML = entries
    .map(
      (e) => `
      <div class="lore-item">
        <div class="section-hdr">
          <strong>#${e.uid} ${escapeHtml(e.comment || '(无标题)')}</strong>
          <div class="btn-row">
            <button class="mini-btn" data-lore-toggle="${e.uid}">${e.enabled ? '🟢 已启用' : '⚪ 已停用'}</button>
            <button class="mini-btn" data-lore-edit="${e.uid}">编辑</button>
          </div>
        </div>
        <div class="muted-label">Keys: ${escapeHtml((e.keys || []).join(', ') || '(constant)')} · ${escapeHtml(
        e.position
      )} ${e.position === 'at_depth' ? `@D${e.depth}` : ''} · Order ${e.order}</div>
      </div>`
    )
    .join('');
}

function openLoreEditor(entry = null) {
  $('lore-editor-card').classList.remove('hidden');
  $('lore-editor-title').textContent = entry ? `编辑条目 #${entry.uid}` : '新增世界书条目';
  $('le-uid').value = entry ? String(entry.uid) : '';
  $('le-comment').value = entry?.comment || '';
  $('le-keys').value = (entry?.keys || []).join(', ');
  $('le-keys-sec').value = (entry?.keys_secondary || []).join(', ');
  $('le-sec-logic').value = String(entry?.selective_logic ?? 0);
  $('le-position').value = entry?.position || 'before_char';
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
document.querySelectorAll('.tab-btn').forEach((btn) => {
  btn.addEventListener('click', () => {
    const target = btn.dataset.tab;
    document.querySelectorAll('.tab-btn').forEach((b) => b.classList.toggle('active', b === btn));
    document.querySelectorAll('.tab-panel').forEach((p) => {
      p.classList.toggle('active', p.id === `panel-${target}`);
    });
    if (target === 'lore' && !loadedLorebook.lorebook_name && $('lorebook-select').value) {
      loadLorebookDetail($('lorebook-select').value);
    }
  });
});

$('char-select').addEventListener('change', (e) => {
  if (e.target.value) {
    callOp('bootstrap', { character_name: e.target.value, mode: appState?.session?.mode || 'play' }).catch((err) =>
      showToast(err.message, true)
    );
  }
});

$('chat-select').addEventListener('change', (e) => {
  if (e.target.value && appState?.session?.character_name) {
    callOp('bootstrap', {
      character_name: appState.session.character_name,
      chat_file: e.target.value,
      mode: appState.session.mode || 'play',
    }).catch((err) => showToast(err.message, true));
  }
});

$('new-chat-btn').addEventListener('click', () => {
  if (!appState?.session?.character_name) return;
  callOp('bootstrap', {
    character_name: appState.session.character_name,
    new_chat: true,
    mode: appState.session.mode || 'play',
  })
    .then(() => showToast('已开启新的故事线'))
    .catch((err) => showToast(err.message, true));
});

$('mode-toggle-btn').addEventListener('click', () => {
  const nextMode = appState?.session?.mode === 'chat' ? 'play' : 'chat';
  callOp('switch_profile', { mode: nextMode }).catch((err) => showToast(err.message, true));
});

// Dimension Mixer Events (Switch A: Epistemic | Switch B: Agentic Hands | Switch C: Director Shuttle)
$('mixer-epistemic-group')?.addEventListener('click', (e) => {
  const btn = e.target.closest('[data-epistemic]');
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

async function toggleDirectorShuttle() {
  const nextDirector = !Boolean(appState?.session?.director_mode);
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

$('mixer-director-btn')?.addEventListener('click', toggleDirectorShuttle);

// Variables Events
$('add-var-btn').addEventListener('click', () => {
  $('var-editor-row').classList.toggle('hidden');
  $('var-key-input').focus();
});
$('var-cancel-btn').addEventListener('click', () => $('var-editor-row').classList.add('hidden'));
$('var-save-btn').addEventListener('click', async () => {
  const key = $('var-key-input').value.trim();
  const rawVal = $('var-val-input').value.trim();
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
});

$('vars-list').addEventListener('click', (e) => {
  const incKey = e.target.dataset?.varInc;
  const delKey = e.target.dataset?.varDel;
  if (incKey) {
    callOp('manage_variables', { scope: 'chat', op: 'inc', key: incKey, value: 1 }).catch((err) =>
      showToast(err.message, true)
    );
  } else if (delKey) {
    callOp('manage_variables', { scope: 'chat', op: 'del', key: delKey }).catch((err) =>
      showToast(err.message, true)
    );
  }
});

// Delegated Chat Stream Floor Operations
$('chat-stream').addEventListener('click', async (e) => {
  const d = e.target.dataset || {};
  try {
    if (d.floorEdit !== undefined) {
      editingFloorIdx = Number(d.floorEdit);
      lastRenderedFloorSig = '';
      renderChatStream(appState.session, appState.messages);
    } else if (d.floorCancel !== undefined) {
      editingFloorIdx = null;
      lastRenderedFloorSig = '';
      renderChatStream(appState.session, appState.messages);
    } else if (d.floorSave !== undefined) {
      const idx = Number(d.floorSave);
      const newText = $(`edit-floor-ta-${idx}`)?.value ?? '';
      editingFloorIdx = null;
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
      await callOp('floor_op', { operation: 'delete_floor', floor_index: Number(d.floorDel) });
      showToast('已撤回该幕内容');
    } else if (d.floorFork !== undefined) {
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
      await callOp('floor_op', { operation: 'prune_swipes', floor_index: Number(d.pruneFloor) });
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

// Suggestions & Composer
$('clear-suggestions-btn').addEventListener('click', () => {
  callOp('clear_suggestions').catch((err) => showToast(err.message, true));
});

$('suggestions-list').addEventListener('click', async (e) => {
  const btn = e.target.closest('[data-sug-idx]');
  if (!btn) return;
  const idx = Number(btn.dataset.sugIdx);
  const text = appState?.suggestions?.suggestions?.[idx];
  if (!text) return;
  if (e.shiftKey) {
    await callOp('clear_suggestions');
    await dispatchAgentTurn('send_turn', { user_text: text });
  } else {
    $('composer-input').value = text;
    $('composer-input').focus();
  }
});

async function submitComposerTurn() {
  const ta = $('composer-input');
  const text = ta.value.trim();
  if (!text) return;
  ta.value = '';
  const isDirector = Boolean(appState?.session?.director_mode);
  await dispatchAgentTurn(isDirector ? 'director_intervention' : 'send_turn', { user_text: text });
}

$('btn-send-turn').addEventListener('click', submitComposerTurn);
$('composer-input').addEventListener('keydown', (e) => {
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

$('btn-suggest-replies').addEventListener('click', () => dispatchAgentTurn('suggest_replies'));
$('btn-expand-intent').addEventListener('click', () => {
  const text = $('composer-input').value.trim();
  dispatchAgentTurn('expand_intent', { user_text: text });
});

// Cards Import & Doctor
const dropzone = $('card-dropzone');
const fileInput = $('card-file-input');
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

function importCardFile(file) {
  const reader = new FileReader();
  reader.onload = async () => {
    try {
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

$('card-search-input').addEventListener('input', () => renderCardsAndDoctor());
$('cards-grid').addEventListener('click', (e) => {
  const file = e.target.dataset?.bootCard;
  if (!file) return;
  callOp('bootstrap', { character_name: file, mode: appState?.session?.mode || 'play' })
    .then(() => {
      document.querySelector('[data-tab="stage"]')?.click();
    })
    .catch((err) => showToast(err.message, true));
});

$('btn-run-lint').addEventListener('click', async () => {
  try {
    const res = await callOp('lint', {});
    showToast(res.healthy ? '角色卡与世界书状态完好' : `发现 ${res.total_issues} 项可优化的设定细节`);
  } catch (err) {
    showToast(err.message, true);
  }
});

$('btn-seppa-fix').addEventListener('click', async () => {
  const charName = appState?.session?.character_name;
  if (!charName) return;
  const prompt = `/card-forge 请运行 st_lint_card_and_lore 检查角色「${charName}」及其关联世界书，自动修复检出的兼容项（如补齐 CCv2+CCv3 双规范数据、补齐 {{original}} 或修正 selective 次级词门控）并保存写回。`;
  try {
    if (activeConvId) {
      await window.sidecar.agent.sendMessage(prompt, activeConvId);
    } else {
      const started = await window.sidecar.agent.startConversation(prompt, `卡片工坊修复 · ${charName}`);
      activeConvId = started?.conversationId || started?.response?.conversationId || activeConvId;
    }
    showToast('已唤起卡片工坊自动修复兼容项');
  } catch (err) {
    showToast(err.message, true);
  }
});

// World Studio Events
$('lorebook-select').addEventListener('change', (e) => loadLorebookDetail(e.target.value));
$('btn-new-entry').addEventListener('click', () => openLoreEditor(null));
$('btn-close-lore-editor').addEventListener('click', () => $('lore-editor-card').classList.add('hidden'));

$('lore-entries-list').addEventListener('click', async (e) => {
  const toggleUid = e.target.dataset?.loreToggle;
  const editUid = e.target.dataset?.loreEdit;
  const book = $('new-lorebook-input').value.trim() || $('lorebook-select').value;
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

$('btn-save-lore').addEventListener('click', async () => {
  const book = $('new-lorebook-input').value.trim() || $('lorebook-select').value || 'CustomWorld';
  const uidRaw = $('le-uid').value.trim();
  const payload = {
    comment: $('le-comment').value.trim(),
    key: $('le-keys').value.split(',').map((s) => s.trim()).filter(Boolean),
    keysecondary: $('le-keys-sec').value.split(',').map((s) => s.trim()).filter(Boolean),
    selectiveLogic: Number($('le-sec-logic').value),
    position: $('le-position').value,
    depth: Number($('le-depth').value || 4),
    order: Number($('le-order').value || 100),
    selective: $('le-selective').checked,
    constant: $('le-constant').checked,
    disable: !$('le-enabled').checked,
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

$('btn-delete-lore').addEventListener('click', async () => {
  const book = $('lorebook-select').value;
  const uidRaw = $('le-uid').value.trim();
  if (!book || uidRaw === '') return;
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

$('btn-sim-lore').addEventListener('click', async () => {
  const queryText = $('lore-sim-input').value.trim();
  const book = $('lorebook-select').value;
  if (!queryText) return;
  try {
    const res = await callOp('query_lore', {
      lorebook_names: book ? [book] : [],
      query_text: queryText,
    });
    const box = $('lore-sim-results');
    const act = res.active_entries || [];
    box.innerHTML =
      `<div class="badge-ok">命中 ${act.length} 条 · 占用 ${res.used_tokens} tok</div>` +
      act
        .map(
          (a) =>
            `<div class="lore-item"><strong>#${a.uid} ${escapeHtml(a.comment)}</strong> <span class="muted-label">触发路径：${escapeHtml(
              a.reason
            )} · 位置：${escapeHtml(a.position)}</span></div>`
        )
        .join('');
  } catch (err) {
    showToast(err.message, true);
  }
});

// Inspector Config Save
$('btn-save-config').addEventListener('click', async () => {
  try {
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
    });
    showToast('文风预设、Author\'s Note 与叙事边界已保存');
  } catch (err) {
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

// ---------------------------------------------------------------------------
// Bootstrap & Adaptive Polling Loop
// ---------------------------------------------------------------------------
async function initWorkbench() {
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

initWorkbench();
