// Exercise existing prerequisites without downloading or installing a runtime.
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { copyFileSync, cpSync, mkdirSync, mkdtempSync, readFileSync, realpathSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const temporary = realpathSync(mkdtempSync(join(tmpdir(), 'noru runtime spaces ')));
const scripts = join(temporary, 'plugin with spaces', 'scripts');
const env = { ...process.env };
delete env.NORU_PYTHON;

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
  const setup = JSON.parse(python('--check').stdout);
  const offline = { ...env, NORU_PYTHON: setup.executable };
  assert.equal(JSON.parse(python('--check', [], offline).stdout).executable, setup.executable);
  const missingEnv = { ...env, PATH: temporary, NORU_PYTHON: '' };
  delete missingEnv.Path;
  const missing = python('--check', [], missingEnv, 2);
  assert.match(missing.stderr, /Python 3.9\+.*required/);
  assert.match(missing.stderr, /does not install or download/);
  // Nested calls must use the selected interpreter even with no Python on PATH.
  const selectedEnv = { ...missingEnv, NORU_PYTHON: setup.executable };
  assert.match(python('--check', [], selectedEnv, 2).stderr, /Git is required/);
  const probe = join(temporary, 'probe with spaces.py');
  writeFileSync(probe, 'import json,sys,subprocess,os\nprint(json.dumps({"args":sys.argv[1:],"python":sys.executable,"node":os.environ["NORU_NODE"]}))\n');
  const result = JSON.parse(python(probe, ['two words', 'literal&value', 'Malmö 日本語'], selectedEnv).stdout);
  assert.deepEqual(result.args, ['two words', 'literal&value', 'Malmö 日本語']);
  assert.equal(result.node, process.execPath);
  writeFileSync(probe, 'raise SystemExit(7)\n');
  python(probe, [], offline, 7);
  python('--check', [], { ...offline, NORU_PYTHON: join(temporary, 'missing python') }, 2);


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
  // A venv is created offline from the existing interpreter, without pip or downloads.
  const venv = join(temporary, 'existing Python installation');
  const created = spawnSync(setup.executable, ['-m', 'venv', '--without-pip', venv], { encoding: 'utf8' });
  assert.equal(created.status, 0, created.stderr);
  const bin = join(venv, process.platform === 'win32' ? 'Scripts' : 'bin');
  const original = join(bin, process.platform === 'win32' ? 'python.exe' : 'python');
  const renamed = join(bin, process.platform === 'win32' ? 'existing interpreter.exe' : 'existing interpreter');
  copyFileSync(original, renamed);
  const existing = { ...env, NORU_PYTHON: renamed };
  assert.equal(JSON.parse(python('--check', [], existing).stdout).executable, renamed);
  workflow(existing, 'existing runtime repository');
  console.log(`Privacy runtime workflow passed on ${process.platform}/${process.arch}; existing prerequisites only.`);
} finally { rmSync(temporary, { recursive: true, force: true }); }
