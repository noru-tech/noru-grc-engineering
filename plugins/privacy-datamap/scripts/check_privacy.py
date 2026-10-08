"""Offline privacy gate. Writes diagnostic cache only; never approves or publishes."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT
REQUIRED = ('.noru/privacy-datamap.yml', '.noru/privacy-datamap.lock.json', '.fides/datamap.yml')


def run_json(command, repo, allowed=(0,)):
    result = subprocess.run(command, cwd=repo, text=True, capture_output=True, timeout=120)
    if result.returncode not in allowed:
        raise ValueError(f'Tooling failed ({Path(command[1]).name}): {result.stderr.strip() or result.stdout.strip()}')
    value = json.loads(result.stdout)
    if not isinstance(value, dict):
        raise ValueError('Tool returned an invalid report')
    return result.returncode, value


def reconciliation_failures(report):
    failures = []
    for key, code in (
        ('collection_review_required', 'SCHEMA_REVIEW_REQUIRED'),
        ('proposal_required', 'PRIVACY_REVIEW_REQUIRED'),
        ('investigation_required', 'PROCESSING_REVIEW_REQUIRED'),
        ('discovery_required', 'DISCOVERY_REVIEW_REQUIRED'),
        ('identity_ambiguities', 'AMBIGUOUS_DATASTORE'),
        ('control_changes', 'BASELINE_REVIEW_REQUIRED'),
        ('system_changes', 'SYSTEM_REVIEW_REQUIRED'),
    ):
        if report.get(key):
            failures.append(f'{code}: {json.dumps(report[key], ensure_ascii=False)}')
    if any(report.get('coverage_gaps', {}).values()):
        failures.append('COVERAGE_GAP: ' + json.dumps(report['coverage_gaps']))
    if report.get('accepted_current') is not True:
        failures.append('BASELINE_NOT_CURRENT: review source changes and refresh the accepted baseline outside CI')
    return failures


def require_expiry(node):
    if isinstance(node, list):
        return sum((require_expiry(item) for item in node), [])
    if not isinstance(node, dict):
        return []
    failures = []
    if isinstance(node.get('interpretation'), dict) and not node['interpretation'].get('expires_at'):
        failures.append('APPROVAL_EXPIRY_REQUIRED: every CI-gated decision needs expires_at')
    for value in node.values():
        failures.extend(require_expiry(value))
    return failures


def check(repo):
    failures = []
    for name in REQUIRED:
        if not (repo / name).is_file():
            failures.append(f'BASELINE_MISSING: {name}; complete the privacy scan and human review, then commit the accepted artifacts')
        elif subprocess.run(['git', 'ls-files', '--error-unmatch', '--', name], cwd=repo, capture_output=True).returncode:
            failures.append(f'BASELINE_UNTRACKED: {name}; add the reviewed artifact to Git')
    if failures:
        return failures
    scripts = PACKAGE / 'scripts'
    collected, _ = run_json(['node', str(scripts / 'collect.mjs'), f'--repo={repo}', '--check', '--output=json', '--quiet'], repo, (0, 1))
    if collected:
        failures.append('SCHEMA_DRIFT: observed source does not match the reviewed manifest')
    parsed = repo / '.noru/.cache/privacy-gate.parsed.json'
    valid, validation = run_json([
        sys.executable, str(scripts / 'validate_manifest.py'), str(repo / REQUIRED[0]),
        '--output=json', '--quiet', '--as-of=' + datetime.now(timezone.utc).date().isoformat(),
        f'--emit-parsed={parsed}',
    ], repo, (0, 1))
    if validation.get('ok') is not True:
        failures.extend(f"MANIFEST_INVALID: {e['path']}: {e['message']}" for e in validation.get('errors', []))
        if not validation.get('errors'):
            failures.append('MANIFEST_INVALID: validator did not confirm validity')
    _, reconciliation = run_json([sys.executable, str(scripts / 'reconcile.py'), f'--repo={repo}', '--output=json', '--quiet'], repo)
    failures.extend(reconciliation_failures(reconciliation))
    if valid == 0 and validation.get('ok') is True:
        manifest = json.loads(parsed.read_text())
        failures.extend(require_expiry(manifest))
        if not manifest.get('evidence_dependencies'):
            failures.append('PROCESSING_BASELINE_MISSING: register evidence dependencies for processing decisions; structural coverage alone is insufficient')
        # Reuse the exact upstream projection; never regenerate committed files in CI.
        projection = "import {readFileSync} from 'node:fs'; import {pathToFileURL} from 'node:url'; const {toFideslang}=await import(pathToFileURL(process.argv[1])); console.log(JSON.stringify(toFideslang(JSON.parse(readFileSync(process.argv[2],'utf8')))));"
        _, expected = run_json(['node', '--input-type=module', '-e', projection, str(scripts / 'lib/fides.mjs'), str(parsed)], repo)
        # Use the publisher's duplicate-key rejecting YAML loader.
        from publish_datamap import read_manifest
        if read_manifest(repo / REQUIRED[2]) != expected:
            failures.append('EXPORT_MISMATCH: .fides/datamap.yml differs from the accepted manifest projection; regenerate outside CI')
    return failures


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, default=Path.cwd())
    args = parser.parse_args()
    try:
        failures = check(args.repo.resolve())
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError) as error:
        print(f'FAIL TOOLING_ERROR: {error}')
        return 2
    for failure in failures:
        print('FAIL ' + failure)
    if failures:
        print(f'Privacy gate blocked: {len(failures)} finding(s). No approvals or accepted files were changed.')
        return 1
    print('Privacy gate passed: accepted baseline and export are current within the registered monitoring scope.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
