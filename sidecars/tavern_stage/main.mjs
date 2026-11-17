/**
 * Tavern Stage Sidecar Backend (`sidecars/tavern_stage/main.mjs`).
 *
 * Bridges Antigravity's native `sidecar_sdk` right-pane Webview (`Aux Pane`)
 * with the zero-dependency Python 3.12 MCP & SillyTavern data spine:
 * 1. Fast Async Disk ETag Polling (`GET /api/state?etag=...`) with single-flight
 *    deduplication and `mutationEpoch` cache-poisoning protection.
 * 2. Strict Raster Magic-Byte Media Sandbox (`GET /api/media`) -> blocks SVG/HTML/XSS
 *    and path traversal outside `characters/`, resolving `card` stem before `char`.
 * 3. Non-blocking stdin JSON bridge (`POST /api/op`) -> handles large PNG card
 *    imports (EPIPE-safe), inline `_include_snapshot` piggybacking, single-flight turn
 *    dispatch lock, and Windows process-tree cleanup on timeout / `cancel_turn`.
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
} from 'node:fs';
import { readFile, readdir, stat, unlink } from 'node:fs/promises';
import { createServer } from 'node:http';
import { createRequire } from 'node:module';
import { homedir } from 'node:os';
import { basename, delimiter, dirname, isAbsolute, join, relative, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { resolvePythonCommand } from '../../scripts/py_resolver.mjs';

const __dirname = dirname(fileURLToPath(import.meta.url));
const PLUGIN_ROOT = resolve(__dirname, '..', '..');
const UI_DIR = join(__dirname, 'ui');
const DEFAULT_MCP_SERVER_PY = join(PLUGIN_ROOT, 'server', 'mcp_server.py');

const HOME_DIR = homedir();
const DEFAULT_STATE_DIR = join(HOME_DIR, '.gemini', 'antigravity', 'tavern-fusion', 'state');
const RAW_STATE_DIR = process.env.PLUGIN_STATE_DIR || '';
export const STATE_DIR =
  !RAW_STATE_DIR || RAW_STATE_DIR === '${PLUGIN_DATA}'
    ? DEFAULT_STATE_DIR
    : resolve(RAW_STATE_DIR);
mkdirSync(STATE_DIR, { recursive: true });

const MAX_REQUEST_BODY_BYTES = 32 * 1024 * 1024;
const MAX_CHILD_OUTPUT_BYTES = 64 * 1024 * 1024;
const WIN_RESERVED_DEVICE_RE = /^(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?$/i;

let cachedPythonSpec = null;
function getPythonSpec() {
  if (!cachedPythonSpec) {
    cachedPythonSpec = resolvePythonCommand();
  }
  return cachedPythonSpec;
}

function getMcpScriptPath() {
  const override = (process.env.TAVERN_MCP_SCRIPT || '').trim();
  return override ? resolve(override) : DEFAULT_MCP_SERVER_PY;
}

export const ALLOWED_OPS = new Set([
  'snapshot',
  'bootstrap',
  'bind_rp_conv',
  'floor_op',
  'prepare_agent_turn',
  'byok_turn',
  'byok_status',
  'cancel_turn',
  'retry_uncommitted_turn',
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

const TURN_DISPATCH_OPS = new Set(['byok_turn', 'prepare_agent_turn']);

// ---------------------------------------------------------------------------
// Path Hygiene, Stem Alignment & Anti-Doxxing Error Sanitization
// ---------------------------------------------------------------------------
function escapeRegExp(str) {
  return str.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

export function sanitizeErrorText(raw, customHome = HOME_DIR) {
  let msg = String(raw ?? '');
  const home = String(customHome || '').trim().replace(/[\\/]+$/, '');
  if (!home) return msg;
  const slashNorm = home.replace(/\\/g, '/');
  const backNorm = home.replace(/\//g, '\\');
  const flags = process.platform === 'win32' ? 'gi' : 'g';
  for (const variant of new Set([home, backNorm, slashNorm])) {
    if (variant) {
      msg = msg.replace(new RegExp(escapeRegExp(variant), flags), '~');
    }
  }
  return msg;
}

export function sanitizeFilenameStem(rawName) {
  let cleaned = String(rawName ?? '')
    .trim()
    .replace(/[<>:"/\\|?*\x00-\x1f]/g, '_')
    .replace(/[. ]+$/g, '')
    .trim();
  if (!cleaned || cleaned === '.' || cleaned === '..' || cleaned.includes('..')) {
    return '';
  }
  if (WIN_RESERVED_DEVICE_RE.test(cleaned)) {
    cleaned = `_${cleaned}`;
  }
  return cleaned;
}

export function normalizeAvatarStem(rawKey) {
  let cleaned = String(rawKey ?? '').trim();
  if (/\.(?:png|json)$/i.test(cleaned)) {
    cleaned = cleaned.replace(/\.(?:png|json)$/i, '');
  }
  return sanitizeFilenameStem(cleaned);
}

export function normalizeWinPath(rawPath, platform = process.platform) {
  let s = String(rawPath ?? '').trim();
  if (!s) return '';
  if (platform === 'win32') {
    const msys = /^\/([a-zA-Z])(\/.*|$)/.exec(s);
    if (msys) {
      s = `${msys[1].toUpperCase()}:${msys[2] || '/'}`;
    }
  }
  const resolved = resolve(s);
  if (platform === 'win32') {
    return resolved.replace(/^([a-z]):/, (_, drive) => `${drive.toUpperCase()}:`);
  }
  return resolved;
}

function isExistingDir(p) {
  try {
    return Boolean(p) && existsSync(p) && statSync(p).isDirectory();
  } catch {
    return false;
  }
}

function isSafeSingleSegment(seg) {
  const s = String(seg ?? '').trim();
  if (!s || s === '.' || s === '..' || s.includes('..')) return false;
  if (s.includes('/') || s.includes('\\') || s.includes(':')) return false;
  if (/[\x00-\x1f]/.test(s)) return false;
  if (s !== basename(s)) return false;
  return true;
}

// ---------------------------------------------------------------------------
// SDK Loader + Standalone Test/Preview Fallback
// ---------------------------------------------------------------------------
export class FallbackResponse {
  constructor(body, { contentType = 'text/plain', status = 200, headers = {} } = {}) {
    this.body = body;
    this.contentType = contentType;
    this.status = status;
    this.headers = headers;
  }
}

function jsonResponse(ResponseClass, payload, status = 200, cacheControl = 'no-cache, no-store') {
  return new ResponseClass(JSON.stringify(payload), {
    contentType: 'application/json; charset=utf-8',
    status,
    headers: {
      'Cache-Control': cacheControl,
      'X-Content-Type-Options': 'nosniff',
    },
  });
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

export class StandaloneSidecarApp {
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
    this.#pages.set(
      '/preload.js',
      () =>
        new FallbackResponse(STANDALONE_PRELOAD_SHIM, {
          contentType: 'application/javascript; charset=utf-8',
          headers: { 'Cache-Control': 'no-cache' },
        })
    );

    const server = createServer(async (req, res) => {
      // DNS-rebinding guard: only loopback host names are accepted.
      if (!isLoopbackHostHeader(req.headers.host)) {
        res.writeHead(421, {
          'Content-Type': 'application/json; charset=utf-8',
          'Cache-Control': 'no-store',
        });
        res.end(JSON.stringify({ error: 'Misdirected request: non-loopback Host header rejected' }));
        return;
      }
      const url = new URL(req.url || '/', 'http://127.0.0.1');
      if (req.method === 'GET' && this.#pages.has(url.pathname)) {
        try {
          const out = await this.#pages.get(url.pathname)();
          if (out && typeof out === 'object' && out.contentType && out.body !== undefined) {
            res.writeHead(out.status || 200, {
              'Cache-Control': 'no-cache',
              'Content-Type': out.contentType,
              ...(out.headers || {}),
            });
            res.end(out.body);
            return;
          }
          res.writeHead(200, {
            'Content-Type': 'text/html; charset=utf-8',
            'Cache-Control': 'no-cache',
          });
          res.end(out);
        } catch (err) {
          res.writeHead(500, {
            'Content-Type': 'application/json; charset=utf-8',
            'Cache-Control': 'no-store',
          });
          res.end(JSON.stringify({ error: sanitizeErrorText(err.message || String(err)) }));
        }
        return;
      }

      if (this.#apis.has(url.pathname)) {
        const mMap = this.#apis.get(url.pathname);
        if (!mMap.has(req.method)) {
          res.writeHead(405, {
            'Content-Type': 'application/json; charset=utf-8',
            'Cache-Control': 'no-store',
          });
          res.end(JSON.stringify({ error: 'Method Not Allowed' }));
          return;
        }
        // Lightweight CSRF guard for the standalone server.
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
            res.writeHead(403, {
              'Content-Type': 'application/json; charset=utf-8',
              'Cache-Control': 'no-store',
            });
            res.end(JSON.stringify({ error: 'Cross-origin request rejected' }));
            return;
          }
        }
        const handler = mMap.get(req.method);
        const chunks = [];
        let totalBytes = 0;
        let aborted = false;

        req.on('error', () => {
          aborted = true;
        });
        req.on('data', (c) => {
          if (aborted) return;
          totalBytes += c.length;
          if (totalBytes > MAX_REQUEST_BODY_BYTES) {
            aborted = true;
            res.writeHead(413, {
              'Content-Type': 'application/json; charset=utf-8',
              'Cache-Control': 'no-store',
            });
            res.end(JSON.stringify({ error: 'Request payload exceeds 32MB limit' }));
            req.destroy();
            return;
          }
          chunks.push(c);
        });
        req.on('end', async () => {
          if (aborted) return;
          try {
            const data = {};
            for (const [k, v] of url.searchParams.entries()) {
              if (k.toLowerCase() !== 'token') data[k] = v;
            }
            const rawBody = Buffer.concat(chunks).toString('utf-8');
            if (rawBody) {
              const ct = String(req.headers['content-type'] || '').toLowerCase();
              if (!ct.includes('application/json')) {
                res.writeHead(415, {
                  'Content-Type': 'application/json; charset=utf-8',
                  'Cache-Control': 'no-store',
                });
                res.end(JSON.stringify({ error: 'Content-Type must be application/json' }));
                return;
              }
              Object.assign(data, JSON.parse(rawBody));
            }

            const result = await handler(data);
            if (result && typeof result === 'object' && result.contentType && result.body !== undefined) {
              const buf = typeof result.body === 'string' ? result.body : Buffer.from(result.body);
              res.writeHead(result.status || 200, {
                'Cache-Control': 'no-cache, no-store',
                'Content-Type': result.contentType,
                ...(result.headers || {}),
              });
              res.end(buf);
              return;
            }
            res.writeHead(200, {
              'Content-Type': 'application/json; charset=utf-8',
              'Cache-Control': 'no-cache, no-store',
            });
            res.end(JSON.stringify(result));
          } catch (err) {
            if (!res.headersSent) {
              res.writeHead(500, {
                'Content-Type': 'application/json; charset=utf-8',
                'Cache-Control': 'no-store',
              });
              res.end(JSON.stringify({ error: sanitizeErrorText(err.message || String(err)) }));
            }
          }
        });
        return;
      }

      res.writeHead(404, {
        'Content-Type': 'application/json; charset=utf-8',
        'Cache-Control': 'no-store',
      });
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
    join(HOME_DIR, '.gemini', 'antigravity', 'lib', 'node_modules', 'sidecar_sdk', 'index.mjs')
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
// SillyTavern Data Root Discovery (Read-Only, Aligned with Python RuntimeContext)
// ---------------------------------------------------------------------------
export function resolveStDataRoot(workspaceUri = '', stateDir = STATE_DIR) {
  // 1. Explicit environment overrides (`ST_DATA_ROOT` > `ST_USER_DATA_DIR`)
  for (const envVal of [process.env.ST_DATA_ROOT, process.env.ST_USER_DATA_DIR]) {
    if (envVal && !String(envVal).startsWith('${')) {
      const envPath = normalizeWinPath(envVal);
      if (isExistingDir(envPath)) return envPath;
    }
  }

  // 2. Remembered `last_st_data_root` from `plugin_settings.json` (maintained under lock by Python)
  try {
    const settingsPath = join(stateDir, 'plugin_settings.json');
    if (existsSync(settingsPath)) {
      const s = JSON.parse(readFileSync(settingsPath, 'utf-8'));
      if (s && s.last_st_data_root && !String(s.last_st_data_root).startsWith('${')) {
        const remPath = normalizeWinPath(s.last_st_data_root);
        if (isExistingDir(remPath)) return remPath;
      }
    }
  } catch {
    // Ignore transient lock or parse error
  }

  // 3. Active workspace relative discovery (never overwrites `plugin_settings.json` unlocked)
  if (workspaceUri) {
    try {
      const wsRaw = workspaceUri.startsWith('file://') ? fileURLToPath(workspaceUri) : workspaceUri;
      const wsBase = normalizeWinPath(wsRaw);
      for (const rel of ['data/default-user', 'SillyTavern/data/default-user', '../SillyTavern/data/default-user']) {
        const cand = normalizeWinPath(resolve(wsBase, rel));
        if (isExistingDir(cand)) {
          return cand;
        }
      }
    } catch {
      // Ignore malformed URI
    }
  }

  // 4. Local repository / sibling fallbacks
  const localCandidates = [
    resolve(PLUGIN_ROOT, '..', 'SillyTavern', 'data', 'default-user'),
    resolve(PLUGIN_ROOT, '..', 'data', 'default-user'),
    resolve(PLUGIN_ROOT, 'examples', 'mock_st_data', 'default-user'),
  ];
  for (const c of localCandidates) {
    const norm = normalizeWinPath(c);
    if (isExistingDir(norm)) {
      return norm;
    }
  }
  const fallback = normalizeWinPath(resolve(PLUGIN_ROOT, 'examples', 'mock_st_data', 'default-user'));
  mkdirSync(fallback, { recursive: true });
  return fallback;
}

// ---------------------------------------------------------------------------
// Async Non-Blocking Disk ETag Fingerprint (with Directory Listing Cache)
// ---------------------------------------------------------------------------
const DIR_LISTING_CACHE_MAX = 256;
const dirListingCache = new Map(); // normDirPath -> { dirSig, names }

async function statStamp(p) {
  try {
    const st = await stat(p);
    return `${st.size}:${Math.trunc(st.mtimeMs)}`;
  } catch {
    return '0';
  }
}

async function listDirCached(dirPath, dirSt) {
  const key = normalizeWinPath(dirPath);
  const dirSig = `${dirSt.mtimeMs}:${dirSt.ctimeMs}`;
  const hit = dirListingCache.get(key);
  if (hit && hit.dirSig === dirSig) {
    return hit.names;
  }
  const names = await readdir(dirPath);
  if (dirListingCache.size >= DIR_LISTING_CACHE_MAX) {
    const oldestKey = dirListingCache.keys().next().value;
    if (oldestKey !== undefined) dirListingCache.delete(oldestKey);
  }
  dirListingCache.set(key, { dirSig, names });
  return names;
}

export async function dirStamp(dirPath) {
  try {
    let dirSt;
    try {
      dirSt = await stat(dirPath);
    } catch (err) {
      return err && err.code === 'ENOENT' ? 'missing' : 'err';
    }
    if (!dirSt.isDirectory()) return 'missing';

    const names = await listDirCached(dirPath, dirSt);
    let maxMtime = 0;
    let totalSize = 0;
    const BATCH = 64;
    for (let i = 0; i < names.length; i += BATCH) {
      const slice = names.slice(i, i + BATCH);
      const stats = await Promise.all(
        slice.map((n) => stat(join(dirPath, n)).catch(() => null))
      );
      for (const st of stats) {
        if (!st) continue;
        if (st.mtimeMs > maxMtime) maxMtime = st.mtimeMs;
        totalSize += st.size;
      }
    }
    return `${names.length}:${totalSize}:${Math.trunc(maxMtime)}`;
  } catch {
    return 'err';
  }
}

async function resolveExistingSubdirCaseInsensitive(parentDir, targetName) {
  if (!targetName) return null;
  const direct = join(parentDir, targetName);
  try {
    const st = await stat(direct);
    if (st.isDirectory()) return direct;
  } catch {
    // Fall back to case-insensitive scan on POSIX
  }
  try {
    const pSt = await stat(parentDir);
    if (!pSt.isDirectory()) return null;
    const entries = await listDirCached(parentDir, pSt);
    const lower = targetName.toLowerCase();
    for (const entry of entries) {
      if (entry.toLowerCase() === lower) {
        const sub = join(parentDir, entry);
        const st = await stat(sub).catch(() => null);
        if (st && st.isDirectory()) return sub;
      }
    }
  } catch {
    // Ignore missing parent
  }
  return null;
}

export async function computeDiskEtag(dataRoot, stateDir = STATE_DIR) {
  const normRoot = normalizeWinPath(dataRoot);
  const h = createHash('sha1');
  const sessFile = join(stateDir, 'active_session.json');
  const [sessStamp, cfgStamp, sugStamp] = await Promise.all([
    statStamp(sessFile),
    statStamp(join(stateDir, 'plugin_settings.json')),
    statStamp(join(stateDir, 'stage_suggestions.json')),
  ]);
  h.update(`root=${normRoot}|`);
  h.update(`sess=${sessStamp}|`);
  h.update(`cfg=${cfgStamp}|`);
  h.update(`sug=${sugStamp}|`);

  const charDir = join(normRoot, 'characters');
  try {
    const rawSess = await readFile(sessFile, 'utf-8');
    const sess = JSON.parse(rawSess);
    if (sess && typeof sess === 'object') {
      if (sess.chat_path) {
        h.update(`chat=${await statStamp(sess.chat_path)}|`);
      }
      const chatKey = sess.avatar_stem || sess.character_name;
      if (chatKey) {
        const safeChatStem = normalizeAvatarStem(chatKey);
        if (safeChatStem) {
          h.update(`cdir=${await dirStamp(join(normRoot, 'chats', safeChatStem))}|`);
        }
      }
      // Track active character's expression subdirectories (`characters/<card_stem>` & `characters/<char_name>`)
      const exprCandidates = new Set();
      if (sess.card_file) exprCandidates.add(normalizeAvatarStem(sess.card_file));
      if (sess.avatar_stem) exprCandidates.add(normalizeAvatarStem(sess.avatar_stem));
      if (sess.character_name) exprCandidates.add(sanitizeFilenameStem(sess.character_name));
      exprCandidates.delete('');
      for (const stem of Array.from(exprCandidates).sort()) {
        const resolvedExprDir = await resolveExistingSubdirCaseInsensitive(charDir, stem);
        const stamp = resolvedExprDir ? await dirStamp(resolvedExprDir) : 'missing';
        h.update(`expr:${stem}=${stamp}|`);
      }
    }
  } catch (err) {
    if (!err || err.code !== 'ENOENT') {
      h.update('sess_parse_err|');
    }
  }

  const [charsStamp, worldsStamp] = await Promise.all([
    dirStamp(charDir),
    dirStamp(join(normRoot, 'worlds')),
  ]);
  h.update(`chars=${charsStamp}|`);
  h.update(`worlds=${worldsStamp}|`);
  return h.digest('hex').slice(0, 16);
}

// ---------------------------------------------------------------------------
// Strict Magic-Byte Media Sandbox
// ---------------------------------------------------------------------------
export function sniffRasterMime(buf) {
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

export function isContainedInside(childReal, parentReal) {
  const normChild = process.platform === 'win32' ? childReal.toLowerCase() : childReal;
  const normParent = process.platform === 'win32' ? parentReal.toLowerCase() : parentReal;
  const rel = relative(normParent, normChild);
  return Boolean(rel) && !rel.startsWith('..') && !isAbsolute(rel);
}

function findSubdirSyncCaseInsensitive(realCharRoot, dirName) {
  if (!dirName) return null;
  const direct = join(realCharRoot, dirName);
  try {
    if (existsSync(direct) && statSync(direct).isDirectory()) {
      return direct;
    }
  } catch {
    // Ignore
  }
  const targetLower = dirName.toLowerCase();
  try {
    for (const entry of readdirSync(realCharRoot)) {
      if (entry.toLowerCase() === targetLower) {
        const sub = join(realCharRoot, entry);
        try {
          if (statSync(sub).isDirectory()) return sub;
        } catch {
          // Ignore broken entry
        }
      }
    }
  } catch {
    // Ignore unreadable root
  }
  return null;
}

export function serveMediaAsset(data, ResponseClass = FallbackResponse) {
  try {
    const dataRoot = resolveStDataRoot(data.workspaceUri || '');
    const charRoot = join(dataRoot, 'characters');
    if (!isExistingDir(charRoot)) {
      return jsonResponse(ResponseClass, { error: 'characters directory not found' }, 404, 'no-cache');
    }
    const realCharRoot = realpathSync(charRoot);
    const kind = String(data.kind || 'avatar');
    let candidatePath = null;

    if (kind === 'avatar') {
      const rawFile = String(data.file || '').trim();
      if (!isSafeSingleSegment(rawFile) || WIN_RESERVED_DEVICE_RE.test(rawFile)) {
        return jsonResponse(ResponseClass, { error: 'Invalid avatar filename' }, 400, 'no-cache');
      }
      const p = join(realCharRoot, rawFile);
      if (existsSync(p) && statSync(p).isFile()) {
        candidatePath = p;
      }
    } else if (kind === 'expression') {
      const rawCard = String(data.card || '').trim();
      const rawChar = String(data.char || '').trim();
      const rawExpr = String(data.expr || '').trim();
      const safeCharStem = rawChar ? sanitizeFilenameStem(rawChar) : '';
      if (
        !rawExpr ||
        !isSafeSingleSegment(rawExpr) ||
        WIN_RESERVED_DEVICE_RE.test(rawExpr) ||
        (rawCard && !isSafeSingleSegment(rawCard)) ||
        (!rawCard && !safeCharStem)
      ) {
        return jsonResponse(ResponseClass, { error: 'Invalid expression parameters' }, 400, 'no-cache');
      }

      // Prefer `card` (avatar filename stem) matching SillyTavern & Python `snapshot`,
      // then fall back to sanitized `char` (`sanitizeFilenameStem` + `isContainedInside`).
      const dirCandidates = [];
      const addCandidate = (v) => {
        if (v && !dirCandidates.includes(v)) dirCandidates.push(v);
      };
      if (rawCard) {
        addCandidate(normalizeAvatarStem(rawCard));
        addCandidate(rawCard.replace(/\.(?:png|json)$/i, '').trim());
      }
      if (safeCharStem) {
        addCandidate(safeCharStem);
      }

      outer: for (const candDirName of dirCandidates) {
        const exprDir = findSubdirSyncCaseInsensitive(realCharRoot, candDirName);
        if (!exprDir) continue;
        for (const ext of ['.png', '.webp', '.jpg', '.jpeg', '.gif']) {
          const p = join(exprDir, `${rawExpr}${ext}`);
          try {
            if (existsSync(p) && statSync(p).isFile()) {
              candidatePath = p;
              break outer;
            }
          } catch {
            // Ignore transient race
          }
        }
      }
    } else {
      return jsonResponse(ResponseClass, { error: 'Unsupported media kind' }, 400, 'no-cache');
    }

    if (!candidatePath) {
      return jsonResponse(ResponseClass, { error: 'Media asset not found' }, 404, 'no-cache');
    }

    const realTarget = realpathSync(candidatePath);
    if (!isContainedInside(realTarget, realCharRoot)) {
      return jsonResponse(ResponseClass, { error: 'Path containment violation blocked' }, 403, 'no-cache');
    }

    const rawBytes = readFileSync(realTarget);
    const mime = sniffRasterMime(rawBytes);
    if (!mime) {
      return jsonResponse(
        ResponseClass,
        { error: 'Unsupported or unsafe media payload (raster magic-byte verification failed)' },
        415,
        'no-cache'
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
  } catch (err) {
    if (err && err.code === 'ENOENT') {
      return jsonResponse(ResponseClass, { error: 'Media asset not found' }, 404, 'no-cache');
    }
    return jsonResponse(
      ResponseClass,
      { error: sanitizeErrorText(err.message || 'Media read error') },
      500,
      'no-cache'
    );
  }
}

// ---------------------------------------------------------------------------
// Python Control-Plane RPC Bridge & Process-Tree Lifecycle
// ---------------------------------------------------------------------------
function resolveOpTimeoutMs(op) {
  if (op === 'byok_turn') {
    const customByok = Number(process.env.TAVERN_BYOK_TIMEOUT_MS);
    return customByok > 0 ? customByok : 180000;
  }
  const customOp = Number(process.env.TAVERN_OP_TIMEOUT_MS);
  return customOp > 0 ? customOp : 20000;
}

function resolveCancelGraceMs() {
  const customGrace = Number(process.env.TAVERN_CANCEL_GRACE_MS);
  return customGrace > 0 ? customGrace : 1500;
}

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

export function terminateChildProcessTree(child) {
  if (!child) return Promise.resolve();
  const pid = child.pid;
  if (!Number.isInteger(pid) || pid <= 0) {
    try {
      child.kill();
    } catch {
      // ignore
    }
    return Promise.resolve();
  }

  // Strictly target ONLY the exact spawned `child.pid` via `/PID <pid> /T /F` on Windows.
  if (process.platform === 'win32') {
    return new Promise((resolveDone) => {
      let done = false;
      const finish = () => {
        if (!done) {
          done = true;
          resolveDone();
        }
      };
      try {
        const tk = spawn('taskkill', ['/PID', String(pid), '/T', '/F'], {
          windowsHide: true,
          stdio: 'ignore',
        });
        const guard = setTimeout(() => {
          try {
            tk.kill();
          } catch {
            // ignore
          }
          try {
            child.kill();
          } catch {
            // ignore
          }
          finish();
        }, 3000);
        tk.on('error', () => {
          clearTimeout(guard);
          try {
            child.kill();
          } catch {
            // ignore
          }
          finish();
        });
        tk.on('close', () => {
          clearTimeout(guard);
          try {
            child.kill();
          } catch {
            // ignore
          }
          finish();
        });
      } catch {
        try {
          child.kill();
        } catch {
          // ignore
        }
        finish();
      }
    });
  }

  try {
    child.kill('SIGTERM');
  } catch {
    // ignore
  }
  return Promise.resolve();
}

export function invokePythonOp(op, argsObj, dataRoot, { includeSnapshot = false, onSpawn = null } = {}) {
  const timeoutMs = resolveOpTimeoutMs(op);
  return new Promise((resolvePromise, rejectPromise) => {
    let pySpec;
    try {
      pySpec = getPythonSpec();
    } catch (err) {
      rejectPromise(new Error(sanitizeErrorText(err.message || String(err))));
      return;
    }

    const cliArgs = [
      ...pySpec.prefixArgs,
      getMcpScriptPath(),
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

    // Prevent unhandled `EPIPE` / `EOF` crash when a large payload (>64KB) writes to a child
    // that exits early before draining stdin.
    child.stdin.on('error', () => {});
    child.stdout.on('error', () => {});
    child.stderr.on('error', () => {});

    let closedResolve;
    const closedPromise = new Promise((r) => {
      closedResolve = r;
    });
    if (typeof onSpawn === 'function') {
      onSpawn({ child, closedPromise });
    }

    const outChunks = [];
    const errChunks = [];
    let outBytes = 0;
    let errBytes = 0;
    let settled = false;
    let timedOut = false;

    const destroyStreams = () => {
      try {
        child.stdin?.destroy();
      } catch {
        // ignore
      }
      try {
        child.stdout?.destroy();
      } catch {
        // ignore
      }
      try {
        child.stderr?.destroy();
      } catch {
        // ignore
      }
    };

    const settleResolve = (val) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolvePromise(val);
    };

    const settleReject = (err) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      const cleanMsg = sanitizeErrorText(err instanceof Error ? err.message : String(err));
      const wrapped = new Error(cleanMsg);
      if (err && err.timedOut) wrapped.timedOut = true;
      rejectPromise(wrapped);
    };

    const timer = setTimeout(() => {
      if (settled) return;
      timedOut = true;
      destroyStreams();
      terminateChildProcessTree(child).finally(() => {
        const timeoutErr = new Error(
          `Python sidecar-op '${op}' timed out after ${(timeoutMs / 1000).toFixed(timeoutMs % 1000 ? 1 : 0)}s`
        );
        timeoutErr.timedOut = true;
        settleReject(timeoutErr);
      });
    }, timeoutMs);

    child.stdout.on('data', (chunk) => {
      if (settled || timedOut) return;
      outBytes += chunk.length;
      if (outBytes <= MAX_CHILD_OUTPUT_BYTES) {
        outChunks.push(chunk);
      }
    });
    child.stderr.on('data', (chunk) => {
      if (settled || timedOut) return;
      errBytes += chunk.length;
      if (errBytes <= 1024 * 1024) {
        errChunks.push(chunk);
      }
    });
    child.on('error', (err) => {
      closedResolve();
      destroyStreams();
      if (timedOut) return;
      settleReject(err);
    });
    child.on('close', () => {
      closedResolve();
      if (settled || timedOut) return;
      const rawOut = Buffer.concat(outChunks).toString('utf-8').trim();
      const stderrText = sanitizeErrorText(Buffer.concat(errChunks).toString('utf-8').trim()).slice(0, 600);
      if (!rawOut) {
        settleReject(new Error(stderrText || `Empty response from Python sidecar-op '${op}'`));
        return;
      }
      try {
        const lastLine = rawOut.split(/\r?\n/).pop();
        const parsed = JSON.parse(lastLine);
        if (!parsed.ok) {
          settleReject(new Error(parsed.error || stderrText || `Sidecar operation '${op}' failed`));
          return;
        }
        settleResolve({
          data: parsed.data,
          snapshot: parsed.snapshot && typeof parsed.snapshot === 'object' ? parsed.snapshot : null,
        });
      } catch (e) {
        const detail = stderrText ? `${e.message} (${stderrText})` : e.message;
        settleReject(new Error(`Invalid JSON from Python sidecar-op '${op}': ${detail}`));
      }
    });

    const wireArgs =
      includeSnapshot && op !== 'snapshot'
        ? { ...(argsObj || {}), _include_snapshot: true }
        : argsObj || {};
    try {
      child.stdin.end(JSON.stringify(wireArgs), 'utf-8');
    } catch {
      // Handled by `child.stdin` error / `close` listener
    }
  });
}

// ---------------------------------------------------------------------------
// Server Registration & Startup
// ---------------------------------------------------------------------------
const STREAM_PREVIEW_PATH = join(STATE_DIR, 'byok_stream_preview.json');
const BYOK_CANCEL_FLAG_PATH = join(STATE_DIR, 'byok_cancel.flag');
const STREAM_PREVIEW_MAX_AGE_S = 600;

async function cleanupStuckByokState(dataRoot) {
  await unlink(STREAM_PREVIEW_PATH).catch(() => {});
  await unlink(BYOK_CANCEL_FLAG_PATH).catch(() => {});
  try {
    await invokePythonOp('cancel_turn', { force_reset: true }, dataRoot, { includeSnapshot: false });
  } catch {
    // Ignore if older backend does not yet implement `cancel_turn`
  }
}

export async function startSidecarServer() {
  let cachedEtag = '';
  let cachedDataRoot = '';
  let cachedSnapshot = null;
  let mutationEpoch = 0;
  let inflightSnapshot = null; // { key, promise }
  let activeTurnDispatch = null; // { op, child, closedPromise, dataRoot }

  const { app, ResponseClass } = await createSidecarHost();

  app.page('/', async () =>
    new ResponseClass(await readFile(join(UI_DIR, 'index.html'), 'utf-8'), {
      contentType: 'text/html; charset=utf-8',
      headers: { 'Cache-Control': 'no-cache' },
    })
  );
  app.page('/styles.css', async () =>
    new ResponseClass(await readFile(join(UI_DIR, 'styles.css'), 'utf-8'), {
      contentType: 'text/css; charset=utf-8',
      headers: { 'Cache-Control': 'no-cache' },
    })
  );
  app.page('/app.js', async () =>
    new ResponseClass(await readFile(join(UI_DIR, 'app.js'), 'utf-8'), {
      contentType: 'application/javascript; charset=utf-8',
      headers: { 'Cache-Control': 'no-cache' },
    })
  );

  app.api(
    '/api/state',
    async (data) => {
      const dataRoot = resolveStDataRoot(data.workspaceUri || '');
      const currentEtag = await computeDiskEtag(dataRoot);
      if (
        data.etag &&
        data.etag === currentEtag &&
        cachedSnapshot &&
        cachedDataRoot === dataRoot &&
        cachedEtag === currentEtag
      ) {
        return jsonResponse(ResponseClass, { unchanged: true, etag: currentEtag });
      }
      if (cachedSnapshot && cachedDataRoot === dataRoot && cachedEtag === currentEtag) {
        return jsonResponse(ResponseClass, { unchanged: false, etag: currentEtag, state: cachedSnapshot });
      }

      const startEpoch = mutationEpoch;
      const flightKey = `${dataRoot}::${startEpoch}::${currentEtag}`;
      if (inflightSnapshot && inflightSnapshot.key === flightKey) {
        const shared = await inflightSnapshot.promise;
        return jsonResponse(ResponseClass, shared);
      }

      const promise = (async () => {
        const first = await invokePythonOp('snapshot', {}, dataRoot);
        const postEtag = await computeDiskEtag(dataRoot);
        if (mutationEpoch === startEpoch && postEtag === currentEtag) {
          cachedEtag = postEtag;
          cachedDataRoot = dataRoot;
          cachedSnapshot = first.data;
          return { unchanged: false, etag: postEtag, state: first.data };
        }
        // Concurrent op or disk write occurred while `snapshot` was in flight:
        // 1) If a concurrent `/api/op` already populated the fresh snapshot for `postEtag`, serve it directly.
        if (cachedSnapshot && cachedDataRoot === dataRoot && cachedEtag === postEtag) {
          return { unchanged: false, etag: postEtag, state: cachedSnapshot };
        }
        // 2) Otherwise fetch one clean post-mutation snapshot; never poison cache or pair old state with new etag.
        const retryEpoch = mutationEpoch;
        const retryPreEtag = postEtag;
        const second = await invokePythonOp('snapshot', {}, dataRoot);
        const secondPostEtag = await computeDiskEtag(dataRoot);
        if (mutationEpoch === retryEpoch && secondPostEtag === retryPreEtag) {
          cachedEtag = secondPostEtag;
          cachedDataRoot = dataRoot;
          cachedSnapshot = second.data;
          return { unchanged: false, etag: secondPostEtag, state: second.data };
        }
        return { unchanged: false, etag: '', state: second.data };
      })();

      inflightSnapshot = { key: flightKey, promise };
      try {
        const payload = await promise;
        return jsonResponse(ResponseClass, payload);
      } finally {
        if (inflightSnapshot && inflightSnapshot.promise === promise) {
          inflightSnapshot = null;
        }
      }
    },
    'GET'
  );

  // Live BYOK streaming preview: read directly (no Python spawn) without `existsSync` TOCTOU.
  app.api(
    '/api/stream_preview',
    async () => {
      try {
        const raw = await readFile(STREAM_PREVIEW_PATH, 'utf-8');
        const preview = JSON.parse(raw);
        const age = Date.now() / 1000 - Number(preview.started_at || 0);
        if (!preview.active || age > STREAM_PREVIEW_MAX_AGE_S) {
          return jsonResponse(ResponseClass, { active: false });
        }
        return jsonResponse(ResponseClass, {
          active: true,
          action_kind: String(preview.action_kind || ''),
          text: String(preview.text || ''),
        });
      } catch (err) {
        if (err && err.code === 'ENOENT') {
          return jsonResponse(ResponseClass, { active: false });
        }
        // Mid-rename or partially written: report "no change" rather than an error.
        return jsonResponse(ResponseClass, { active: true, text: null });
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
        return jsonResponse(
          ResponseClass,
          { ok: false, error: `Operation '${op}' is not permitted` },
          400
        );
      }
      const dataRoot = resolveStDataRoot(data.workspaceUri || '');

      // Two-phase `cancel_turn`: cooperative Python flag first, then exact-PID tree kill + force_reset after grace window.
      // Waits for the cancelled `byok_turn` dispatch lock to release BEFORE returning 200 so an immediate next turn
      // never hits a stale HTTP 409 or has its freshly populated snapshot cache wiped by late `catch` cleanup.
      if (op === 'cancel_turn') {
        mutationEpoch += 1;
        cachedSnapshot = null;
        cachedEtag = '';
        const runningByok = activeTurnDispatch?.op === 'byok_turn' ? activeTurnDispatch : null;
        if (runningByok) {
          runningByok.cancelled = true;
        }
        try {
          let cancelRes = await invokePythonOp('cancel_turn', data.args || {}, dataRoot, {
            includeSnapshot: !runningByok,
          });
          if (runningByok) {
            if (runningByok.child && runningByok.child.exitCode === null) {
              await Promise.race([runningByok.closedPromise, sleep(resolveCancelGraceMs())]);
            }
            if (runningByok.child && runningByok.child.exitCode === null) {
              await terminateChildProcessTree(runningByok.child);
              await unlink(STREAM_PREVIEW_PATH).catch(() => {});
              await Promise.race([runningByok.releasedPromise, sleep(2000)]);
              if (activeTurnDispatch === runningByok) {
                activeTurnDispatch = null;
              }
              cancelRes = await invokePythonOp(
                'cancel_turn',
                { ...(data.args || {}), force_reset: true },
                dataRoot,
                { includeSnapshot: true }
              );
            } else {
              await Promise.race([runningByok.releasedPromise, sleep(2000)]);
              if (activeTurnDispatch === runningByok) {
                activeTurnDispatch = null;
              }
              await unlink(STREAM_PREVIEW_PATH).catch(() => {});
              if (!cancelRes.snapshot) {
                const snapOnly = await invokePythonOp('snapshot', {}, dataRoot, { includeSnapshot: false });
                cancelRes = { data: cancelRes.data, snapshot: snapOnly.data };
              }
            }
          }
          mutationEpoch += 1;
          const finishEpoch = mutationEpoch;
          const etag = await computeDiskEtag(dataRoot);
          if (cancelRes.snapshot && mutationEpoch === finishEpoch) {
            cachedDataRoot = dataRoot;
            cachedEtag = etag;
            cachedSnapshot = cancelRes.snapshot;
            return jsonResponse(ResponseClass, {
              ok: true,
              data: cancelRes.data,
              etag,
              state: cancelRes.snapshot,
            });
          }
          cachedSnapshot = null;
          cachedEtag = '';
          return jsonResponse(ResponseClass, { ok: true, data: cancelRes.data, etag });
        } catch (err) {
          return jsonResponse(
            ResponseClass,
            { ok: false, error: sanitizeErrorText(err.message || String(err)) },
            400
          );
        }
      }

      // Server-side single-flight guard for turn dispatch (`byok_turn`, `prepare_agent_turn`).
      const isTurnDispatch = TURN_DISPATCH_OPS.has(op);
      if (isTurnDispatch && activeTurnDispatch !== null) {
        return jsonResponse(
          ResponseClass,
          { ok: false, error: '上一轮回合仍在处理中，请稍候或先停止当前生成。' },
          409
        );
      }

      let releaseDispatchResolve = () => {};
      const releasedPromise = isTurnDispatch
        ? new Promise((r) => {
            releaseDispatchResolve = r;
          })
        : Promise.resolve();
      const dispatchRecord = isTurnDispatch
        ? {
            op,
            child: null,
            closedPromise: Promise.resolve(),
            releasedPromise,
            markReleased: releaseDispatchResolve,
            dataRoot,
            cancelled: false,
          }
        : null;
      if (dispatchRecord) {
        activeTurnDispatch = dispatchRecord;
      }

      mutationEpoch += 1;
      cachedSnapshot = null;
      cachedEtag = '';

      try {
        const resObj = await invokePythonOp(op, data.args || {}, dataRoot, {
          includeSnapshot: op !== 'snapshot',
          onSpawn: dispatchRecord
            ? ({ child, closedPromise }) => {
                dispatchRecord.child = child;
                dispatchRecord.closedPromise = closedPromise;
              }
            : null,
        });
        if (dispatchRecord?.cancelled) {
          const etag = await computeDiskEtag(dataRoot);
          return jsonResponse(ResponseClass, { ok: true, data: resObj.data, etag });
        }
        mutationEpoch += 1;
        const finishEpoch = mutationEpoch;
        const etag = await computeDiskEtag(dataRoot);
        if (resObj.snapshot && mutationEpoch === finishEpoch) {
          cachedDataRoot = dataRoot;
          cachedEtag = etag;
          cachedSnapshot = resObj.snapshot;
          return jsonResponse(ResponseClass, {
            ok: true,
            data: resObj.data,
            etag,
            state: resObj.snapshot,
          });
        }
        cachedSnapshot = null;
        cachedEtag = '';
        return jsonResponse(ResponseClass, {
          ok: true,
          data: resObj.data,
          etag,
        });
      } catch (err) {
        if (!dispatchRecord?.cancelled) {
          mutationEpoch += 1;
          cachedSnapshot = null;
          cachedEtag = '';
          if (op === 'byok_turn') {
            await cleanupStuckByokState(dataRoot);
          }
        }
        return jsonResponse(
          ResponseClass,
          { ok: false, error: sanitizeErrorText(err.message || String(err)) },
          400
        );
      } finally {
        if (dispatchRecord) {
          if (activeTurnDispatch === dispatchRecord) {
            activeTurnDispatch = null;
          }
          dispatchRecord.markReleased();
        }
      }
    },
    'POST'
  );

  return await app.run();
}

if (process.env.TAVERN_STAGE_NO_LISTEN !== '1') {
  await startSidecarServer();
}
