"""Exercise the real pinned validator plus the adapter's fail-closed decisions."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'plugins/privacy-datamap/scripts'))
from check_privacy import PACKAGE, check, reconciliation_failures, require_expiry, run_json


class PrivacyGateTests(unittest.TestCase):
    def validate(self, fixture, date='2026-08-21'):
        return run_json([sys.executable, str(PACKAGE / 'scripts/validate_manifest.py'),
                        str(PACKAGE / 'fixtures' / fixture),
                        '--output=json', '--as-of=' + date], Path.cwd(), (0, 1))

    def test_pinned_validator_accepts_reviewed_fixture(self):
        status, report = self.validate('valid.privacy-datamap.yml')
        self.assertEqual(status, 0, report)

    def test_unknown_taxonomy_blocked(self):
        status, report = self.validate('invalid-unknown-category.privacy-datamap.yml')
        self.assertEqual(status, 1)
        self.assertTrue(any('unknown fideslang' in e['message'] for e in report['errors']))

    def test_unresolved_review_blocked(self):
        status, report = self.validate('invalid-needs-review.privacy-datamap.yml')
        self.assertEqual(status, 1)
        self.assertTrue(any('needs_review' in e['path'] for e in report['errors']))

    def test_changed_collection_signature_blocked(self):
        status, report = self.validate('invalid-structure-changed.privacy-datamap.yml')
        self.assertEqual(status, 1)
        self.assertTrue(any('structure_digest' in e['path'] for e in report['errors']))

    def test_expired_review_blocked(self):
        status, report = self.validate('valid.privacy-datamap.yml', '2030-01-01')
        self.assertEqual(status, 1)
        self.assertTrue(any('expired' in e['message'] for e in report['errors']))

    def test_missing_baseline_is_actionable(self):
        with tempfile.TemporaryDirectory() as folder:
            failures = check(Path(folder))
        self.assertEqual(len(failures), 3)
        self.assertTrue(all(f.startswith('BASELINE_MISSING:') for f in failures))

    def test_report_success_does_not_hide_processing_changes(self):
        failures = reconciliation_failures({'ok': True, 'accepted_current': False,
                                           'investigation_required': [{'id': 'welcome-payload'}]})
        self.assertTrue(any('PROCESSING_REVIEW_REQUIRED' in f for f in failures))

    def test_unknown_report_shape_fails_closed(self):
        self.assertTrue(reconciliation_failures({'ok': True}))

    def test_current_baseline_passes(self):
        self.assertEqual(reconciliation_failures({'accepted_current': True}), [])

    def test_missing_expiry_blocked_even_if_validator_warns(self):
        self.assertTrue(require_expiry({'interpretation': {'owner': 'Synthetic test'}}))

class EndToEndGateTests(unittest.TestCase):
    """Synthetic decisions below are test data, never customer approval records."""
    def test_real_baseline_and_mutations(self):
        import importlib.util
        import json
        from datetime import datetime, timedelta, timezone
        import validate_manifest
        import types
        def dump(value, **kwargs):
            program = "import {toYaml} from './plugins/privacy-datamap/scripts/collect.mjs'; let s=''; for await (const x of process.stdin) s+=x; process.stdout.write(toYaml(JSON.parse(s)));"
            return subprocess.run(['node', '--input-type=module', '-e', program], input=json.dumps(value), capture_output=True, text=True, check=True).stdout
        yaml = types.SimpleNamespace(safe_load=lambda text: validate_manifest.load_yaml(text)[0], safe_dump=dump)
        with tempfile.TemporaryDirectory() as folder:
            repo = Path(folder)
            def git(*args):
                subprocess.run(['git', '-C', folder, *args], check=True, capture_output=True)
            git('init', '-b', 'main')
            git('remote', 'add', 'origin', 'https://example.test/synthetic/privacy.git')
            (repo / '.gitignore').write_text('.noru/.cache/\n')
            (repo / 'schema.sql').write_text('CREATE TABLE users (\n    email TEXT NOT NULL\n);\n')
            (repo / 'app.py').write_text('def deliver(email):\n    return {"recipient": email}\n')
            git('add', '.')
            git('-c', 'user.name=Synthetic Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-m', 'Synthetic fixture')
            scripts = PACKAGE / 'scripts'
            run_json(['node', str(scripts/'collect.mjs'), f'--repo={repo}', '--output=json'], repo)
            path = repo / '.noru/privacy-datamap.yml'
            manifest = yaml.safe_load(path.read_text())
            now = datetime.now(timezone.utc).date()
            interpretation = {'owner': 'Synthetic Test Reviewer', 'decided_at': now.isoformat(),
                              'expires_at': (now+timedelta(days=30)).isoformat(),
                              'rationale': 'Synthetic fixture decision used only in automated tests.', 'refs': ['app.py:1']}
            for dataset in manifest['dataset']:
                for collection in dataset['collections']:
                    collection.pop('needs_review', None)
                    collection['interpretation'] = interpretation.copy()
                    for field in collection['fields']:
                        field.pop('needs_review', None)
                        field['data_categories'] = ['user.contact.email']
            for system in manifest['system']:
                system['privacy_declarations'] = [{'name':'Synthetic delivery','data_use':'essential.service',
                    'data_subjects':['customer'],'data_categories':['user.contact.email'],
                    'refs':['app.py:1'],'interpretation':interpretation.copy()}]
            spec = importlib.util.spec_from_file_location('gate_test_dependencies', scripts/'dependencies.py')
            sys.path.insert(0, str(scripts))
            try:
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                dep = {'id':'delivery','path':'app.py','method':'python_ast','selector':'deliver',
                       'targets':['system:'+manifest['system'][0]['fides_key']]}
                dep['fingerprint'] = module.observe(repo, dep)['fingerprint']
            finally:
                sys.path.pop(0)
            manifest['evidence_dependencies'] = [dep]
            path.write_text(yaml.safe_dump(manifest, sort_keys=False))
            git('add', '.noru/privacy-datamap.yml')
            run_json([sys.executable, str(scripts/'reconcile.py'), f'--repo={repo}', '--seal', '--output=json'], repo)
            parsed = repo/'.noru/.cache/privacy-datamap.parsed.json'
            run_json([sys.executable, str(scripts/'validate_manifest.py'), str(path), '--output=json', f'--emit-parsed={parsed}'], repo)
            run_json(['node', str(scripts/'collect.mjs'), f'--repo={repo}', '--output=json'], repo)
            git('add', '.noru/privacy-datamap.lock.json', '.fides/datamap.yml')
            accepted = {name: (repo/name).read_bytes() for name in ('.noru/privacy-datamap.yml','.noru/privacy-datamap.lock.json','.fides/datamap.yml')}
            self.assertEqual(check(repo), [])
            self.assertEqual(accepted, {name:(repo/name).read_bytes() for name in accepted})
            # The actual upstream fingerprint logic catches processing-only changes.
            (repo/'app.py').write_text('def deliver(email):\n    return {"recipient": email, "new_provider": True}\n')
            self.assertTrue(any('PROCESSING_REVIEW_REQUIRED' in f for f in check(repo)))
            (repo/'app.py').write_text('def deliver(email):\n    return {"recipient": email}\n')
            (repo/'schema.sql').write_text('CREATE TABLE users (\n    email TEXT NOT NULL,\n    phone_number TEXT\n);\n')
            self.assertTrue(any('SCHEMA_' in f for f in check(repo)))
            (repo/'schema.sql').write_text('CREATE TABLE users (\n    email TEXT NOT NULL\n);\n')
            export = repo/'.fides/datamap.yml'
            value = yaml.safe_load(export.read_text())
            value['dataset'][0]['name'] = 'Unreviewed export edit'
            export.write_text(yaml.safe_dump(value))
            self.assertTrue(any('EXPORT_MISMATCH' in f for f in check(repo)))

import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from publish_datamap import NoRedirect, publish, read_manifest


class PublisherTests(unittest.TestCase):
    def read(self, content):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "map.yml"
            path.write_text(content)
            return read_manifest(path)

    def test_valid_manifest(self):
        self.assertEqual(self.read("dataset:\n  - fides_key: demo\n")["dataset"][0]["fides_key"], "demo")

    def test_rejects_destructive_or_ambiguous_input(self):
        for content in ("{}", "dataset: []", "dataset: null", "dataset: {}", "dataset: []\ndataset: []", "dataset: [{fides_key: demo}, {fides_key: demo}]", "dataset: [{fides_key: demo, date: 2026-01-01}]"):
            with self.subTest(content=content), self.assertRaises((ValueError, TypeError)):
                self.read(content)

    def test_request_contract_without_network(self):
        payload = {"slug": "demo/app", "manifest": {"dataset": [{"fides_key": "demo"}]}, "commitSha": "abc", "branch": "main"}
        with patch("publish_datamap.urllib.request.build_opener") as factory:
            factory.return_value.open.return_value = io.BytesIO(b'{"data":{"unchanged":true,"warnings":[]}}')
            result = publish(payload, "https://api.example.test", "test-key")
            request = factory.return_value.open.call_args.args[0]
            self.assertEqual(request.full_url, "https://api.example.test/v1/privacy/datamaps")
            self.assertEqual(request.get_method(), "POST")
            self.assertEqual(json.loads(request.data), payload)
            self.assertEqual(request.get_header("Authorization"), "Bearer test-key")
            self.assertTrue(result["unchanged"])

    def test_refuses_insecure_endpoint_and_redirects(self):
        with self.assertRaises(ValueError):
            publish({}, "http://api.example.test", "test-key")
        self.assertIsNone(NoRedirect().redirect_request(None, None, 302, "", {}, "https://elsewhere.test"))

class PortablePublisherTests(unittest.TestCase):
    def test_explicit_metadata_and_no_provider_autodetection(self):
        import os
        import publish_datamap as publisher
        with tempfile.TemporaryDirectory() as folder:
            file = Path(folder)/'map.json'
            file.write_text(json.dumps({'dataset':[{'fides_key':'example'}]}))
            env = {'NORU_API_BASE_URL':'https://api.example.test', 'NORU_API_KEY':'test-key',
                   'NORU_SOURCE_SLUG':'example/app','CI_COMMIT_BRANCH':'ignored',
                   'CI_COMMIT_SHA':'ignored','GITHUB_SHA':'ignored',
                   'NORU_SOURCE_BRANCH':'neutral','NORU_SOURCE_COMMIT_SHA':'neutral-sha'}
            with patch.dict(os.environ, env, clear=True), patch.object(publisher, 'publish', return_value={'unchanged':True}) as call:
                self.assertEqual(publisher.main([str(file),'--publish','--branch=explicit','--commit-sha=explicit-sha']),0)
                self.assertEqual(call.call_args.args[0]['branch'],'explicit')
                self.assertEqual(call.call_args.args[0]['commitSha'],'explicit-sha')
                self.assertEqual(publisher.main([str(file),'--publish']),0)
                self.assertEqual(call.call_args.args[0]['branch'],'neutral')
                del os.environ['NORU_SOURCE_BRANCH']; del os.environ['NORU_SOURCE_COMMIT_SHA']
                publisher.main([str(file),'--publish'])
                self.assertNotIn('branch',call.call_args.args[0])
                self.assertNotIn('commitSha',call.call_args.args[0])
                call.reset_mock(); publisher.main([str(file)]); call.assert_not_called()
                call.return_value={'unchanged':False,'warnings':['unknown category']}
                self.assertEqual(publisher.main([str(file),'--publish']),1)

    def test_strict_parser_preserves_escapes_and_rejects_duplicates(self):
        from fides_document import load_document
        self.assertEqual(load_document('name: "a\\nquoted \\"value\\""\n')['name'], 'a\nquoted "value"')
        for text in ('name: one\nname: two\n', '{"name":1,"name":2}',
                     'dataset:\n  - fides_key: first\n    fides_key: second\n',
                     'name: &alias hello\n', 'name: one\n---\nname: two\n'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                load_document(text)

if __name__ == '__main__':
    unittest.main()
