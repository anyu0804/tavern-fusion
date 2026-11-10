/**
 * Cross-platform Python 3.10+ executable resolver for Tavern Fusion.
 * Safely skips Microsoft Store `WindowsApps\python3.exe` stubs on Windows
 * and avoids hardcoding any OS user home paths.
 */
import { execFileSync } from 'node:child_process';
import { existsSync, readdirSync } from 'node:fs';
import { homedir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
export const PLUGIN_ROOT = resolve(__dirname, '..');

function isWorkingPython(cmd, prefixArgs = []) {
  try {
    const out = execFileSync(
      cmd,
      [...prefixArgs, '-c', 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}:{sys.executable}")'],
      { encoding: 'utf-8', timeout: 4000, windowsHide: true, stdio: ['ignore', 'pipe', 'ignore'] }
    ).trim();
    if (!out) return false;
    const [ver, exePath = ''] = out.split(':', 2);
    if (exePath.toLowerCase().includes('windowsapps')) return false;
    const [maj, min] = ver.split('.').map(Number);
    return maj > 3 || (maj === 3 && min >= 10);
  } catch {
    return false;
  }
}

export function resolvePythonCommand() {
  const isWin = process.platform === 'win32';
  const venvRel = isWin ? ['Scripts', 'python.exe'] : ['bin', 'python'];

  const fileCandidates = [
    process.env.TAVERN_PYTHON,
    join(PLUGIN_ROOT, '.venv', ...venvRel),
    join(PLUGIN_ROOT, '..', 'st-antigravity-mcp', '.venv', ...venvRel),
  ].filter(Boolean);

  for (const candidate of fileCandidates) {
    if (existsSync(candidate) && isWorkingPython(candidate)) {
      return { command: candidate, prefixArgs: [] };
    }
  }

  const pathCommands = isWin ? ['python', 'py', 'python3'] : ['python3', 'python'];
  for (const cmd of pathCommands) {
    const extra = cmd === 'py' ? ['-3'] : [];
    if (isWorkingPython(cmd, extra)) {
      return { command: cmd, prefixArgs: extra };
    }
  }

  if (isWorkingPython('uv', ['run', 'python'])) {
    return { command: 'uv', prefixArgs: ['run', 'python'] };
  }

  if (isWin) {
    const winRoots = [
      process.env.LOCALAPPDATA && join(process.env.LOCALAPPDATA, 'Programs', 'Python'),
      join(homedir(), 'AppData', 'Roaming', 'uv', 'python'),
    ].filter(Boolean);
    for (const root of winRoots) {
      if (!existsSync(root)) continue;
      try {
        for (const sub of readdirSync(root).sort().reverse()) {
          const exe = join(root, sub, 'python.exe');
          if (existsSync(exe) && isWorkingPython(exe)) {
            return { command: exe, prefixArgs: [] };
          }
        }
      } catch {
        // Ignore unreadable dirs
      }
    }
  }

  throw new Error(
    'Tavern Fusion could not locate a valid Python >= 3.10 interpreter. ' +
    'Install Python 3.12+ or set TAVERN_PYTHON.'
  );
}
