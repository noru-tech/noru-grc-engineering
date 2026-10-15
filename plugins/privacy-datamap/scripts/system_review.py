"""Evidence and flow binding for explicit non-personal processing reviews."""
import hashlib
import json
import pathlib


def binding(system, refs, repo):
    files = {}
    root = pathlib.Path(repo).resolve()
    for ref in refs:
        path, line = ref.rsplit(':', 1)
        full = (root / path).resolve()
        if not full.is_relative_to(root) or not full.is_file() or int(line) < 1:
            raise ValueError('Non-personal system review requires current repository citations')
        content = full.read_bytes()
        if int(line) > len(content.splitlines()): raise ValueError('Non-personal system citation exceeds source length')
        files[path] = hashlib.sha256(content).hexdigest()
    return hashlib.sha256(json.dumps({'files': files, 'system': {key: system.get(key) for key in
        ('fides_key', 'dataset_references', 'ingress', 'egress')}}, sort_keys=True).encode()).hexdigest()


def needs_review(system, repo):
    record = system.get('meta', {}).get('non_personal_processing')
    if not record: return False
    try:
        return bool(record.get('needs_review') or record.get('evidence_binding') != binding(system, record.get('refs', []), repo))
    except (ValueError, OSError, TypeError):
        return True
