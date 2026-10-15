"""Stable, source-bound diagnostic grouping; all occurrences remain reviewable."""
import hashlib
import json
import pathlib
import re

CATALOG = json.loads((pathlib.Path(__file__).resolve().parent.parent/'references/dotnet-diagnostics.json').read_text())['rules']
RESOLUTION = {
    'missing_structure':'Provide current source/schema evidence or extend extraction for the missing persisted structure.',
    'ambiguous_binding':'Resolve the competing symbols, registrations or storage mappings using source evidence.',
    'unsupported_semantics':'Implement and test the unsupported semantics, or provide an evidence-backed structural supplement.',
    'source_membership_uncertainty':'Establish which project, build configuration and source files participate without executing the target build.',
}

RULE_RESOLUTION = {
    'DOTNET_TYPE_MISSING':'Supply the requested type through an evidenced namespace/project reference or current external source/schema.',
    'DOTNET_TYPE_AMBIGUOUS':'Disambiguate the listed type definitions through namespace qualification and project membership.',
    'DOTNET_EXTERNAL_VERSION':'Provide current source/schema for the inherited type or an exact package version covered by a pinned framework contract.',
    'DOTNET_EXTENSION_BINDING':'Establish one invoked source extension with a supported ModelBuilder signature; provide an adapter for other signatures.',
    'DOTNET_EXTENSION_CYCLE':'Resolve the cyclic registration path or supply an evidence-backed finite model definition.',
    'DOTNET_RUNTIME_BRANCH':'Establish branch-specific runtime connection bindings; do not treat a conditional registration as an unconditional datastore.',
    'DOTNET_BUILD_MEMBERSHIP':'Establish the applicable build conditions and included source/project references without executing target builds.',
}


def finalize(reader):
    groups = {}
    for gap in reader.gaps:
        path, raw_line = gap['ref'].rsplit(':',1); line = int(raw_line)
        rule = CATALOG.get(gap['reason'], {})
        code = gap.get('code') or rule.get('code') or 'DOTNET_SOURCE_SYNTAX'
        category = ({'DOTNET_TYPE_MISSING':'missing_structure','DOTNET_TYPE_AMBIGUOUS':'ambiguous_binding','DOTNET_BUILD_MEMBERSHIP':'source_membership_uncertainty'}.get(code)) or gap.get('category') or rule.get('category') or ('source_membership_uncertainty' if code=='DOTNET_BUILD_MEMBERSHIP' else 'missing_structure' if code=='DOTNET_EXTERNAL_VERSION' else 'ambiguous_binding' if code=='DOTNET_RUNTIME_BRANCH' else None) or ('ambiguous_binding' if code.endswith(('BINDING','CYCLE','PRECEDENCE','AMBIGUOUS','ENTRYPOINT')) else 'unsupported_semantics')
        text = reader.sources.get(path, {}).get('text')
        if text is None:
            full = reader.repo/path
            try: text = full.read_text(encoding='utf-8-sig') if not full.is_symlink() and full.resolve().is_relative_to(reader.repo) and full.stat().st_size <= 1_000_000 else ''
            except (OSError,UnicodeError): text = ''
        lines = text.splitlines(); excerpt = lines[line-1] if 0 < line <= len(lines) else ''
        models = [m for m in reader.models if m['path']==path and m['line']<=line<=max([m['line']]+[t.line for t in m['body']])]
        symbol = max(models,key=lambda m:m['line'])['qualified'] if models else None
        projects = [p for p in reader.projects if pathlib.PurePosixPath(path).is_relative_to(pathlib.PurePosixPath(p).parent)]
        project = max(projects,key=len) if projects else None
        scope = gap.get('_scope') or {'kind':'type' if symbol else 'source','path':path,'symbol':symbol,'project':project}
        stable = json.dumps([code,scope],sort_keys=True)
        diagnostic_id = 'dotnet:'+hashlib.sha256(stable.encode()).hexdigest()[:24]
        evidence = {'path':path,'start_line':line,'end_line':line,'start_column':gap.get('_column',len(excerpt)-len(excerpt.lstrip())+1),
                    'end_column':len(excerpt)+1,'sha256':hashlib.sha256(text.encode('utf-8')).hexdigest(), 'extractor':'dotnet-structural@2'}
        # Source hashes use original bytes, including BOM, like the canonical evidence gate.
        try:
            full = reader.repo/path
            if not full.is_symlink() and full.resolve().is_relative_to(reader.repo) and full.stat().st_size <= 1_000_000:
                evidence['sha256'] = hashlib.sha256(full.read_bytes()).hexdigest()
        except OSError: pass
        record = groups.setdefault(diagnostic_id, {**{k:v for k,v in gap.items() if not k.startswith('_')},'code':code,'diagnostic_id':diagnostic_id,'category':category,'scope':scope,
                                                   'resolution_requirement':RULE_RESOLUTION.get(code,RESOLUTION[category]), 'evidence':[],'occurrences':[]})
        occurrence = {'reason':gap['reason'],'ref':gap['ref'],'evidence':evidence}
        if gap.get('details'): occurrence['details'] = gap['details']
        if occurrence not in record['occurrences']: record['occurrences'].append(occurrence)
        if evidence not in record['evidence']: record['evidence'].append(evidence)
    diagnostics = sorted(groups.values(),key=lambda d:(d['scope']['path'],d['code'],d['diagnostic_id']))
    for diagnostic in diagnostics:
        diagnostic['occurrences'].sort(key=lambda o:(o['ref'],o['reason']))
        diagnostic['evidence'].sort(key=lambda e:(e['path'],e['start_line'],e['start_column']))
    return diagnostics


def summary(gaps):
    return {'diagnostics':len(gaps),'occurrences':sum(len(g.get('occurrences',[g])) for g in gaps),
            'by_category':{category:sum(g.get('category')==category for g in gaps) for category in RESOLUTION},
            'diagnostic_ids':[g['diagnostic_id'] for g in gaps]}
