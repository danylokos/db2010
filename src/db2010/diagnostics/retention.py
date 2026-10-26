"""Prune authenticated superseded QEMU captures; preserve summaries and original seals."""
import argparse
from datetime import datetime, timezone
import gzip
import hashlib
import json
import os
from pathlib import Path
import shlex
import sys

from ..paths import ROOT
from .artifacts import artifact, artifact_name, files, relative_artifact, read_json, load_run

BULK={artifact_name(name) for name in ('trace.parquet','memory.parquet')}
COMPRESS={artifact_name(name) for name in ('result.json','ram_execution_evidence.json')}


def read(path):return json.loads(path.read_text())


def identity(path):
    with path.open('rb') as stream:digest=hashlib.file_digest(stream,'sha256').hexdigest()
    return {'size_bytes':path.stat().st_size,'sha256':digest}


def expanded_identity(path):
    digest=hashlib.sha256();size=0
    with gzip.open(path,'rb') as stream:
        while chunk:=stream.read(1024*1024):digest.update(chunk);size+=len(chunk)
    return {'size_bytes':size,'sha256':digest.hexdigest()}


def dump(path,value):
    temporary=path.with_name(path.name+'.tmp')
    temporary.write_text(json.dumps(value,indent=2)+'\n');os.replace(temporary,path)


def regeneration_command(path):
    path=Path(path).resolve()
    manifest=load_run(path)
    limits=manifest['limits']
    if manifest.get('starting_checkpoint'):
        command=[sys.executable,'-B','-m','db2010.execution.regenerate',str(path)]
    else:
        command=[sys.executable,'-B','-m','db2010','--experiment',str(artifact(path,'experiment.json')),
                 '--artifacts-dir',str(path.parent),'--label',path.name+'-regenerated']
    if manifest.get('emulator_root'):
        command.extend(['--repo', manifest['emulator_root']])
    if not manifest.get('starting_checkpoint') and manifest.get('input_root'):
        command.extend(['--input-root', manifest['input_root']])
    for family, flag in ([] if manifest.get('starting_checkpoint') else [('diagnostics','--diagnostics'),('trace','--trace'),('snapshot','--snapshot'),('audio','--record-audio'),('flash','--save-flash')]):
        if family in manifest['families']: command.append(flag)
    if not manifest.get('starting_checkpoint') and manifest.get('trace',{}).get('collector',{}).get('disassembly'):
        command.append('--disassemble')
    if manifest.get('starting_checkpoint'): return shlex.join(command)
    if manifest.get('research_guards', True): command.append('--research-guards')
    if manifest.get('uart_debug'):
        command.append('--uart-debug')
    for key,flag in [('instruction_limit','--limit'),('seconds','--seconds')]:
        if limits.get(key) is not None:command.extend([flag,str(limits[key])])
    # Current runs retain authenticated absolute engine image references.
    state_path = artifact(path, "state.json")
    if state_path.exists() and "external_inputs" in manifest:
        state = read(state_path)
        from .artifacts import verify_external_inputs
        verify_external_inputs(manifest["external_inputs"])
        names = {0x44020000: "main", 0x45400000: "fs", 0x45F00000: "gdfs"}
        for seed in state["regions"][0]["seeds"]:
            if seed["address"] in names:
                command.extend(["--"+names[seed["address"]], seed["file"]])
        for name in ('nand_main', 'nand_spare'):
            if state.get('nand', {}).get(name+'_image'):
                command.extend(['--'+name.replace('_', '-'), state['nand'][name+'_image']])
        return shlex.join(command)
    # Historical runs use contained frozen images.
    for name, filename in [('main','main.bin'),('fs','fs.bin'),('gdfs','nor-gdfs.bin')]:
        source=artifact(path,filename)
        if source.is_file(): command.extend(['--'+name,str(source)])
    return shlex.join(command)


def validate_names(path,seals):
    load_run(path)
    for name,value in seals.items():
        relative_artifact(path,name)
        if (name in ('artifacts.sha256.json',artifact_name('retention.json'))
                or not isinstance(value,dict) or set(value)!={'size_bytes','sha256'}):
            raise ValueError('unsupported original artifact inventory')


def verify(path, *, finish=False):
    path=Path(path).resolve()
    metadata=read(artifact(path,'retention.json'));seals=read(artifact(path,'artifacts.sha256.json'));validate_names(path,seals)
    if identity(artifact(path,'artifacts.sha256.json'))!=metadata['original_inventory']:raise ValueError('original inventory changed')
    removed=metadata['removed'];compressed=metadata['compressed']
    if not set(removed)<=BULK or not set(compressed)<=COMPRESS or not (set(removed)|set(compressed))<=set(seals):raise ValueError('unexpected retention policy')
    for name,value in removed.items():
        if value!=seals[name]:raise ValueError('removed identity differs from original seal')
    retained=set(seals)-set(removed)-set(compressed)
    for name in retained:
        if identity(relative_artifact(path,name))!=seals[name]:raise ValueError(f'retained artifact differs: {name}')
    for name,value in compressed.items():
        target=relative_artifact(path,value['file'])
        if value['file']!=name+'.gz' or identity(target)!=value['stored'] or expanded_identity(target)!=seals[name]:
            raise ValueError(f'compressed artifact differs: {name}')
    expected=retained|{v['file'] for v in compressed.values()}|{artifact_name('retention.json'),'artifacts.sha256.json'}
    actual=files(path)|{'artifacts.sha256.json'}
    if finish:
        if not actual<=expected|set(removed)|set(compressed) or not expected<=actual:raise ValueError('retained inventory differs')
        # Resume only an authenticated interrupted prune; check remaining originals before removal.
        for name in set(removed)|set(compressed):
            original=relative_artifact(path,name)
            if original.exists() and identity(original)!=seals[name]:raise ValueError(f'remaining original differs: {name}')
        for name in set(removed)|set(compressed):(relative_artifact(path,name)).unlink(missing_ok=True)
        metadata['status']='bulk-pruned';metadata['completed_utc']=datetime.now(timezone.utc).isoformat()
        dump(artifact(path,'retention.json'),metadata)
    else:
        if metadata['status']!='bulk-pruned' or actual!=expected:raise ValueError('retained inventory differs or prune incomplete')
    return metadata


def prepare(path, repository, prior):
    path=Path(path).resolve();repository=Path(repository).resolve()
    if (artifact(path,'retention.json')).exists():
        if read(artifact(path,'retention.json'))['status']=='bulk-pruned':return verify(path)
        return verify(path,finish=True)
    seals=read(artifact(path,'artifacts.sha256.json'));validate_names(path,seals)
    actual=files(path)
    missing=set(seals)-actual
    if actual-set(seals):raise ValueError('unsealed extra artifacts')
    previous={}
    for name in missing:
        record=prior.get(str((relative_artifact(path,name)).relative_to(repository)))
        if name not in BULK or record!=seals[name]:raise ValueError(f'missing original without authenticated prior removal: {name}')
        previous[name]=record
    for name in actual:
        if identity(relative_artifact(path,name))!=seals[name]:raise ValueError(f'original artifact differs: {name}')
    # Shared inputs are always needed for relocation/replay, and may also back a
    # checkpoint. Only optional trace ledgers can be removed in layout v2.
    removed={name:seals[name] for name in sorted((actual&BULK)|missing)}
    compressed={}
    for name in sorted(actual&COMPRESS):
        source=relative_artifact(path,name);target=relative_artifact(path,name+'.gz');temporary=relative_artifact(path,name+'.gz.tmp')
        if target.exists():raise ValueError('compressed destination already exists')
        try:
            with source.open('rb') as stream,temporary.open('xb') as output:
                with gzip.GzipFile(filename='',mode='wb',fileobj=output,mtime=0,compresslevel=6) as zipped:
                    while chunk:=stream.read(1024*1024):zipped.write(chunk)
            if expanded_identity(temporary)!=seals[name]:raise ValueError('lossless compression check failed')
            if temporary.stat().st_size>=source.stat().st_size:
                continue
            os.replace(temporary,target)
        finally:temporary.unlink(missing_ok=True)
        compressed[name]={'file':target.relative_to(path).as_posix(),'original':seals[name],'stored':identity(target)}
    metadata={'format_version':1,'status':'prepared','created_utc':datetime.now(timezone.utc).isoformat(),
              'original_inventory':identity(artifact(path,'artifacts.sha256.json')),'removed':removed,
              'previously_removed':previous,'compressed':compressed,
              'new_removed_bytes':sum(v['size_bytes'] for n,v in removed.items() if n not in previous),
              'compression_saved_bytes':sum(v['original']['size_bytes']-v['stored']['size_bytes'] for v in compressed.values()),
              'regenerate_command':regeneration_command(path),
              'reproduction_note':'Use frozen runner-sources.zip, engine source lock/bundle, build/dependency identities and original inputs for historical behavior. Regenerate into a new directory; current models may differ. Retained integrity does not perform missing CPU/bus replay.'}
    dump(artifact(path,'retention.json'),metadata);verify(path,finish=True)
    return verify(path)


def inventory(args):
    root=args.path.resolve();repository=args.repository.resolve();prior={};prior_files={}
    for file in args.prior_removals:
        prior_files[str(file.resolve().relative_to(repository))]=identity(file)
        for value in read(file)['removed']:
            record={k:value[k] for k in ('size_bytes','sha256')}
            if value['path'] in prior and prior[value['path']]!=record:raise ValueError('conflicting prior removals')
            prior[value['path']]=record
    keeps=set(args.keep);rows=[];skipped=[]
    for path in sorted(p for p in root.iterdir() if p.is_dir()):
        if path.is_symlink():
            skipped.append({'run':path.name,'reason':'symlinked run directory preserved'});continue
        if path.name in keeps or any(path.name.startswith(p) for p in args.keep_prefix):
            skipped.append({'run':path.name,'reason':'explicitly preserved'});continue
        try:
            manifest=load_run(path)
        except ValueError:
            skipped.append({'run':path.name,'reason':'unsupported layout; use frozen tooling'});continue
        if (artifact(path,'retention.json')).exists():
            metadata=read(artifact(path,'retention.json'))
            if metadata.get('original_inventory'):
                if args.action=='prune':prepare(path,repository,prior)
                elif args.action=='verify':verify(path)
                rows.append({'run':path.name,'retention_sha256':identity(artifact(path,'retention.json'))['sha256'],
                             'new_removed_bytes':metadata['new_removed_bytes'],'compression_saved_bytes':metadata['compression_saved_bytes']})
            continue
        result=artifact(path,'result.json')
        if not (artifact(path,'artifacts.sha256.json')).exists():continue
        value=manifest.get('outcome', {})
        if not value.get('success') or not value.get('stop'):
            skipped.append({'run':path.name,'reason':'failed/incomplete capture preserved'});continue
        if args.action=='verify':continue
        seals=read(artifact(path,'artifacts.sha256.json'))
        new=sum(relative_artifact(path,n).stat().st_size for n in BULK if relative_artifact(path,n).exists())
        if args.action=='plan':
            print(json.dumps({'run':path.name,'bulk_bytes':new,'result_bytes':result.stat().st_size if result.exists() else 0}),flush=True);continue
        try:metadata=prepare(path,repository,prior)
        except ValueError as error:
            skipped.append({'run':path.name,'reason':str(error)});print(json.dumps(skipped[-1]),flush=True);continue
        rows.append({'run':path.name,'retention_sha256':identity(artifact(path,'retention.json'))['sha256'],
                     'new_removed_bytes':metadata['new_removed_bytes'],'compression_saved_bytes':metadata['compression_saved_bytes']})
        print(json.dumps({'run':path.name,'reclaimed_bytes':metadata['new_removed_bytes']+metadata['compression_saved_bytes']}),flush=True)
    if args.action in ('prune','verify'):
        index={'format_version':1,'runs':rows,'preserved_runs':sorted(keeps),'preserved_prefixes':args.keep_prefix,
               'skipped':skipped,'prior_removal_manifests':prior_files,
               'new_reclaimed_bytes':sum(r['new_removed_bytes']+r['compression_saved_bytes'] for r in rows),
               'scope':'successful superseded Python/QEMU runs only; original inventories/resources and failed/incomplete captures preserved'}
        destination=root/'qemu-retention-index.json'
        if args.action=='prune':dump(destination,index)
        else:
            old=read(destination)
            if {(r['run'],r['retention_sha256']) for r in rows}!={(r['run'],r['retention_sha256']) for r in old['runs']}:
                raise ValueError('retention index/run set differs')
        print(json.dumps({'verified_runs':len(rows),'new_reclaimed_bytes':index['new_reclaimed_bytes'],'skipped_runs':len(skipped)}),flush=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('plan','prune','verify'));parser.add_argument('path',type=Path)
    parser.add_argument('--repository',type=Path,default=ROOT)
    for key in ['keep','keep-prefix']:parser.add_argument('--'+key,action='append',default=[])
    parser.add_argument('--prior-removals',type=Path,action='append',default=[])
    args=parser.parse_args()
    if args.action=='prune' and not args.keep:parser.error('prune requires --keep for the current full baseline')
    for name in args.keep:
        if name in ('.','..') or Path(name).name!=name or not (args.path/name).is_dir():parser.error('--keep must name an existing run')
    inventory(args)


if __name__ == "__main__":
    main()
