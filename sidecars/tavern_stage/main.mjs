/**
 * Tavern Stage Sidecar Backend (`sidecars/tavern_stage/main.mjs`).
 *
 * Bridges Antigravity's native `sidecar_sdk` right-pane Webview (`Aux Pane`)
 * with the zero-dependency Python 3.12 MCP & SillyTavern data spine:
 * 1. Fast Disk ETag Polling (`GET /api/state?etag=...`) -> <1ms idle checks.
 * 2. Strict Raster Magic-Byte Media Sandbox (`GET /api/media`) -> blocks SVG/HTML/XSS
 *    and path traversal outside `characters/`.
 * 3. Non-blocking stdin JSON bridge (`POST /api/op`) -> handles large PNG card
 *    imports, floor revisions, Swipes, World Studio edits, and built-in model turn prep.
 * 4. Includes a clean standalone fallback server (`TAVERN_STAGE_STANDALONE=1`) for
 *    automated integration tests and local browser preview.
 */
import { spawn } from 'node:child_process';
import { createHash } from 'node:crypto';
import {
  existsSync,
  mkdirSync,
  readFileSync,
  readdirSync,
  realpathSync,
  statSync,
  writeFileSync,
  renameSync,
} from 'node:fs';
import { createServer } from 'node:http';
import { createRequire } from 'node:module';
import { homedir } from 'node:os';
import { basename, delimiter, dirname, isAbsolute, join, relative, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { resolvePythonCommand } from '../../scripts/py_resolver.mjs';

const __dirname = dirname(fileURLToPath(import.meta.url));
const PLUGIN_ROOT = resolve(__dirname, '..', '..');
const UI_DIR = join(__dirname, 'ui');
const MCP_SERVER_PY = join(PLUGIN_ROOT, 'server', 'mcp_server.py');

const DEFAULT_STATE_DIR = join(homedir(), '.gemini', 'antigravity', 'tavern-fusion', 'state');
const RAW_STATE_DIR = process.env.PLUGIN_STATE_DIR || '';
const STATE_DIR =
  !RAW_STATE_DIR || RAW_STATE_DIR === '${PLUGIN_DATA}'
    ? DEFAULT_STATE_DIR
    : resolve(RAW_STATE_DIR);
mkdirSync(STATE_DIR, { recursive: true });

let cachedPythonSpec = null;
function getPythonSpec() {
  if (!cachedPythonSpec) {
    cachedPythonSpec = resolvePythonCommand();
  }
  return cachedPythonSpec;
}

const ALLOWED_OPS = new Set([
  'snapshot',
  'bootstrap',
  'bind_rp_conv',
  'floor_op',
  'prepare_agent_turn',
  'byok_turn',
  'byok_status',
  'read_lorebook',
  'evolve_lore',
  'query_lore',
  'lint',
  'switch_profile',
  'manage_variables',
  'import_card',
  'clear_suggestions',
  'ack_first_run_notice',
  'ack_minor_risk_warn',
]);

// ---------------------------------------------------------------------------
// SDK Loader + Standalone Test/Preview Fallback
// ---------------------------------------------------------------------------
class FallbackResponse {
  constructor(body, { contentType = 'text/plain', status = 200, headers = {} } = {}) {
    this.body = body;
    this.contentType = contentType;
    this.status = status;
    this.headers = headers;
  }
}

const STANDALONE_PRELOAD_SHIM = `
(function () {
  if (window.sidecar) return;
  window.sidecar = {
    conversationId: '',
    standalone: true,
    fetch: (url, options = {}) => {
      const headers = new Headers(options.headers || {});
      headers.set('Content-Type', 'application/json');
      return fetch(url, { ...options, headers });
    },
    getWorkspaceUris: async () => [],
    onWorkspaceChange: () => () => {},
    agent: {
      sendMessage: async () => {
        throw new Error('Standalone preview mode: open Tavern Stage inside Antigravity Aux Pane to drive built-in models.');
      },
      startConversation: async () => {
        throw new Error('Standalone preview mode: open Tavern Stage inside Antigravity Aux Pane to drive built-in models.');
      },
      getConversationMetadata: async () => ({}),
    },
    ui: {
      toggleAuxPane: () => {},
      toggleConversation: () => {},
      addArtifactComment: () => {},
    },
  };
})();
`;

const LOOPBACK_HOSTNAMES = new Set(['127.0.0.1', 'localhost', '[::1]', '::1']);

function isLoopbackHostHeader(hostHeader) {
  if (!hostHeader) return false;
  let hostname;
  try {
    hostname = new URL(`http://${hostHeader}`).hostname.toLowerCase();
  } catch {
    return false;
  }
  return LOOPBACK_HOSTNAMES.has(hostname);
}

class StandaloneSidecarApp {
  #pages = new Map();
  #apis = new Map();
  #port;

  constructor(port = 0) {
    this.#port = Number(process.env.ANTIGRAVITY_SIDECAR_WEB_PORT || process.env.PORT || port || 0);
  }

  page(path, handler) {
    this.#pages.set(path, handler);
  }

  api(path, handler, method = 'POST') {
    if (!this.#apis.has(path)) this.#apis.set(path, new Map());
    this.#apis.get(path).set(method.toUpperCase(), handler);
  }

  run() {
    this.#pages.set('/preload.js', () => new FallbackResponse(STANDALONE_PRELOAD_SHIM, {
      contentType: 'application/javascript; charset=utf-8',
    }));

    const server = createServer(async (req, res) => {
      // DNS-rebinding guard: a rebound attacker page reaches us with its own domain in
      // `Host` (and a matching `Origin`), so the same-origin check alone is not enough.
      // Only loopback host names are ever accepted, for pages, media and the op bridge.
      if (!isLoopbackHostHeader(req.headers.host)) {
        res.writeHead(421, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ error: 'Misdirected request: non-loopback Host header rejected' }));
        return;
      }
      const url = new URL(req.url || '/', 'http://127.0.0.1');
      if (req.method === 'GET' && this.#pages.has(url.pathname)) {
        const out = await this.#pages.get(url.pathname)();
        if (out && typeof out === 'object' && out.contentType && out.body !== undefined) {
          res.writeHead(out.status || 200, { 'Content-Type': out.contentType, ...(out.headers || {}) });
          res.end(out.body);
          return;
        }
        res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
        res.end(out);
        return;
      }

      if (this.#apis.has(url.pathname)) {
        const mMap = this.#apis.get(url.pathname);
        if (!mMap.has(req.method)) {
          res.writeHead(405, { 'Content-Type': 'application/json' });
          res.end(JSON.stringify({ error: 'Method Not Allowed' }));
          return;
        }
        // Lightweight CSRF guard for the standalone server: browsers attach `Origin` on
        // cross-origin POSTs even without a preflight, so a drive-by web page must never
        // reach the mutating op bridge. (SDK mode relies on the host's UI token instead.)
        const originHeader = req.headers.origin;
        if (originHeader) {
          let sameHost = false;
          try {
            const selfHost = new URL(`http://${req.headers.host || '127.0.0.1'}`).host;
            sameHost = new URL(originHeader).host === selfHost;
          } catch {
            sameHost = false;
          }
          if (!sameHost) {
            res.writeHead(403, { 'Content-Type': 'application/json' });
            res.end(JSON.stringify({ error: 'Cross-origin request rejected' }));
            return;
          }
        }
        const handler = mMap.get(req.method);
        const chunks = [];
        req.on('data', (c) => chunks.push(c));
        req.on('end', async () => {
          try {
            const data = {};
            for (const [k, v] of url.searchParams.entries()) {
              if (k.toLowerCase() !== 'token') data[k] = v;
            }
            const rawBody = Buffer.concat(chunks).toString('utf-8');
            if (rawBody) Object.assign(data, JSON.parse(rawBody));

            const result = await handler(data);
            if (result && typeof result === 'object' && result.contentType && result.body !== undefined) {
              const buf = typeof result.body === 'string' ? result.body : Buffer.from(result.body);
              res.writeHead(result.status || 200, { 'Content-Type': result.contentType, ...(result.headers || {}) });
              res.end(buf);
              return;
            }
            res.writeHead(200, { 'Content-Type': 'application/json; charset=utf-8' });
            res.end(JSON.stringify(result));
          } catch (err) {
            res.writeHead(500, { 'Content-Type': 'application/json; charset=utf-8' });
            res.end(JSON.stringify({ error: err.message || String(err) }));
          }
        });
        return;
      }

      res.writeHead(404, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ error: 'Not found' }));
    });

    return new Promise((resolvePromise) => {
      server.listen(this.#port, '127.0.0.1', () => {
        const addr = server.address();
        process.stdout.write(`[tavern_stage] listening on 127.0.0.1:${addr.port}\n`);
        resolvePromise(server);
      });
    });
  }
}

async function importSidecarSdkModule() {
  try {
    return await import('sidecar_sdk');
  } catch {
    // Node.js ESM `import()` ignores `NODE_PATH` when workspace is on another drive (e.g. D:\).
  }

  const candidateFiles = [];
  if (process.env.NODE_PATH) {
    for (const part of process.env.NODE_PATH.split(delimiter).filter(Boolean)) {
      candidateFiles.push(join(part.trim(), 'sidecar_sdk', 'index.mjs'));
    }
  }

  try {
    const req = createRequire(import.meta.url);
    const resolved = req.resolve('sidecar_sdk');
    if (resolved) candidateFiles.push(resolved);
  } catch {
    // Ignore if createRequire cannot locate without explicit path
  }

  candidateFiles.push(
    join(homedir(), '.gemini', 'antigravity', 'lib', 'node_modules', 'sidecar_sdk', 'index.mjs')
  );

  for (const file of candidateFiles) {
    if (file && existsSync(file)) {
      return await import(pathToFileURL(file).href);
    }
  }

  throw new Error('Unable to locate Antigravity sidecar_sdk module.');
}

async function createSidecarHost() {
  if (process.env.TAVERN_STAGE_STANDALONE === '1') {
    return { app: new StandaloneSidecarApp(), ResponseClass: FallbackResponse };
  }
  try {
    const sdk = await importSidecarSdkModule();
    return { app: new sdk.SidecarApp(), ResponseClass: sdk.Response };
  } catch (err) {
    if (process.env.ANTIGRAVITY_SIDECAR_WEB_PORT && process.env.ANTIGRAVITY_LS_ADDRESS) {
      throw err;
    }
    return { app: new StandaloneSidecarApp(), ResponseClass: FallbackResponse };
  }
}

// ---------------------------------------------------------------------------
// SillyTavern Data Root Discovery & Sync
// ---------------------------------------------------------------------------
function rememberDataRoot(resolvedRoot) {
  try {
    const settingsPath = join(STATE_DIR, 'plugin_settings.json');
    let cur = {};
    if (existsSync(settingsPath)) {
      cur = JSON.parse(readFileSync(settingsPath, 'utf-8'));
    }
    if (cur.last_st_data_root === resolvedRoot) return;
    cur.last_st_data_root = resolvedRoot;
    const tmp = `${settingsPath}.tmp.${process.pid}`;
    writeFileSync(tmp, JSON.stringify(cur, null, 2), 'utf-8');
    renameSync(tmp, settingsPath);
  } catch {
    // Non-fatal if concurrent hook is updating settings
  }
}

function resolveStDataRoot(workspaceUri = '') {
  if (process.env.ST_DATA_ROOT && !process.env.ST_DATA_ROOT.startsWith('${')) {
    const envPath = resolve(process.env.ST_DATA_ROOT);
    if (existsSync(envPath)) return envPath;
  }

  if (workspaceUri) {
    try {
      const wsPath = workspaceUri.startsWith('file://') ? fileURLToPath(workspaceUri) : workspaceUri;
      for (const rel of ['data/default-user', 'SillyTavern/data/default-user', '../SillyTavern/data/default-user']) {
        const cand = resolve(wsPath, rel);
        if (existsSync(cand) && statSync(cand).isDirectory()) {
          rememberDataRoot(cand);
          return cand;
        }
      }
    } catch {
      // Ignore malformed URI
    }
  }

  try {
    const settingsPath = join(STATE_DIR, 'plugin_settings.json');
    if (existsSync(settingsPath)) {
      const s = JSON.parse(readFileSync(settingsPath, 'utf-8'));
      if (s.last_st_data_root && existsSync(s.last_st_data_root)) {
        return resolve(s.last_st_data_root);
      }
    }
  } catch {
    // Ignore parse error
  }

  const localCandidates = [
    resolve(PLUGIN_ROOT, '..', 'SillyTavern', 'data', 'default-user'),
    resolve(PLUGIN_ROOT, '..', 'data', 'default-user'),
    resolve(PLUGIN_ROOT, 'examples', 'mock_st_data', 'default-user'),
  ];
  for (const c of localCandidates) {
    if (existsSync(c) && statSync(c).isDirectory()) {
      return c;
    }
  }
  const fallback = resolve(PLUGIN_ROOT, 'examples', 'mock_st_data', 'default-user');
  mkdirSync(fallback, { recursive: true });
  return fallback;
}

// ---------------------------------------------------------------------------
// Sub-Millisecond Disk ETag Fingerprint
// ---------------------------------------------------------------------------
function statStamp(p) {
  try {
    const st = statSync(p);
    return `${st.size}:${Math.trunc(st.mtimeMs)}`;
  } catch {
    return '0';
  }
}

function dirStamp(dirPath) {
  try {
    if (!existsSync(dirPath)) return 'missing';
    const names = readdirSync(dirPath);
    let maxMtime = 0;
    let totalSize = 0;
    for (const n of names) {
      try {
        const st = statSync(join(dirPath, n));
        if (st.mtimeMs > maxMtime) maxMtime = st.mtimeMs;
        totalSize += st.size;
      } catch {
        // Ignore transient temp files
      }
    }
    return `${names.length}:${totalSize}:${Math.trunc(maxMtime)}`;
  } catch {
    return 'err';
  }
}

function computeDiskEtag(dataRoot) {
  const h = createHash('sha1');
  const sessFile = join(STATE_DIR, 'active_session.json');
  h.update(`root=${dataRoot}|`);
  h.update(`sess=${statStamp(sessFile)}|`);
  h.update(`cfg=${statStamp(join(STATE_DIR, 'plugin_settings.json'))}|`);
  h.update(`sug=${statStamp(join(STATE_DIR, 'stage_suggestions.json'))}|`);

  if (existsSync(sessFile)) {
    try {
      const sess = JSON.parse(readFileSync(sessFile, 'utf-8'));
      if (sess.chat_path) {
        h.update(`chat=${statStamp(sess.chat_path)}|`);
      }
      const chatKey = sess.avatar_stem || sess.character_name;
      if (chatKey) {
        const safeStem = String(chatKey).replace(/[<>:"/\\|?*\x00-\x1f]/g, '_').trim();
        h.update(`cdir=${dirStamp(join(dataRoot, 'chats', safeStem))}|`);
      }
    } catch {
      h.update('sess_parse_err|');
    }
  }

  h.update(`chars=${dirStamp(join(dataRoot, 'characters'))}|`);
  h.update(`worlds=${dirStamp(join(dataRoot, 'worlds'))}|`);
  return h.digest('hex').slice(0, 16);
}

// ---------------------------------------------------------------------------
// Strict Magic-Byte Media Sandbox
// ---------------------------------------------------------------------------
function sniffRasterMime(buf) {
  if (!Buffer.isBuffer(buf) || buf.length < 12) return null;
  // PNG: 89 50 4E 47 0D 0A 1A 0A
  if (
    buf[0] === 0x89 &&
    buf[1] === 0x50 &&
    buf[2] === 0x4e &&
    buf[3] === 0x47 &&
    buf[4] === 0x0d &&
    buf[5] === 0x0a &&
    buf[6] === 0x1a &&
    buf[7] === 0x0a
  ) {
    return 'image/png';
  }
  // JPEG: FF D8 FF
  if (buf[0] === 0xff && buf[1] === 0xd8 && buf[2] === 0xff) {
    return 'image/jpeg';
  }
  // GIF87a / GIF89a
  const hdr6 = buf.subarray(0, 6).toString('ascii');
  if (hdr6 === 'GIF87a' || hdr6 === 'GIF89a') {
    return 'image/gif';
  }
  // WEBP: RIFF....WEBP
  if (
    buf.subarray(0, 4).toString('ascii') === 'RIFF' &&
    buf.subarray(8, 12).toString('ascii') === 'WEBP'
  ) {
    return 'image/webp';
  }
  return null;
}

function isContainedInside(childReal, parentReal) {
  const normChild = process.platform === 'win32' ? childReal.toLowerCase() : childReal;
  const normParent = process.platform === 'win32' ? parentReal.toLowerCase() : parentReal;
  const rel = relative(normParent, normChild);
  return Boolean(rel) && !rel.startsWith('..') && !isAbsolute(rel);
}

function serveMediaAsset(data, ResponseClass) {
  const dataRoot = resolveStDataRoot(data.workspaceUri || '');
  const charRoot = join(dataRoot, 'characters');
  if (!existsSync(charRoot)) {
    return new ResponseClass(JSON.stringify({ error: 'characters directory not found' }), {
      contentType: 'application/json',
      status: 404,
    });
  }
  const realCharRoot = realpathSync(charRoot);
  const kind = String(data.kind || 'avatar');
  let candidatePath = null;

  if (kind === 'avatar') {
    const rawFile = String(data.file || '').trim();
    if (!rawFile || rawFile !== basename(rawFile) || rawFile.includes('..')) {
      return new ResponseClass(JSON.stringify({ error: 'Invalid avatar filename' }), {
        contentType: 'application/json',
        status: 400,
      });
    }
    const p = join(realCharRoot, rawFile);
    if (existsSync(p) && statSync(p).isFile()) {
      candidatePath = p;
    }
  } else if (kind === 'expression') {
    const rawChar = String(data.char || '').trim();
    const rawExpr = String(data.expr || '').trim();
    if (!rawChar || !rawExpr || rawChar.includes('..') || rawExpr.includes('..') || rawExpr !== basename(rawExpr)) {
      return new ResponseClass(JSON.stringify({ error: 'Invalid expression parameters' }), {
        contentType: 'application/json',
        status: 400,
      });
    }
    let exprDir = null;
    const directDir = join(realCharRoot, basename(rawChar));
    if (existsSync(directDir) && statSync(directDir).isDirectory()) {
      exprDir = directDir;
    } else {
      const targetLower = basename(rawChar).toLowerCase();
      for (const entry of readdirSync(realCharRoot)) {
        const sub = join(realCharRoot, entry);
        if (entry.toLowerCase() === targetLower && statSync(sub).isDirectory()) {
          exprDir = sub;
          break;
        }
      }
    }
    if (exprDir) {
      for (const ext of ['.png', '.webp', '.jpg', '.jpeg', '.gif']) {
        const p = join(exprDir, `${rawExpr}${ext}`);
        if (existsSync(p) && statSync(p).isFile()) {
          candidatePath = p;
          break;
        }
      }
    }
  } else {
    return new ResponseClass(JSON.stringify({ error: 'Unsupported media kind' }), {
      contentType: 'application/json',
      status: 400,
    });
  }

  if (!candidatePath) {
    return new ResponseClass(JSON.stringify({ error: 'Media asset not found' }), {
      contentType: 'application/json',
      status: 404,
    });
  }

  const realTarget = realpathSync(candidatePath);
  if (!isContainedInside(realTarget, realCharRoot)) {
    return new ResponseClass(JSON.stringify({ error: 'Path containment violation blocked' }), {
      contentType: 'application/json',
      status: 403,
    });
  }

  const rawBytes = readFileSync(realTarget);
  const mime = sniffRasterMime(rawBytes);
  if (!mime) {
    return new ResponseClass(
      JSON.stringify({ error: 'Unsupported or unsafe media payload (raster magic-byte verification failed)' }),
      {
        contentType: 'application/json',
        status: 415,
        headers: { 'X-Content-Type-Options': 'nosniff' },
      }
    );
  }

  return new ResponseClass(rawBytes, {
    contentType: mime,
    status: 200,
    headers: {
      'X-Content-Type-Options': 'nosniff',
      'Content-Security-Policy': "default-src 'none'; sandbox",
      'Cache-Control': 'no-cache',
    },
  });
}

// ---------------------------------------------------------------------------
// Python Control-Plane RPC Bridge
// ---------------------------------------------------------------------------
// BYOK turns wait on a remote model; every other op is a fast local file operation.
const SLOW_OP_TIMEOUT_MS = { byok_turn: 180000 };
const DEFAULT_OP_TIMEOUT_MS = 20000;

function invokePythonOp(op, argsObj, dataRoot) {
  const timeoutMs = SLOW_OP_TIMEOUT_MS[op] || DEFAULT_OP_TIMEOUT_MS;
  return new Promise((resolvePromise, rejectPromise) => {
    let pySpec;
    try {
      pySpec = getPythonSpec();
    } catch (err) {
      rejectPromise(err);
      return;
    }
    const cliArgs = [
      ...pySpec.prefixArgs,
      MCP_SERVER_PY,
      '--data-root',
      dataRoot,
      '--state-dir',
      STATE_DIR,
      '--sidecar-op',
      op,
      '--args',
      '-',
    ];

    const child = spawn(pySpec.command, cliArgs, {
      cwd: PLUGIN_ROOT,
      env: {
        ...process.env,
        PYTHONIOENCODING: 'utf-8',
        PYTHONUTF8: '1',
        PLUGIN_STATE_DIR: STATE_DIR,
      },
      windowsHide: true,
      stdio: ['pipe', 'pipe', 'pipe'],
    });

    const outChunks = [];
    const errChunks = [];
    const timer = setTimeout(() => {
      child.kill();
      rejectPromise(new Error(`Python sidecar-op '${op}' timed out after ${Math.round(timeoutMs / 1000)}s`));
    }, timeoutMs);

    child.stdout.on('data', (chunk) => outChunks.push(chunk));
    child.stderr.on('data', (chunk) => errChunks.push(chunk));
    child.on('error', (err) => {
      clearTimeout(timer);
      rejectPromise(err);
    });
    child.on('close', () => {
      clearTimeout(timer);
      const rawOut = Buffer.concat(outChunks).toString('utf-8').trim();
      if (!rawOut) {
        const stderrText = Buffer.concat(errChunks).toString('utf-8').trim();
        rejectPromise(new Error(stderrText || `Empty response from Python sidecar-op '${op}'`));
        return;
      }
      try {
        const lastLine = rawOut.split(/\r?\n/).pop();
        const parsed = JSON.parse(lastLine);
        if (!parsed.ok) {
          rejectPromise(new Error(parsed.error || `Sidecar operation '${op}' failed`));
          return;
        }
        resolvePromise(parsed.data);
      } catch (e) {
        rejectPromise(new Error(`Invalid JSON from Python sidecar-op '${op}': ${e.message}`));
      }
    });

    child.stdin.write(JSON.stringify(argsObj || {}), 'utf-8');
    child.stdin.end();
  });
}

// ---------------------------------------------------------------------------
// Server Registration & Startup
// ---------------------------------------------------------------------------
let cachedEtag = '';
let cachedDataRoot = '';
let cachedSnapshot = null;

const { app, ResponseClass } = await createSidecarHost();

app.page('/', () =>
  new ResponseClass(readFileSync(join(UI_DIR, 'index.html'), 'utf-8'), {
    contentType: 'text/html; charset=utf-8',
    headers: { 'Cache-Control': 'no-cache' },
  })
);
app.page('/styles.css', () =>
  new ResponseClass(readFileSync(join(UI_DIR, 'styles.css'), 'utf-8'), {
    contentType: 'text/css; charset=utf-8',
    headers: { 'Cache-Control': 'no-cache' },
  })
);
app.page('/app.js', () =>
  new ResponseClass(readFileSync(join(UI_DIR, 'app.js'), 'utf-8'), {
    contentType: 'application/javascript; charset=utf-8',
    headers: { 'Cache-Control': 'no-cache' },
  })
);

app.api(
  '/api/state',
  async (data) => {
    const dataRoot = resolveStDataRoot(data.workspaceUri || '');
    const currentEtag = computeDiskEtag(dataRoot);
    if (
      data.etag &&
      data.etag === currentEtag &&
      cachedSnapshot &&
      cachedDataRoot === dataRoot
    ) {
      return { unchanged: true, etag: currentEtag };
    }
    const snapshot = await invokePythonOp('snapshot', {}, dataRoot);
    const freshEtag = computeDiskEtag(dataRoot);
    cachedEtag = freshEtag;
    cachedDataRoot = dataRoot;
    cachedSnapshot = snapshot;
    return { unchanged: false, etag: freshEtag, state: snapshot };
  },
  'GET'
);

// Live BYOK streaming preview: the Python op writes this file while tokens arrive. Read it
// directly (no Python spawn) so the UI can poll it every ~150 ms during a turn.
const STREAM_PREVIEW_PATH = join(STATE_DIR, 'byok_stream_preview.json');
const STREAM_PREVIEW_MAX_AGE_S = 600;
app.api(
  '/api/stream_preview',
  async () => {
    try {
      if (!existsSync(STREAM_PREVIEW_PATH)) return { active: false };
      const preview = JSON.parse(readFileSync(STREAM_PREVIEW_PATH, 'utf-8'));
      const age = Date.now() / 1000 - Number(preview.started_at || 0);
      if (!preview.active || age > STREAM_PREVIEW_MAX_AGE_S) return { active: false };
      return {
        active: true,
        action_kind: String(preview.action_kind || ''),
        text: String(preview.text || ''),
      };
    } catch {
      // Mid-rename or partially written: report "no change" rather than an error.
      return { active: true, text: null };
    }
  },
  'GET'
);

app.api(
  '/api/media',
  async (data) => serveMediaAsset(data, ResponseClass),
  'GET'
);

app.api(
  '/api/op',
  async (data) => {
    const op = String(data.op || '').trim();
    if (!ALLOWED_OPS.has(op)) {
      return new ResponseClass(
        JSON.stringify({ ok: false, error: `Operation '${op}' is not permitted` }),
        { contentType: 'application/json; charset=utf-8', status: 400 }
      );
    }
    const dataRoot = resolveStDataRoot(data.workspaceUri || '');
    try {
      const result = await invokePythonOp(op, data.args || {}, dataRoot);
      cachedSnapshot = null;
      cachedEtag = '';
      return {
        ok: true,
        data: result,
        etag: computeDiskEtag(dataRoot),
      };
    } catch (err) {
      return new ResponseClass(
        JSON.stringify({ ok: false, error: err.message || String(err) }),
        { contentType: 'application/json; charset=utf-8', status: 400 }
      );
    }
  },
  'POST'
);

await app.run();
