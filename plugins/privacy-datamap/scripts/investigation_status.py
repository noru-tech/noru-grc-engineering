"""Read-only investigation progress; cache writes are checkpoints, not proof of completion."""
import datetime
import pathlib


def mode(document, requested=False):
    current = document.get('collection_mode', 'candidate' if document.get('preview') else 'manifest')
    if requested and current != 'candidate':
        raise ValueError('--candidate requires current candidate observations; run collect.mjs --candidate and reconcile.py --candidate first')
    return current


def progress(document, repo, path):
    import field_review
    contract = document.get('classification_review', {})
    rows = list(field_review.all_rows(contract.get('fields', [])))
    partition = field_review.gap_partition(document, field_review.derived_for(document, repo) or {}, repo)
    active = partition['active_blockers']
    path = pathlib.Path(path)
    return {'mode': mode(document), 'note': 'Field counts describe recorded decisions, not validation or approval.', 'fields': {'decided': sum(r.get('status') == 'proposed' for r in rows),
        'pending': sum(r.get('status') != 'proposed' for r in rows), 'total': len(rows)},
        'gaps': {'decided': len(partition['resolved_diagnostics']), 'pending': len(active), 'observed': len(partition['observed_diagnostics'])},
        'last_cache_write': datetime.datetime.fromtimestamp(path.stat().st_mtime, datetime.timezone.utc).isoformat() if path.is_file() else None}


def line(status):
    f, g = status['fields'], status['gaps']
    return (f"Investigation [{status['mode']}]: fields {f['decided']} decided / {f['pending']} pending; "
            f"gaps {g['decided']} decided / {g['pending']} pending; last cache write {status['last_cache_write'] or 'none'}")
