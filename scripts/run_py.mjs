#!/usr/bin/env node
/**
 * Transparent cross-platform Python launcher (`scripts/run_py.mjs`).
 * Inherits raw OS stdio descriptors directly so MCP JSON-RPC framing
 * and Antigravity Lifecycle Hook JSON payloads pass with zero buffering.
 */
import { spawn } from 'node:child_process';
import { existsSync } from 'node:fs';
import { resolve } from 'node:path';
import { PLUGIN_ROOT, resolvePythonCommand } from './py_resolver.mjs';

const rawArgs = process.argv.slice(2);
if (rawArgs.length === 0) {
  process.stderr.write('Usage: node run_py.mjs <script.py> [args...]\n');
  process.exit(2);
}

// Lifecycle hooks are not guaranteed to run with the plugin root as cwd. Resolve
// plugin-owned scripts against PLUGIN_ROOT first so an untrusted workspace containing
// a homonymous `./hooks/*.py` or `./server/*.py` can never hijack hook execution,
// then fall back to cwd for developer CLI invocations (e.g. `tests/*.py`).
const rawScript = rawArgs[0].replace(/\$\{PLUGIN_ROOT\}/g, PLUGIN_ROOT);
const pluginCandidate = resolve(PLUGIN_ROOT, rawScript);
const cwdCandidate = resolve(process.cwd(), rawScript);
const scriptPath = existsSync(pluginCandidate)
  ? pluginCandidate
  : existsSync(cwdCandidate)
    ? cwdCandidate
    : pluginCandidate;
const restArgs = rawArgs.slice(1);
const { command, prefixArgs } = resolvePythonCommand();

const env = {
  ...process.env,
  PYTHONIOENCODING: 'utf-8',
  PYTHONUTF8: '1',
  PLUGIN_ROOT: process.env.PLUGIN_ROOT || PLUGIN_ROOT,
};

const child = spawn(command, [...prefixArgs, scriptPath, ...restArgs], {
  stdio: 'inherit',
  env,
  windowsHide: true,
});

child.on('error', (err) => {
  process.stderr.write(`[Tavern Fusion Launcher Error] ${err.message}\n`);
  process.exit(1);
});

child.on('exit', (code, signal) => {
  if (signal) {
    process.kill(process.pid, signal);
    return;
  }
  process.exit(code ?? 0);
});
