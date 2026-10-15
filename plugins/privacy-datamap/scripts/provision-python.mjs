// Downloads only a pinned runtime, never repository dependencies or configuration.
import { createHash } from 'node:crypto';
import { mkdir, mkdtemp, rename, rm, writeFile } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { execFileSync } from 'node:child_process';
import { installation, probe } from './python-runtime.mjs';

export function verifyArchive(bytes, expected) {
  if (createHash('sha256').update(bytes).digest('hex') !== expected) throw new Error('Python runtime archive SHA-256 mismatch; nothing was installed. Retry or report a corrupted upstream download.');
}

async function download(url) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 120000);
  try {
    const response = await fetch(url, { signal: controller.signal });
    if (!response.ok) throw new Error(`Runtime download returned HTTP ${response.status}`);
    const chunks = []; let size = 0;
    for await (const chunk of response.body) {
      size += chunk.length;
      if (size > 100 * 1024 * 1024) throw new Error('Runtime download exceeded the 100 MiB limit');
      chunks.push(chunk);
    }
    return Buffer.concat(chunks);
  } finally { clearTimeout(timer); }
}

export async function provision() {
  const { spec, directory, executable } = installation();
  const root = dirname(directory);
  await mkdir(root, { recursive: true, mode: 0o700 });
  const lock = directory + '.lock';
  let held = false; let temporary;
  try {
    for (let attempts = 0; attempts < 150; attempts++) {
      try { await mkdir(lock); held = true; break; }
      catch (error) {
        if (error.code !== 'EEXIST') throw error;
        if (existsSync(executable) && probe(executable)) return;
        await new Promise(resolve => setTimeout(resolve, 1000));
      }
    }
    if (!held) throw new Error(`Runtime setup is busy. Retry when the other setup finishes; if it was interrupted, remove the stale lock ${lock}.`);
    if (existsSync(executable) && probe(executable)) return;
    if (existsSync(directory)) throw new Error(`The cached runtime is incomplete or unusable. Remove ${directory} and retry --setup.`);
    console.error('Noru: preparing a verified Python runtime before offline analysis (one-time download).');
    temporary = await mkdtemp(join(root, '.python-setup-'));
    const archive = join(temporary, 'runtime.tar.gz');
    const bytes = await download(spec.url);
    verifyArchive(bytes, spec.sha256);
    await writeFile(archive, bytes, { mode: 0o600 });
    const unpacked = join(temporary, 'unpacked');
    await mkdir(unpacked);
    // tar ships with supported Windows 10/11, macOS and Linux. No shell expansion.
    const tar = process.platform === 'win32' ? join(process.env.SystemRoot || 'C:\\Windows', 'System32', 'tar.exe') : '/usr/bin/tar';
    execFileSync(tar, ['-xzf', archive, '-C', unpacked], { timeout: 60000, windowsHide: true, stdio: 'pipe' });
    if (!probe(join(unpacked, spec.executable))) throw new Error('Downloaded Python cannot run on this OS or is blocked by endpoint protection.');
    await writeFile(join(unpacked, 'noru-runtime.json'), JSON.stringify({ url: spec.url, sha256: spec.sha256 }) + '\n');
    await rename(unpacked, directory);
    if (!probe(executable)) throw new Error('Python verification failed after installing the runtime.');
  } finally {
    if (temporary) await rm(temporary, { recursive: true, force: true });
    if (held) await rm(lock, { recursive: true, force: true });
  }
}

if (process.argv[1]?.endsWith('provision-python.mjs')) {
  provision().catch(error => {
    console.error(`Python runtime setup failed: ${error.message}\nCheck write access to the Noru runtime cache and HTTPS access to github.com/release-assets.githubusercontent.com, then retry python-runtime.mjs --setup. No scan was completed.`);
    process.exitCode = 2;
  });
}
