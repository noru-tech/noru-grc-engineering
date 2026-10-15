#!/usr/bin/env python3
"""Compare toolkit extraction versions on checked-out C# source; never run target code."""
import argparse
import hashlib
import io
import json
import pathlib
import subprocess
import tarfile
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
SUFFIXES = {'.cs','.csproj','.props','.targets','.sln','.slnx'}
SKIP = {'.git','.noru','bin','obj','node_modules','vendor','packages','migrations','test','tests'}


def inventory(repo):
    listed = subprocess.check_output(['git','-C',str(repo),'ls-files','--cached','--others','--exclude-standard','-z']).decode().split('\0')
    files=[]; excluded=[]; source_hash=hashlib.sha256()
    for name in sorted(set(listed)):
        path=pathlib.PurePosixPath(name)
        if not name or path.suffix not in SUFFIXES: continue
        if any(part.lower() in SKIP or part.lower().endswith(('.tests','.unittests','.functionaltests')) for part in path.parts): excluded.append(name); continue
        full=repo/name
        if full.is_symlink() or not full.is_file(): excluded.append(name); continue
        files.append(name);source_hash.update(name.encode()+b'\0'+hashlib.sha256(full.read_bytes()).digest())
    return files,excluded,source_hash.hexdigest()


def extract(script, repo, files, excluded):
    result=subprocess.run(['python3',str(script),str(repo)],input=json.dumps({'files':files,'excluded':excluded}),text=True,capture_output=True,timeout=180)
    if result.returncode: raise RuntimeError(result.stderr)
    data=json.loads(result.stdout)
    return {'projects':len(data['projects']),'relationships':len(data['relationships']),
            'fields':sum(len(t['fields']) for t in data['tables']),'logical_datastores':len({r['dataset'] for r in data['relationships']}),
            'diagnostics':data.get('diagnostic_summary',{'diagnostics':len(data['gaps']),'occurrences':len(data['gaps'])}),
            'gaps':data['gaps'],'relationships_evidence':data['relationships']}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo',type=pathlib.Path,action='append',required=True)
    parser.add_argument('--baseline-ref',required=True)
    parser.add_argument('--output',type=pathlib.Path,required=True)
    args=parser.parse_args()
    baseline_sha=subprocess.check_output(['git','-C',str(ROOT),'rev-parse',args.baseline_ref+'^{commit}'],text=True).strip()
    archive=subprocess.check_output(['git','-C',str(ROOT),'archive',baseline_sha,'plugins/privacy-datamap/scripts','plugins/privacy-datamap/references'])
    extractor_files=sorted((ROOT/'plugins/privacy-datamap/scripts').glob('dotnet*.py'))+sorted((ROOT/'plugins/privacy-datamap/references').glob('dotnet*.json'))
    extractor_digest=hashlib.sha256(b''.join(p.name.encode()+b'\0'+p.read_bytes() for p in extractor_files)).hexdigest()
    report={'extractor_source_digest':extractor_digest,'baseline_commit':baseline_sha,'comparison':'checked-out working source; no builds, migrations or generators executed','repositories':[]}
    with tempfile.TemporaryDirectory(prefix='noru-dotnet-baseline-') as temporary:
        with tarfile.open(fileobj=io.BytesIO(archive)) as bundle: bundle.extractall(temporary,filter='data')
        for repo in args.repo:
            repo=repo.resolve();files,excluded,digest=inventory(repo)
            row={'repository':str(repo),'commit':subprocess.check_output(['git','-C',str(repo),'rev-parse','HEAD'],text=True).strip(),
                 'source_digest':digest,'source_files':len(files),'excluded_or_unavailable':len(excluded)}
            row['before']=extract(pathlib.Path(temporary)/'plugins/privacy-datamap/scripts/dotnet.py',repo,files,excluded)
            row['after']=extract(ROOT/'plugins/privacy-datamap/scripts/dotnet.py',repo,files,excluded)
            repeated=extract(ROOT/'plugins/privacy-datamap/scripts/dotnet.py',repo,files,excluded)
            row['deterministic']=row['after']==repeated
            row['source_unchanged']=inventory(repo)[2]==digest
            if not row['source_unchanged']: raise RuntimeError('Target source changed during the comparison')
            if not row['deterministic']: raise RuntimeError('Extraction changed between identical source runs')
            report['repositories'].append(row)
            print(json.dumps({k:row[k] for k in ('repository','source_files','deterministic')}),flush=True)
    args.output.write_text(json.dumps(report,indent=2)+'\n')


if __name__=='__main__':main()
