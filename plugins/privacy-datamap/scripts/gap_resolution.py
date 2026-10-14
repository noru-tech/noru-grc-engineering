"""Shared extraction dispositions. Observations are never removed by a decision."""
import copy
import hashlib
import json
import pathlib

VERSION = 1
COVERAGE_KEYS = ('unparsed_candidates', 'migration_gaps', 'schema_conflicts')
MUTABLE = {'status', 'resolution', 'resolution_binding', 'resolution_history', 'reopened_reason', 'blocker_reasons'}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


def identity(item):
    return item.get('diagnostic_id') or item.get('gap_id') or 'gap:' + digest({
        'kind': item.get('kind', item.get('format')), 'scope': item.get('scope', item.get('subject', item.get('ref', sorted({span.get('path', '') for span in item.get('evidence', [])})))),
        'rule': item.get('code', item.get('message', item.get('reason')))})


def observations(derived):
    result = []
    for key in COVERAGE_KEYS:
        for raw in derived.get('coverage', {}).get(key, []):
            item = copy.deepcopy(raw)
            item.update(gap_id=identity(raw), kind=raw.get('kind', raw.get('format', key)),
                        message=raw.get('message', raw.get('reason', 'Structural coverage requires investigation.')),
                        coverage_key=key, collector_observation_digest=digest(raw), status='unresolved')
            result.append(item)
    return result


def occurrence_ids(gap):
    occurrences = gap.get('occurrences') or [{k:v for k,v in gap.items() if k not in MUTABLE | {'evidence_ids', 'coverage_key'}}]
    return sorted(set('occurrence:' + digest(item) for item in occurrences))


def binding(gap, contract):
    resolution = gap.get('resolution', {})
    rows = {row['entity_id']: row for row in walk(contract.get('fields', []))}
    return digest({'observation': {k:v for k,v in gap.items() if k not in MUTABLE},
                   'resolution': resolution, 'structure': contract.get('binding', {}).get('structural_digest'),
                   'evidence': {key:contract.get('evidence', {}).get(key) for key in resolution.get('evidence_ids', [])},
                   'targets': {key:rows.get(key, {}).get('observation_digest') for key in resolution.get('field_ids', [])}})


def walk(rows):
    for row in rows:
        yield row
        yield from walk(row.get('nested_members', []))


def observation_errors(gap, repo):
    errors = []
    if repo is not None:
        root = pathlib.Path(repo).resolve()
        for name, expected in gap.get('observation_files', {}).items():
            path = root / name
            if path.is_symlink() or not path.resolve().is_relative_to(root) or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                errors.append('diagnostic source changed: ' + name)
    return errors


def resolution_errors(gap, contract, repo=None):
    import field_review
    decision = gap.get('resolution', {})
    errors = []
    if gap.get('status') != 'resolved' or decision.get('outcome') not in {'modeled', 'not_applicable', 'accepted_certificate'}:
        return ['unresolved']
    errors.extend(observation_errors(gap, repo))
    if gap.get('resolution_binding') != binding(gap, contract):
        errors.append('resolution is unbound or its evidence/structure changed')
    if decision.get('outcome') == 'accepted_certificate':
        if not contract.get('accepted_certificate') or decision.get('certificate_digest') != contract.get('accepted_certificate'):
            errors.append('missing sealed certificate')
        return errors
    provenance = decision.get('provenance', {})
    if not isinstance(provenance, dict) or not provenance.get('method') or provenance.get('evidence') != {key:contract.get('evidence', {}).get(key) for key in decision.get('evidence_ids', [])}:
        errors.append('resolution provenance must retain the cited evidence')
    if not decision.get('rationale') or not decision.get('investigation') or not decision.get('evidence_ids'):
        errors.append('rationale, investigation and evidence are required')
    if decision.get('covered_occurrence_ids') != occurrence_ids(gap):
        errors.append('resolution must explicitly cover every observed occurrence')
    for key in decision.get('evidence_ids', []):
        span = contract.get('evidence', {}).get(key)
        if not span:
            errors.append('missing resolution evidence')
        elif repo is not None:
            errors.extend(field_review.evidence_errors(span, repo, contract.get('migration_exclusions')))
    if decision.get('outcome') == 'not_applicable' and not str(decision.get('scope_rationale', '')).strip():
        errors.append('explain why the finding does not affect represented data')
    if decision.get('outcome') == 'modeled':
        rows = {row['entity_id']: row for row in walk(contract.get('fields', []))}
        targets = decision.get('field_ids', [])
        if not targets or any(key not in rows or not rows[key].get('evidence_ids') or (rows[key].get('open_content') and rows[key].get('open_content_resolution', {}).get('outcome') not in {'bounded_content_contract', 'closed_by_runtime_validation', 'accepted_certificate'}) for key in targets):
            errors.append('modeled resolution requires evidenced, closed structural definitions')
        scope = gap.get('scope', {})
        dataset = scope.get('datastore') or scope.get('dataset')
        if dataset and any(key.split('/')[0] != dataset for key in targets):
            errors.append('modeled definitions do not cover the affected datastore')
        required = gap.get('required_field_ids', [])
        if not set(required) <= set(targets):
            errors.append('modeled definitions omit required structure')
        if gap.get('coverage_key') == 'migration_gaps':
            if not decision.get('coverage_rationale') or not decision.get('identity_rationale'):
                errors.append('migration resolution requires separate datastore identity and schema completeness evidence')
            coverage = decision.get('coverage_binding', {})
            migration_path = gap.get('coverage', {}).get('migration') or gap.get('ref', '').rsplit(':', 1)[0]
            if (not coverage.get('datastore') or any(key.split('/')[0] != coverage['datastore'] for key in targets)
                    or migration_path not in coverage.get('migration_paths', [])
                    or set(coverage.get('field_ids', [])) != set(targets)
                    or not coverage.get('evidence_ids') or not set(coverage['evidence_ids']) <= set(decision.get('evidence_ids', []))):
                errors.append('migration resolution requires an evidenced binding of this migration, datastore and complete target definitions')
    return sorted(set(errors))


def calculate(document, derived, repo=None):
    """All gates consume this partition; a raw gap absent from review remains blocking."""
    import field_review
    contract = document.get('classification_review', {})
    observed = copy.deepcopy(contract.get('gaps', []))
    known = {identity(gap) for gap in observed}
    for item in observations(derived):
        if identity(item) not in known:
            observed.append(item)
        elif any(gap.get('collector_observation_digest') != item['collector_observation_digest'] for gap in observed if identity(gap)==identity(item)):
            observed.append(item)
    stale = bool(contract and (contract.get('binding', {}).get('structural_digest') != field_review.structure_digest(derived)))
    if repo is not None and contract:
        stale = stale or contract.get('binding', {}).get('source_inventory') != field_review.source_inventory(repo, contract.get('migration_exclusions'))
    resolved, active = [], []
    groups = {}
    for gap in observed:
        groups.setdefault(identity(gap), []).append(gap)
    for key, group in groups.items():
        distinct = {digest({k:v for k,v in gap.items() if k not in MUTABLE}) for gap in group}
        for gap in group:
            errors = resolution_errors(gap, contract, repo)
            if stale: errors.append('review source or structure is stale')
            if len(distinct) != 1: errors.append('conflicting observations share a diagnostic identity')
            if errors:
                active.append({**gap, 'blocker_reasons': sorted(set(errors))})
            else:
                resolved.append(gap)
    return {'version':VERSION, 'observed_diagnostics':observed, 'resolved_diagnostics':resolved, 'active_blockers':active}
