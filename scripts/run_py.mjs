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

// Lifecycle hooks are not guaranteed to run with the plugin root as cwd. Keep the
// historic cwd-relative behaviour first, then fall back to the launcher's own
// location (derived from import.meta.url in py_resolver.mjs) so a hook script is
// still found when the host starts us from a workspace directory. An unexpanded
// ${PLUGIN_ROOT} placeholder is also normalized here as a defensive measure.
const rawScript = rawArgs[0].replace(/\$\{PLUGIN_ROOT\}/g, PLUGIN_ROOT);
const cwdCandidate = resolve(process.cwd(), rawScript);
const pluginCandidate = resolve(PLUGIN_ROOT, rawScript);
const scriptPath = existsSync(cwdCandidate)
  ? cwdCandidate
  : existsSync(pluginCandidate)
    ? pluginCandidate
    : cwdCandidate;
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
