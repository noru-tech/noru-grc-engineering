// Client-independent Python selection. Provisioning precedes offline analysis.
import { execFileSync, spawnSync } from 'node:child_process';
import { existsSync, readFileSync, realpathSync } from 'node:fs';
import { delimiter, dirname, isAbsolute, join, resolve } from 'node:path';
import { homedir } from 'node:os';
import { fileURLToPath, pathToFileURL } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
export const manifest = JSON.parse(readFileSync(join(HERE, '../references/python-runtime.json'), 'utf8'));
export const probeCode = 'import sys,json,ast,hashlib,pathlib,subprocess,ssl,sqlite3,ctypes,xml.etree.ElementTree; assert sys.version_info >= (3,9); print(json.dumps({"executable":sys.executable,"version":list(sys.version_info[:3])}))';
let selected;

export function cacheRoot(env = process.env, platform = process.platform) {
  if (env.NORU_RUNTIME_CACHE) return resolve(env.NORU_RUNTIME_CACHE);
  return platform === 'win32' ? join(env.LOCALAPPDATA || homedir(), 'Noru', 'runtimes')
    : platform === 'darwin' ? join(homedir(), 'Library', 'Caches', 'Noru', 'runtimes')
    : join(env.XDG_CACHE_HOME || join(homedir(), '.cache'), 'noru', 'runtimes');
}

export function installation(root = cacheRoot(), platform = process.platform, arch = process.arch) {
  const spec = manifest.platforms[`${platform}-${arch}`];
  if (!spec) throw new Error(`No managed Python for ${platform}/${arch}. Set NORU_PYTHON to a compatible Python 3.9+ executable or request platform support.`);
  const directory = join(root, `cpython-${manifest.version}-${manifest.release}-${platform}-${arch}`);
  return { spec, directory, executable: join(directory, spec.executable) };
}

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
  const names = process.platform === 'win32' ? ['python.exe', 'python3.exe', 'py.exe'] : ['python3', 'python'];
  return (process.env.PATH || process.env.Path || '').split(delimiter)
    .filter(p => isAbsolute(p) && resolve(p) !== process.cwd())
    .flatMap(p => names.map(n => ({ command: join(p, n), prefix: n === 'py.exe' ? ['-3'] : [] })))
    .filter(p => existsSync(p.command));
}

export function resolvePython() {
  if (selected) return selected;
  if (process.env.NORU_PYTHON) {
    if (!isAbsolute(process.env.NORU_PYTHON)) throw new Error('NORU_PYTHON must be an absolute executable path, without shell quotes or arguments.');
    selected = probe(process.env.NORU_PYTHON);
    if (!selected) throw new Error('NORU_PYTHON failed the Python 3.9+ standard-library check. Remove the override to use automatic runtime setup.');
  } else {
    if (process.env.NORU_PYTHON_MODE !== 'managed') {
      for (const candidate of candidates()) {
        selected = probe(candidate.command, candidate.prefix);
        if (selected) break;
      }
    }
    if (!selected) {
      const target = installation();
      selected = existsSync(target.executable) && probe(target.executable);
      if (!selected) {
        if (process.env.NORU_RUNTIME_OFFLINE === '1') throw new Error('No compatible Python is available offline. Connect once and run node "<plugin>/scripts/python-runtime.mjs" --setup, then retry. No scan was completed.');
        execFileSync(process.execPath, [join(HERE, 'provision-python.mjs')], { stdio: ['ignore', 'ignore', 'inherit'], timeout: 300000, windowsHide: true });
        selected = probe(target.executable);
        if (!selected) throw new Error(`Provisioned Python failed verification at ${target.directory}. Remove that runtime directory and retry --setup; check endpoint protection permissions.`);
      }
    }
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
    if (!script) throw new Error('Usage: node python-runtime.mjs --setup | <script.py> [arguments]');
    const python = resolvePython();
    if (script === '--setup') console.log(JSON.stringify({ executable: python, node: process.execPath }));
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
