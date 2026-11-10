#!/usr/bin/env node
/**
 * Transparent cross-platform Python launcher (`scripts/run_py.mjs`).
 * Inherits raw OS stdio descriptors directly so MCP JSON-RPC framing
 * and Antigravity Lifecycle Hook JSON payloads pass with zero buffering.
 */
import { spawn } from 'node:child_process';
import { resolve } from 'node:path';
import { PLUGIN_ROOT, resolvePythonCommand } from './py_resolver.mjs';

const rawArgs = process.argv.slice(2);
if (rawArgs.length === 0) {
  process.stderr.write('Usage: node run_py.mjs <script.py> [args...]\n');
  process.exit(2);
}

const scriptPath = resolve(process.cwd(), rawArgs[0]);
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
