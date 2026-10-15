// Client-independent Python selection. Existing prerequisites only; no installation or network access.
import { execFileSync, spawnSync } from 'node:child_process';
import { existsSync, realpathSync } from 'node:fs';
import { delimiter, dirname, isAbsolute, join, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
export const probeCode = 'import sys,json,ast,hashlib,pathlib,subprocess,ssl,sqlite3,ctypes,xml.etree.ElementTree; assert sys.version_info >= (3,9); print(json.dumps({"executable":sys.executable,"version":list(sys.version_info[:3])}))';
let selected;

export function probe(command, prefix = []) {
  const result = spawnSync(command, [...prefix, '-I', '-c', probeCode], { encoding: 'utf8', timeout: 15000, windowsHide: true });
  if (result.error || result.status !== 0) return null;
  try {
    const info = JSON.parse(result.stdout);
    return isAbsolute(info.executable) && existsSync(info.executable) ? realpathSync(info.executable) : null;
  } catch { return null; }
}

function candidates() {
  // Never search the target working directory or relative PATH entries for executables.
  const names = process.platform === 'win32' ? ['python.exe', 'python3.exe'] : ['python3', 'python'];
  return (process.env.PATH || process.env.Path || '').split(delimiter)
    .filter(p => isAbsolute(p) && resolve(p) !== process.cwd())
    .flatMap(p => names.map(n => ({ command: join(p, n), prefix: [] })))
    .filter(p => existsSync(p.command));
}

export function resolvePython() {
  if (selected) return selected;
  if (process.env.NORU_PYTHON) {
    if (!isAbsolute(process.env.NORU_PYTHON)) throw new Error('NORU_PYTHON must be an absolute executable path, without shell quotes or arguments.');
    selected = probe(process.env.NORU_PYTHON);
    if (!selected) throw new Error('NORU_PYTHON failed the Python 3.9+ standard-library check. Point it to an existing compatible interpreter, or remove it to search PATH.');
  } else {
    for (const candidate of candidates()) {
      selected = probe(candidate.command, candidate.prefix);
      if (selected) break;
    }
    if (!selected) throw new Error('Python 3.9+ with its standard library is required. Make an existing python or python3 executable available on PATH, or set NORU_PYTHON to its absolute path. The plugin does not install or download prerequisites. No scan was completed.');
  }
  process.env.NORU_PYTHON = selected;
  process.env.NORU_NODE = process.execPath;
  process.env.PYTHONUTF8 = '1';
  return selected;
}

export function execPythonSync(args, options = {}) {
  return execFileSync(resolvePython(), args, { ...options, windowsHide: true });
}

export function spawnPythonSync(args, options = {}) {
  return spawnSync(resolvePython(), args, { ...options, windowsHide: true });
}

if (process.argv[1] && existsSync(process.argv[1]) && pathToFileURL(realpathSync(process.argv[1])).href === import.meta.url) {
  try {
    const [script, ...args] = process.argv.slice(2);
    if (!script) throw new Error('Usage: node python-runtime.mjs --check | <script.py> [arguments]');
    const python = resolvePython();
    if (script === '--check') {
      if (Number(process.versions.node.split('.')[0]) < 18) throw new Error('Node 18+ is required. Select a compatible existing Node installation.');
      const git = spawnSync('git', ['--version'], { encoding: 'utf8', windowsHide: true, timeout: 15000 });
      if (git.error || git.status !== 0) throw new Error('Git is required on PATH. Make an existing Git installation available and retry. No scan was completed.');
      console.log(JSON.stringify({ executable: python, node: process.execPath, git: git.stdout.trim() }));
    }
    else {
      const target = isAbsolute(script) || script.includes('/') || script.includes('\\') ? resolve(script) : join(HERE, script);
      const child = spawnSync(python, [target, ...args], { stdio: 'inherit', windowsHide: true });
      if (child.error) throw child.error;
      process.exitCode = child.status ?? 2;
    }
  } catch (error) {
    console.error(`Python runtime error: ${error.message}`);
    process.exitCode = 2;
  }
}
