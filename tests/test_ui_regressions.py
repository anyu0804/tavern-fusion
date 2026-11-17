#!/usr/bin/env python3
"""Regression suite for Tavern Stage UI (`app.js`, `index.html`, `styles.css`),
Skills/Subagents contract alignment, and cross-layer Sidecar UI protocols.
"""

from __future__ import annotations

from html.parser import HTMLParser
import json
from pathlib import Path
import re
import shutil
import subprocess
import unittest

ROOT_DIR = Path(__file__).resolve().parent.parent
UI_DIR = ROOT_DIR / "sidecars" / "tavern_stage" / "ui"
APP_JS = UI_DIR / "app.js"
INDEX_HTML = UI_DIR / "index.html"
STYLES_CSS = UI_DIR / "styles.css"


class _DomAuditParser(HTMLParser):
    """Lightweight HTML structure auditor for `index.html`."""

    VOID_TAGS = frozenset(
        {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
    )
    VISUAL_CONTAINER_CLASSES = frozenset(
        {
            "hero-card",
            "portrait-wrap",
            "dimension-mixer-bar",
            "mixer-seg",
            "floor-card",
            "suggestions-box",
            "section-card",
            "dropzone",
            "gallery-card",
            "doctor-item",
            "lore-item",
            "meter-track",
            "dump-pre",
            "modal-backdrop",
            "modal-panel",
            "recovery-banner",
        }
    )

    def __init__(self) -> None:
        super().__init__()
        self.by_id: dict[str, dict[str, str]] = {}
        self.select_options: dict[str, list[str]] = {}
        self.tab_buttons: list[dict[str, str]] = []
        self._stack: list[tuple[str, dict[str, str], int]] = []
        self._active_select_id: str | None = None
        self.max_visual_container_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = {k: (v if v is not None else "") for k, v in attrs}
        el_id = attr_map.get("id", "")
        if el_id:
            self.by_id[el_id] = {"tag": tag, **attr_map}

        classes = set(attr_map.get("class", "").split())
        if "tab-btn" in classes:
            self.tab_buttons.append(attr_map)

        prev_depth = self._stack[-1][2] if self._stack else 0
        next_depth = prev_depth + (1 if (classes & self.VISUAL_CONTAINER_CLASSES) else 0)
        if next_depth > self.max_visual_container_depth:
            self.max_visual_container_depth = next_depth

        if tag == "select" and el_id:
            self._active_select_id = el_id
            self.select_options.setdefault(el_id, [])
        elif tag == "option" and self._active_select_id:
            self.select_options[self._active_select_id].append(attr_map.get("value", ""))

        if tag not in self.VOID_TAGS:
            self._stack.append((tag, attr_map, next_depth))

    def handle_endtag(self, tag: str) -> None:
        if tag == "select":
            self._active_select_id = None
        for idx in range(len(self._stack) - 1, -1, -1):
            if self._stack[idx][0] == tag:
                del self._stack[idx:]
                break


def _extract_css_blocks(css_text: str) -> list[tuple[str, str]]:
    cleaned = re.sub(r"/\*.*?\*/", "", css_text, flags=re.DOTALL)
    blocks: list[tuple[str, str]] = []
    for match in re.finditer(r"([^{}]+)\{([^{}]*)\}", cleaned):
        selector = " ".join(match.group(1).strip().split())
        body = match.group(2).strip()
        blocks.append((selector, body))
    return blocks


class TavernStageUiRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.node_bin = shutil.which("node")
        if not cls.node_bin:
            raise RuntimeError("Node.js binary ('node') is required to run Tavern Stage UI regression tests.")

    def test_01_app_js_syntax_check(self) -> None:
        proc = subprocess.run(
            [self.node_bin, "--check", str(APP_JS)],
            cwd=str(ROOT_DIR),
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, msg=f"node --check failed:\n{proc.stderr}")

    def test_02_index_html_positions_controls_and_nesting_depth(self) -> None:
        parser = _DomAuditParser()
        parser.feed(INDEX_HTML.read_text(encoding="utf-8"))

        expected_positions = [
            "before_char",
            "after_char",
            "before_examples",
            "after_examples",
            "an_top",
            "an_bottom",
            "at_depth",
            "outlet",
        ]
        self.assertEqual(parser.select_options.get("le-position"), expected_positions)

        for required_id in (
            "btn-cancel-turn",
            "recovery-banner",
            "btn-retry-uncommitted",
            "lore-issues-list",
            "first-run-modal",
            "minor-risk-modal",
        ):
            self.assertIn(required_id, parser.by_id, f"Missing #{required_id} in index.html")

        portrait_src = parser.by_id["stage-portrait"].get("src", "")
        self.assertTrue(
            portrait_src.startswith("data:image/svg+xml;utf8,"),
            f"#stage-portrait must have an initial inline SVG src, got: {portrait_src!r}",
        )

        self.assertEqual(len(parser.tab_buttons), 4)
        for btn in parser.tab_buttons:
            self.assertIn(btn.get("aria-selected"), ("true", "false"))
            self.assertTrue(btn.get("aria-controls", "").startswith("panel-"))

        self.assertLessEqual(
            parser.max_visual_container_depth,
            3,
            f"Visual container nesting exceeded <=3 layers: got {parser.max_visual_container_depth}",
        )

    def test_03_styles_css_modal_scroll_and_list_rules(self) -> None:
        css_text = STYLES_CSS.read_text(encoding="utf-8")
        blocks = _extract_css_blocks(css_text)

        modal_bodies = [body for sel, body in blocks if sel == ".modal-panel"]
        self.assertEqual(len(modal_bodies), 1)
        self.assertRegex(modal_bodies[0], r"max-height\s*:\s*calc\(100vh\s*-\s*28px\)")
        self.assertRegex(modal_bodies[0], r"overflow-y\s*:\s*auto")

        list_bodies = [body for sel, body in blocks if ".doctor-list" in sel and ".lore-list" in sel]
        self.assertTrue(list_bodies, "Missing .doctor-list / .lore-list layout rule in styles.css")
        self.assertRegex(list_bodies[0], r"display\s*:\s*flex")
        self.assertRegex(list_bodies[0], r"flex-direction\s*:\s*column")
        self.assertRegex(list_bodies[0], r"gap\s*:\s*[1-9]\d*px")

        recovery_bodies = [body for sel, body in blocks if sel == ".recovery-banner"]
        self.assertEqual(len(recovery_bodies), 1)
        self.assertRegex(css_text, r"@media\s*\(\s*max-width\s*:\s*420px\s*\)")

    def test_04_skills_agents_and_acceptance_doc_contracts(self) -> None:
        card_forge = (ROOT_DIR / "skills" / "card-forge" / "SKILL.md").read_text(encoding="utf-8")
        self.assertNotIn("st_read_card(card_filename=..., include_avatar_base64=true)", card_forge)
        self.assertIn("include_avatar_base64=false", card_forge)

        rp_skill = (ROOT_DIR / "skills" / "rp" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("agentic_hands", rp_skill)
        self.assertIn("[STAGE_DIRECTOR_INTERVENTION]", rp_skill)

        lore_skill = (ROOT_DIR / "skills" / "lore-weaver" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn('"delete"', lore_skill)

        kartoittaja = (ROOT_DIR / "agents" / "kartoittaja-brancher.md").read_text(encoding="utf-8")
        self.assertIn("char_mes=", kartoittaja)

        acceptance_path = ROOT_DIR / "ACCEPTANCE.md"
        self.assertTrue(acceptance_path.is_file(), "ACCEPTANCE.md must exist at repository root")
        acceptance = acceptance_path.read_text(encoding="utf-8")
        self.assertIn("install_local.py --global-plugin", acceptance)
        self.assertIn("~/.gemini/antigravity/sidecar_data/tavern-fusion/tavern_stage/logs/sidecar.log", acceptance)
        self.assertNotRegex(acceptance, r"(?i)[a-z]:\\users\\|/users/[^/\s]+|/home/[^/\s]+")

    def test_05_headless_app_js_runtime_and_race_regressions(self) -> None:
        harness_js = r"""
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';

const appJsPath = process.env.APP_JS_PATH;
const code = readFileSync(appJsPath, 'utf-8');

class ClassListStub {
  constructor(owner, initial = []) {
    this.owner = owner;
    this.set = new Set(initial.filter(Boolean));
  }
  add(...names) { names.forEach((n) => this.set.add(n)); }
  remove(...names) { names.forEach((n) => this.set.delete(n)); }
  toggle(name, force) {
    const on = force === undefined ? !this.set.has(name) : Boolean(force);
    if (on) this.set.add(name); else this.set.delete(name);
    return on;
  }
  contains(name) { return this.set.has(name); }
}

class ElementStub {
  constructor(tag, id = '', classes = []) {
    this.tagName = tag.toUpperCase();
    this.id = id;
    this.classList = new ClassListStub(this, classes);
    this.dataset = {};
    this.style = {};
    this.attributes = {};
    this.listeners = new Map();
    this.children = [];
    this.parentNode = null;
    this.isConnected = true;
    this.value = '';
    this.checked = false;
    this.disabled = false;
    this.title = '';
    this.placeholder = '';
    this.src = '';
    this.open = false;
    this.scrollTop = 0;
    this.scrollHeight = 200;
    this.clientHeight = 180;
    this.selectionStart = 0;
    this.selectionEnd = 0;
    this.options = [];
    this._innerHTML = '';
    this.textContent = '';
  }
  set innerHTML(html) {
    this._innerHTML = String(html ?? '');
    this.textContent = this._innerHTML.replace(/<[^>]+>/g, '');
    if (this.tagName === 'SELECT') {
      const opts = [];
      const re = /<option\s+([^>]*)>([\s\S]*?)<\/option>/gi;
      let m;
      let selectedVal = null;
      while ((m = re.exec(this._innerHTML)) !== null) {
        const attrs = m[1];
        const valM = /value="([^"]*)"/.exec(attrs);
        const val = valM ? valM[1] : '';
        opts.push({ value: val, textContent: m[2] });
        if (/\bselected\b/.test(attrs)) selectedVal = val;
      }
      this.options = opts;
      if (selectedVal !== null) this.value = selectedVal;
      else if (opts.length > 0 && !opts.some((o) => o.value === this.value)) this.value = opts[0].value;
    }
    const editMatch = /<textarea[^>]*id="(edit-floor-ta-(\d+))"[^>]*>([\s\S]*?)<\/textarea>/.exec(this._innerHTML);
    if (this.id === 'chat-stream') {
      for (const [k] of elements) {
        if (k.startsWith('edit-floor-ta-')) elements.delete(k);
      }
      if (editMatch) {
        const ta = new ElementStub('textarea', editMatch[1]);
        ta.dataset.editFloorTa = editMatch[2];
        ta.value = editMatch[3]
          .replace(/&quot;/g, '"')
          .replace(/&#39;/g, "'")
          .replace(/&lt;/g, '<')
          .replace(/&gt;/g, '>')
          .replace(/&amp;/g, '&');
        elements.set(ta.id, ta);
      }
    }
  }
  get innerHTML() { return this._innerHTML; }
  setAttribute(k, v) { this.attributes[k] = String(v); }
  getAttribute(k) { return this.attributes[k] ?? null; }
  addEventListener(type, fn) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(fn);
  }
  async dispatch(type, eventInit = {}) {
    const handlers = this.listeners.get(type) || [];
    const ev = {
      type,
      target: this,
      preventDefault() { this.defaultPrevented = true; },
      closest: (sel) => (sel === 'button' && this.tagName === 'BUTTON' ? this : null),
      ...eventInit,
    };
    for (const h of handlers) {
      await h(ev);
    }
    return ev;
  }
  focus() { documentStub.activeElement = this; }
  setSelectionRange(s, e) { this.selectionStart = s; this.selectionEnd = e; }
  appendChild(child) {
    child.parentNode = this;
    if (!this.children.includes(child)) this.children.push(child);
    if (child.id) elements.set(child.id, child);
    if (this.tagName === 'SELECT') this.options.push(child);
    return child;
  }
  remove() {
    this.isConnected = false;
    if (this.id) elements.delete(this.id);
  }
  querySelectorAll() { return []; }
  get lastElementChild() { return this.children[this.children.length - 1] || null; }
}

const elements = new Map();
function reg(tag, id, classes = []) {
  const el = new ElementStub(tag, id, classes);
  elements.set(id, el);
  return el;
}

[
  ['span', 'data-profile-badge'],
  ['span', 'busy-dot'],
  ['div', 'toast-bar', ['hidden']],
  ['main', 'panel-stage', ['tab-panel', 'active']],
  ['main', 'panel-cards', ['tab-panel']],
  ['main', 'panel-lore', ['tab-panel']],
  ['main', 'panel-inspector', ['tab-panel']],
  ['div', 'recovery-banner', ['hidden']],
  ['span', 'recovery-text'],
  ['button', 'btn-retry-uncommitted'],
  ['img', 'stage-portrait'],
  ['span', 'expr-badge', ['hidden']],
  ['select', 'char-select'],
  ['button', 'mode-toggle-btn'],
  ['select', 'chat-select'],
  ['button', 'new-chat-btn'],
  ['p', 'scenario-line'],
  ['div', 'mixer-epistemic-group'],
  ['button', 'mixer-agentic-btn'],
  ['button', 'mixer-director-btn'],
  ['div', 'vars-list'],
  ['button', 'add-var-btn'],
  ['div', 'var-editor-row', ['hidden']],
  ['input', 'var-key-input'],
  ['input', 'var-val-input'],
  ['button', 'var-save-btn'],
  ['button', 'var-cancel-btn'],
  ['section', 'chat-stream'],
  ['div', 'busy-banner', ['hidden']],
  ['span', 'busy-text'],
  ['section', 'suggestions-box', ['hidden']],
  ['span', 'suggestions-title'],
  ['button', 'clear-suggestions-btn'],
  ['div', 'suggestions-list'],
  ['textarea', 'composer-input'],
  ['button', 'btn-suggest-replies'],
  ['button', 'btn-expand-intent'],
  ['button', 'btn-cancel-turn', ['hidden']],
  ['button', 'btn-send-turn'],
  ['span', 'doctor-badge', ['hidden']],
  ['div', 'doctor-list'],
  ['button', 'btn-run-lint'],
  ['button', 'btn-seppa-fix'],
  ['input', 'card-search-input'],
  ['div', 'cards-grid'],
  ['select', 'lorebook-select'],
  ['input', 'new-lorebook-input'],
  ['button', 'btn-new-entry'],
  ['button', 'btn-sim-lore'],
  ['input', 'lore-sim-input'],
  ['div', 'lore-sim-results'],
  ['section', 'lore-editor-card', ['hidden']],
  ['span', 'lore-editor-title'],
  ['button', 'btn-close-lore-editor'],
  ['input', 'le-uid'],
  ['input', 'le-comment'],
  ['input', 'le-keys'],
  ['input', 'le-keys-sec'],
  ['select', 'le-sec-logic'],
  ['select', 'le-position'],
  ['input', 'le-depth'],
  ['input', 'le-order'],
  ['input', 'le-selective'],
  ['input', 'le-constant'],
  ['input', 'le-enabled'],
  ['textarea', 'le-content'],
  ['button', 'btn-delete-lore'],
  ['button', 'btn-save-lore'],
  ['span', 'lore-count-label'],
  ['div', 'lore-issues-list', ['hidden']],
  ['div', 'lore-entries-list'],
  ['span', 'cache-ratio-label'],
  ['div', 'cache-meter-bar'],
  ['div', 'token-stats-line'],
  ['button', 'btn-save-config'],
  ['select', 'cfg-preset-select'],
  ['select', 'cfg-policy-select'],
  ['input', 'cfg-an-depth'],
  ['select', 'cfg-backend-select'],
  ['input', 'cfg-byok-base-url'],
  ['input', 'cfg-byok-model'],
  ['select', 'cfg-byok-cache-mode'],
  ['input', 'cfg-byok-cache-control'],
  ['input', 'cfg-byok-stream'],
  ['input', 'cfg-typewriter'],
  ['div', 'cfg-byok-status'],
  ['textarea', 'cfg-an-text'],
  ['pre', 'dump-report-pre'],
  ['div', 'first-run-modal', ['hidden']],
  ['ul', 'first-run-items'],
  ['button', 'btn-ack-first-run'],
  ['div', 'minor-risk-modal', ['hidden']],
  ['h2', 'minor-risk-title'],
  ['p', 'minor-risk-lead'],
  ['ul', 'minor-risk-reasons'],
  ['button', 'btn-minor-ack-adult'],
  ['button', 'btn-minor-switch-card'],
].forEach(([t, id, cls]) => reg(t, id, cls));

const composerFooter = new ElementStub('footer', 'composer-footer', ['composer']);
const tabStageBtn = new ElementStub('button', 'tab-stage', ['tab-btn', 'active']);
tabStageBtn.dataset.tab = 'stage';
const tabCardsBtn = new ElementStub('button', 'tab-cards', ['tab-btn']);
tabCardsBtn.dataset.tab = 'cards';

const documentStub = {
  activeElement: null,
  getElementById: (id) => elements.get(id) || null,
  createElement: (tag) => new ElementStub(tag),
  querySelector: (sel) => {
    if (sel === '.composer') return composerFooter;
    if (sel === '[data-tab="stage"]') return tabStageBtn;
    if (sel === '[data-tab="cards"]') return tabCardsBtn;
    return null;
  },
  querySelectorAll: (sel) => {
    if (sel === '.tab-btn') return [tabStageBtn, tabCardsBtn];
    return [];
  },
};

const fetchCalls = [];
let customFetchHandler = async () => ({ ok: true, status: 200, json: async () => ({ ok: true, data: {} }) });
const agentStartCalls = [];
const agentSendCalls = [];

const sandbox = {
  console,
  URLSearchParams,
  setTimeout: (fn, ms, ...rest) => {
    const t = setTimeout(fn, ms, ...rest);
    t.unref?.();
    return t;
  },
  clearTimeout,
  setInterval: (fn, ms, ...rest) => {
    const t = setInterval(fn, ms, ...rest);
    t.unref?.();
    return t;
  },
  clearInterval,
  localStorage: {
    _m: new Map(),
    getItem(k) { return this._m.has(k) ? this._m.get(k) : null; },
    setItem(k, v) { this._m.set(k, String(v)); },
  },
  document: documentStub,
  window: {
    sidecar: {
      standalone: false,
      fetch: async (url, options = {}) => {
        fetchCalls.push({ url, options });
        return await customFetchHandler(url, options);
      },
      agent: {
        startConversation: async (prompt, title) => {
          agentStartCalls.push({ prompt, title });
          return { conversationId: 'conv-created-999' };
        },
        sendMessage: async (prompt, convId) => {
          agentSendCalls.push({ prompt, convId });
          return {};
        },
      },
    },
  },
  __TAVERN_STAGE_HEADLESS_TEST__: true,
};
sandbox.globalThis = sandbox;
sandbox.fetch = sandbox.window.sidecar.fetch;

vm.createContext(sandbox);
vm.runInContext(code, sandbox, { filename: appJsPath });

const T = sandbox.__TAVERN_STAGE_TEST__;
assert.ok(T, 'Expected __TAVERN_STAGE_TEST__ hooks to be exported');

// 1. escapeHtml & formatRpProse (& dialogue isolation + single/double quotes)
assert.equal(T.escapeHtml(`a&b<c>d"e'f`), 'a&amp;b&lt;c&gt;d&quot;e&#39;f');
const proseHtml = T.formatRpProse('"Tom & Jerry" 她轻笑一声, "a < b"');
assert.match(proseHtml, /<span class="rp-quote">"Tom &amp; Jerry"<\/span>/);
assert.match(proseHtml, /<span class="rp-quote">"a &lt; b"<\/span>/);
assert.ok(!proseHtml.includes('<span class="rp-quote">" 她轻笑一声, "</span>'), 'Narration between two dialogues containing & must NOT be wrapped as rp-quote');

// 2. Contract D: Non-ASCII JSON -> pure 7-bit ASCII \uXXXX round-trip lossless
const complexObj = {
  op: 'floor_op',
  args: { new_text: '星界圣域 · 塞拉菲娜🎭\n"台词" & \'单引号\' \\ 反斜杠 — 𠀋' },
};
const asciiJson = T.serializeAsciiJson(complexObj);
for (let i = 0; i < asciiJson.length; i += 1) {
  const codeUnit = asciiJson.charCodeAt(i);
  assert.ok(codeUnit >= 0x20 && codeUnit <= 0x7e, `Non-ASCII byte leaked at index ${i}: ${codeUnit}`);
}
assert.deepEqual(JSON.parse(asciiJson), complexObj);

// 3. Contract C: mediaUrl always carries v=, .json cards map to PLACEHOLDER_SVG, expression sends char + card
T.setInternalState({ currentEtag: '', activeWorkspaceUri: '' });
assert.equal(T.mediaUrl({ kind: 'avatar', file: 'Seraphina.png' }), '/api/media?kind=avatar&file=Seraphina.png&v=0');
T.setInternalState({ currentEtag: 'ab12cd34ef567890' });
assert.equal(
  T.mediaUrl({ kind: 'avatar', file: 'Seraphina.png' }),
  '/api/media?kind=avatar&file=Seraphina.png&v=ab12cd34ef567890'
);
assert.equal(T.cardAvatarUrl('PlainCard.json'), T.PLACEHOLDER_SVG);

const baseState = {
  data_root_name: 'default-user',
  plugin_settings: {
    first_run_notice_ack_v1: true,
    first_run_notice_ack_version: 2,
    policy_mode: 'host_gentle',
    generation_backend: 'host',
  },
  first_run_notice: { version: 2, items: ['条款与风险摘要：测试内容'] },
  minor_risk_gate: { gate_action: 'pass', reasons: [] },
  byok: { ready: false, key_present: false, model: '' },
  presets: ['Default'],
  lorebooks: ['Eldoria', 'CyberCity'],
  cards: [
    { file: 'seraphina_v1.png', name: 'Seraphina', spec: 'ccv3', expressions_count: 2, world: 'Eldoria', minor_gate_action: 'pass' },
    { file: 'seraphina_v2.png', name: 'Seraphina', spec: 'ccv3', expressions_count: 0, world: 'CyberCity', minor_gate_action: 'pass' },
    { file: 'text_only.json', name: 'JsonHero', spec: 'json', expressions_count: 0, world: '', minor_gate_action: 'pass' },
  ],
  session: {
    active: true,
    rp_conv_id: 'rp-conv-123',
    character_name: '塞拉菲娜',
    card_file: 'seraphina_v1.png',
    chat_file: 'chat_a.jsonl',
    mode: 'play',
    epistemic_level: 0,
    agentic_hands: false,
    director_mode: false,
    preset_name: 'Default',
    stage_busy: false,
    pending_turn_mode: 'normal',
    authors_note: '初始注释',
    an_depth: 4,
    last_persistence_error: null,
  },
  active_card: { name: '塞拉菲娜', scenario: '星界林间', world: 'Eldoria' },
  current_expression: 'joy',
  chats: [{ file: 'chat_a.jsonl' }, { file: 'chat_b.jsonl' }],
  variables: { affection: 15 },
  messages: [
    { name: '塞拉菲娜', is_user: false, mes: '*微微颔首* "欢迎。"', swipes: ['*微微颔首* "欢迎。"'], swipe_id: 0 },
    { name: '你', is_user: true, mes: '*走近一步*', swipes: ['*走近一步*'], swipe_id: 0 },
    { name: '塞拉菲娜', is_user: false, mes: '"小心台阶。"', swipes: ['"小心台阶。"', '"跟紧我。"'], swipe_id: 0 },
  ],
  dump: { prefix_cache_ratio_pct: 75, static_tokens: 300, dynamic_tokens: 100, lore_used_tokens: 60, lore_budget: 2048, quarantined_backstage_count: 2, dump_report: 'OK' },
  doctor: { total_issues: 0, card_issues: [], lorebook_issues: [] },
  suggestions: {},
};

// Avoid auto read_lorebook network op during pure render checks by pre-seeding loadedLorebook
T.setInternalState({
  currentEtag: 'etag001',
  appState: structuredClone(baseState),
  loadedLorebook: { lorebook_name: 'Eldoria', entries: [], issues: [] },
});
T.renderAll();

const portraitSrc = elements.get('stage-portrait').src;
assert.match(portraitSrc, /kind=expression/);
assert.match(portraitSrc, /card=seraphina_v1\.png/);
assert.match(portraitSrc, /v=etag001/);
// Duplicate character name must select exact card_file seraphina_v1.png, NOT seraphina_v2.png!
assert.equal(elements.get('char-select').value, 'seraphina_v1.png');

// 4. First-run notice version check (v1 ack must re-prompt on v2)
const v1OnlyState = structuredClone(baseState);
delete v1OnlyState.plugin_settings.first_run_notice_ack_version;
T.setInternalState({ appState: v1OnlyState, localFirstRunAcked: false });
assert.equal(T.isFirstRunAcked(), false);
assert.equal(T.formatNoticeItemHtml('条款与风险摘要：请仔细阅读'), '<strong>条款与风险摘要</strong>：请仔细阅读');
T.setInternalState({ appState: structuredClone(baseState) });
assert.equal(T.isFirstRunAcked(), true);

// 5. Contract A + [P1-UI-5]: Out-of-order stale pollSeq discard & piggybacked snapshot in callOp
let resolveSlowPoll;
const slowPollPromise = new Promise((r) => { resolveSlowPoll = r; });
const freshState = structuredClone(baseState);
freshState.variables = { affection: 99 };
const staleState = structuredClone(baseState);
staleState.variables = { affection: 1 };

fetchCalls.length = 0;
customFetchHandler = async (url, opts) => {
  if (url.startsWith('/api/state')) {
    await slowPollPromise;
    return { ok: true, status: 200, json: async () => ({ unchanged: false, etag: 'etag_stale', state: staleState }) };
  }
  if (url === '/api/op') {
    return {
      ok: true,
      status: 200,
      json: async () => ({ ok: true, data: { scope: 'chat' }, etag: 'etag_fresh', state: freshState }),
    };
  }
  throw new Error('Unexpected url: ' + url);
};

const bgPoll = T.pollState(false);
await T.callOp('manage_variables', { scope: 'chat', op: 'set', key: 'affection', value: 99 });
// Contract A: callOp used piggybacked state without issuing a second /api/state request!
assert.equal(fetchCalls.filter((c) => c.url.startsWith('/api/state')).length, 1);
assert.equal(T.getInternalState().currentEtag, 'etag_fresh');
assert.equal(T.getInternalState().appState.variables.affection, 99);

// Now let the slow stale poll resolve -> must be discarded!
resolveSlowPoll();
await bgPoll;
assert.equal(T.getInternalState().currentEtag, 'etag_fresh');
assert.equal(T.getInternalState().appState.variables.affection, 99);

// 6. [P1-UI-1] Composer draft preserved when turn rejected (e.g. BYOK unconfigured)
const byokUnready = structuredClone(baseState);
byokUnready.plugin_settings.generation_backend = 'byok';
byokUnready.byok = { ready: false, key_present: false, model: '' };
T.setInternalState({ appState: byokUnready });
elements.get('composer-input').value = '这是一段珍贵的长台词草稿，绝不能丢！';
await T.submitComposerTurn();
assert.equal(elements.get('composer-input').value, '这是一段珍贵的长台词草稿，绝不能丢！');

// 7. [P1-UI-2] In-place floor edit draft preserved across poll & index shifted on earlier floor delete + 2-click confirm
T.setInternalState({ appState: structuredClone(baseState), lastRenderedFloorSig: '' });
T.renderAll();
T.setInternalState({
  editingFloorState: {
    chatFile: 'chat_a.jsonl',
    idx: 2,
    draft: '玩家正在精修第2幕台词…',
    selStart: 5,
    selEnd: 5,
    focused: true,
  },
  lastRenderedFloorSig: '',
});
const polledWithNewFloor = structuredClone(baseState);
polledWithNewFloor.messages.push({ name: '你', is_user: true, mes: '新增第3幕', swipes: ['新增第3幕'], swipe_id: 0 });
T.setInternalState({ appState: polledWithNewFloor });
T.renderChatStream(polledWithNewFloor.session, polledWithNewFloor.messages);
assert.equal(elements.get('edit-floor-ta-2').value, '玩家正在精修第2幕台词…');

// Simulate 2-click inline delete on floor #0 while editing floor #2
const delBtn = new ElementStub('button', 'tmp-del-0');
delBtn.textContent = '🗑️';
delBtn.dataset.floorDel = '0';
const afterDeleteState = structuredClone(polledWithNewFloor);
afterDeleteState.messages.splice(0, 1);
const opCalls = [];
customFetchHandler = async (url, opts) => {
  const parsedBody = JSON.parse(opts.body);
  opCalls.push(parsedBody);
  return { ok: true, status: 200, json: async () => ({ ok: true, data: {}, etag: 'etag_del', state: afterDeleteState }) };
};
const chatStream = elements.get('chat-stream');
// First click only arms confirmation
await chatStream.dispatch('click', { target: delBtn });
assert.equal(opCalls.length, 0);
assert.equal(delBtn.textContent, '确认撤回？');
// Second click executes delete_floor and shifts editingFloorState.idx 2 -> 1!
await chatStream.dispatch('click', { target: delBtn });
assert.equal(opCalls.length, 1);
assert.equal(opCalls[0].op, 'floor_op');
assert.equal(opCalls[0].args.operation, 'delete_floor');
assert.equal(T.getInternalState().editingFloorState.idx, 1);
assert.equal(elements.get('edit-floor-ta-1').value, '玩家正在精修第2幕台词…');

// 8. [P1-UI-3] Inspector dirty guard protects unsaved form inputs during background poll
elements.get('cfg-byok-model').value = 'gemini-2.5-pro';
await elements.get('cfg-byok-model').dispatch('input');
assert.equal(T.getInternalState().inspectorDirty, true);
T.renderInspector(afterDeleteState.session);
assert.equal(elements.get('cfg-byok-model').value, 'gemini-2.5-pro');

// 9. [P0-UI-2], [P1-UI-4], [P2-UI-1]: World Studio save sends enabled=false, syncs new book, and renders a.trigger
elements.get('new-lorebook-input').value = 'StarRealm';
elements.get('le-uid').value = '';
elements.get('le-comment').value = '星门律令';
elements.get('le-keys').value = '星门';
elements.get('le-keys-sec').value = '';
elements.get('le-sec-logic').value = '0';
elements.get('le-position').value = 'an_top';
elements.get('le-depth').value = '4';
elements.get('le-order').value = '90';
elements.get('le-selective').checked = false;
elements.get('le-constant').checked = false;
elements.get('le-enabled').checked = false;
elements.get('le-content').value = '星门仅在朔月开启。';

opCalls.length = 0;
const stateWithNewBook = structuredClone(baseState);
stateWithNewBook.lorebooks = ['Eldoria', 'CyberCity', 'StarRealm'];
customFetchHandler = async (url, opts) => {
  const parsedBody = JSON.parse(opts.body);
  opCalls.push(parsedBody);
  if (parsedBody.op === 'read_lorebook') {
    return {
      ok: true,
      status: 200,
      json: async () => ({
        ok: true,
        data: { lorebook_name: 'StarRealm', entries: [{ uid: 100, comment: '星门律令', keys: ['星门'], enabled: false, position: 'an_top', depth: 4, order: 90 }], issues: [] },
        etag: 'etag_lore',
        state: stateWithNewBook,
      }),
    };
  }
  if (parsedBody.op === 'query_lore') {
    return {
      ok: true,
      status: 200,
      json: async () => ({
        ok: true,
        data: {
          used_tokens: 18,
          active_entries: [{ uid: 100, comment: '星门律令', trigger: 'keyword(星门)', position: 'an_top', tokens: 18 }],
          evicted_entries: [],
        },
        etag: 'etag_lore',
        state: stateWithNewBook,
      }),
    };
  }
  return { ok: true, status: 200, json: async () => ({ ok: true, data: { uid: 100 }, etag: 'etag_lore', state: stateWithNewBook }) };
};

await elements.get('btn-save-lore').dispatch('click');
const upsertCall = opCalls.find((c) => c.op === 'evolve_lore');
assert.equal(upsertCall.args.lorebook_name, 'StarRealm');
assert.equal(upsertCall.args.entry_payload.enabled, false);
assert.equal(upsertCall.args.entry_payload.position, 'an_top');
assert.equal(elements.get('lorebook-select').value, 'StarRealm');

elements.get('lore-sim-input').value = '星门正在震动';
await elements.get('btn-sim-lore').dispatch('click');
assert.match(elements.get('lore-sim-results').innerHTML, /keyword\(星门\)/);

// 10. [P1-UI-6] Card Doctor Seppä fix opens isolated conversation without polluting activeConvId
agentStartCalls.length = 0;
agentSendCalls.length = 0;
T.setInternalState({ activeConvId: 'rp-conv-123' });
await elements.get('btn-seppa-fix').dispatch('click');
assert.equal(agentSendCalls.length, 0);
assert.equal(agentStartCalls.length, 1);
assert.equal(T.getInternalState().activeConvId, 'rp-conv-123');

// 11. Contract B: Recovery banner retry_uncommitted_turn & stuck stage_busy cancel_turn(force_reset=true)
const errState = structuredClone(baseState);
errState.session.stage_busy = true;
errState.session.last_persistence_error = {
  error: 'lock busy',
  uncommitted_turn: { character_name: 'Seraphina', char_mes: '"未落盘正文"' },
};
T.setInternalState({ appState: errState, byokBusy: false });
T.renderAll();
assert.equal(elements.get('recovery-banner').classList.contains('hidden'), false);
assert.equal(elements.get('btn-cancel-turn').classList.contains('hidden'), false);

opCalls.length = 0;
const recoveredState = structuredClone(baseState);
customFetchHandler = async (url, opts) => {
  const parsedBody = JSON.parse(opts.body);
  opCalls.push(parsedBody);
  return { ok: true, status: 200, json: async () => ({ ok: true, data: {}, etag: 'etag_rec', state: recoveredState }) };
};
await elements.get('btn-retry-uncommitted').dispatch('click');
assert.equal(opCalls[0].op, 'retry_uncommitted_turn');
assert.equal(elements.get('recovery-banner').classList.contains('hidden'), true);

T.setInternalState({ appState: errState, byokBusy: false });
T.renderAll();
opCalls.length = 0;
await elements.get('btn-cancel-turn').dispatch('click');
assert.equal(opCalls[0].op, 'cancel_turn');
assert.deepEqual(opCalls[0].args, { force_reset: true });

// Active BYOK turn cancel sends empty args {} (graceful cancel_turn without force_reset)
T.setInternalState({ appState: errState, byokBusy: true });
T.renderAll();
opCalls.length = 0;
await elements.get('btn-cancel-turn').dispatch('click');
assert.equal(opCalls[0].op, 'cancel_turn');
assert.deepEqual(opCalls[0].args, {});

// 12. Deferred gallery rendering while hidden + .json card avatar uses PLACEHOLDER_SVG when opened
T.setInternalState({ currentEtag: 'etag_gal_9', appState: structuredClone(baseState), lastRenderedCardsSig: '' });
elements.get('cards-grid').innerHTML = '';
T.renderCardsAndDoctor(false);
assert.equal(elements.get('cards-grid').innerHTML, '', 'Hidden cards tab must defer rebuilding gallery DOM');
await tabCardsBtn.dispatch('click');
const gridHtml = elements.get('cards-grid').innerHTML;
assert.match(gridHtml, /file=seraphina_v1\.png&amp;v=etag_gal_9/);
assert.ok(!gridHtml.includes('file=text_only.json'), '.json card must not hit /api/media?file=text_only.json');
assert.ok(gridHtml.includes('data:image/svg+xml;utf8,'), '.json card must render inline PLACEHOLDER_SVG');

process.stdout.write(JSON.stringify({ ok: true, assertions: 'all_passed' }) + '\n');
""";
        import os
        proc = subprocess.run(
            [self.node_bin, "--input-type=module", "-e", harness_js],
            cwd=str(ROOT_DIR),
            env={**os.environ, "APP_JS_PATH": str(APP_JS)},
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=20,
            check=False,
        )
        self.assertEqual(
            proc.returncode,
            0,
            msg=f"Headless app.js regression script failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}",
        )
        out = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertTrue(out.get("ok"))
        self.assertEqual(out.get("assertions"), "all_passed")


if __name__ == "__main__":
    unittest.main()
