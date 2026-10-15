// Run with --provision in OS CI: installed Python is deliberately not considered.
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { copyFileSync, cpSync, mkdirSync, mkdtempSync, readFileSync, realpathSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createHash } from 'node:crypto';
import { verifyArchive } from '../plugins/privacy-datamap/scripts/provision-python.mjs';
import { installation, manifest } from '../plugins/privacy-datamap/scripts/python-runtime.mjs';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const temporary = realpathSync(mkdtempSync(join(tmpdir(), 'noru runtime spaces ')));
const scripts = join(temporary, 'plugin with spaces', 'scripts');
const env = { ...process.env, NORU_RUNTIME_CACHE: join(temporary, 'runtime cache') };
delete env.NORU_PYTHON;
if (process.argv.includes('--provision')) env.NORU_PYTHON_MODE = 'managed';

function run(args, environment = env, expected = 0) {
  const result = spawnSync(process.execPath, args, { env: environment, encoding: 'utf8', timeout: 300000 });
  assert.equal(result.status, expected, `${args.join(' ')}\n${result.stdout}\n${result.stderr}\n${result.error || ''}`);
  return result;
}
function python(script, args = [], environment = env, expected = 0) {
  return run([join(scripts, 'python-runtime.mjs'), script, ...args], environment, expected);
}
try {
  cpSync(join(root, 'plugins/privacy-datamap'), dirname(scripts), { recursive: true });
  const bytes = Buffer.from('runtime archive');
  verifyArchive(bytes, createHash('sha256').update(bytes).digest('hex'));
  assert.throws(() => verifyArchive(bytes, '0'.repeat(64)), /SHA-256 mismatch/);
  for (const platform of ['win32', 'darwin', 'linux']) for (const arch of ['x64', 'arm64']) {
    const target = installation(temporary, platform, arch);
    assert.match(target.spec.sha256, /^[a-f0-9]{64}$/);
    assert.ok(target.spec.url.startsWith('https://github.com/astral-sh/python-build-standalone/releases/download/'));
  }
  const setup = JSON.parse(python('--setup').stdout);
  if (env.NORU_PYTHON_MODE === 'managed') assert.ok(setup.executable.startsWith(temporary));
  // A warm managed runtime must work with networking explicitly forbidden.
  const offline = { ...env, NORU_PYTHON: setup.executable, NORU_RUNTIME_OFFLINE: '1' };
  assert.equal(JSON.parse(python('--setup', [], offline).stdout).executable, setup.executable);
  const probe = join(temporary, 'probe with spaces.py');
  writeFileSync(probe, 'import json,sys,subprocess,os\nprint(json.dumps({"args":sys.argv[1:],"python":sys.executable,"node":os.environ["NORU_NODE"]}))\n');
  const result = JSON.parse(python(probe, ['two words', 'literal&value', 'Malmö 日本語'], offline).stdout);
  assert.deepEqual(result.args, ['two words', 'literal&value', 'Malmö 日本語']);
  assert.equal(result.node, process.execPath);
  writeFileSync(probe, 'raise SystemExit(7)\n');
  python(probe, [], offline, 7);
  python('--setup', [], { ...offline, NORU_PYTHON: join(temporary, 'missing python') }, 2);
  python('--setup', [], { ...env, NORU_PYTHON: '', NORU_PYTHON_MODE: 'managed', NORU_RUNTIME_CACHE: join(temporary, 'empty'), NORU_RUNTIME_OFFLINE: '1' }, 2);

  function workflow(environment, name) {
    const repo = join(temporary, name); mkdirSync(repo);
    writeFileSync(join(repo, 'schema.sql'), 'CREATE TABLE contacts (\n  id INTEGER PRIMARY KEY,\n  email TEXT\n);\n');
    const collection = JSON.parse(run([join(scripts, 'collect.mjs'), `--repo=${repo}`, '--output=json'], environment).stdout);
    assert.ok(collection);
    python('reconcile.py', [`--repo=${repo}`, '--output=json'], environment);
    assert.equal(JSON.parse(python('field_review.py', ['--repo', repo], environment).stdout).status, 'classification incomplete');
    python('field_review.py', ['--repo', repo, '--check'], environment, 1);
    const summary = JSON.parse(python('investigate.py', ['--repo', repo, '--summary'], environment).stdout);
    assert.ok(summary.work.some(row => row.kind === 'field'));
    const review = python('review.py', [`--repo=${repo}`, '--output=json'], environment, 1);
    assert.ok(JSON.parse(review.stdout));
    // Review correctly reports unfinished semantic work, never a successful complete scan.
    assert.ok(!review.stderr.includes('Traceback'));
  }
  workflow(offline, 'repository with spaces');
  python(join(root, 'scripts/test_enrichment_workflow.py'), [], offline);
  if (env.NORU_PYTHON_MODE === 'managed') {
    // Reuse an existing installation under a name other than python3, including on Windows.
    const renamed = join(dirname(setup.executable), process.platform === 'win32' ? 'existing interpreter.exe' : 'existing interpreter');
    copyFileSync(setup.executable, renamed);
    const existing = { ...offline, NORU_PYTHON: renamed, NORU_PYTHON_MODE: '' };
    assert.equal(JSON.parse(python('--setup', [], existing).stdout).executable, renamed);
    workflow(existing, 'existing runtime repository');
  }
  console.log(`Privacy runtime workflow passed on ${process.platform}/${process.arch}; provisioned=${env.NORU_PYTHON_MODE === 'managed'}.`);
} finally { rmSync(temporary, { recursive: true, force: true }); }
