"""Retention validates originals before deletion and protects active/failure records."""
from db2010.diagnostics.artifacts import artifact as artifact_path
from db2010.diagnostics.artifacts import artifact_name, seal, RunArtifacts
from argparse import Namespace
import gzip
import json
from pathlib import Path

import pytest

from db2010.diagnostics.retention import identity, inventory, prepare, read, verify


def run(root,name='old',*,success=True):
    p=root/name;context=RunArtifacts(root, families=['diagnostics','trace'], destination=p)
    result={'machine':'db2010','engine_source_commit':'a'*40,'success':success,
            'execution_limits':{'instruction_limit':1,'seconds':None},'trace_enabled':True,
            'snapshot':{'stop':{'reason':'instruction_limit'}},'evidence':'native '*1000}
    (artifact_path(p, 'result.json', create=True)).write_text(json.dumps(result)+'\n')
    (artifact_path(p, 'experiment.json', create=True)).write_text('{"experiment":"test","machine":"db2010"}\n')
    (artifact_path(p, 'trace.parquet', create=True)).write_bytes(b'authenticated CPU evidence')
    (artifact_path(p, 'memory.parquet', create=True)).write_bytes(b'authenticated bus evidence')
    (artifact_path(p, 'main.bin', create=True)).write_bytes(b'derived original MAIN payload')
    (artifact_path(p, 'execution-proof-0.bin', create=True)).write_bytes(b'required small native-code proof')
    (artifact_path(p, 'uart0.bin', create=True)).write_bytes(b'original firmware log \n')
    context.finish(result)
    return p


def test_lossless_result_and_original_seal_are_preserved(tmp_path):
    p=run(tmp_path);original=(artifact_path(p, 'result.json')).read_bytes();sealed=(p/'artifacts.sha256.json').read_bytes()
    assert prepare(p,tmp_path,{})['status']=='bulk-pruned'
    assert (p/'artifacts.sha256.json').read_bytes()==sealed
    assert gzip.decompress((artifact_path(p, 'result.json.gz')).read_bytes())==original
    assert not (artifact_path(p, 'trace.parquet')).exists() and (artifact_path(p, 'main.bin')).exists()
    assert (artifact_path(p, 'execution-proof-0.bin')).read_bytes()==b'required small native-code proof'
    assert (artifact_path(p, 'uart0.bin')).read_bytes()==b'original firmware log \n'
    assert verify(p)['status']=='bulk-pruned'


@pytest.mark.parametrize('change',['mutated','missing','unsealed'])
def test_invalid_originals_do_not_delete_or_compress(tmp_path,change):
    p=run(tmp_path)
    if change=='mutated':(artifact_path(p, 'uart0.bin', create=True)).write_bytes(b'changed')
    if change=='missing':(artifact_path(p, 'memory.parquet')).unlink()
    if change=='unsealed':(p/'new-file').write_bytes(b'unsealed')
    with pytest.raises(ValueError):prepare(p,tmp_path,{})
    assert (artifact_path(p, 'result.json')).exists() and (artifact_path(p, 'trace.parquet')).exists()
    assert not (artifact_path(p, 'retention.json')).exists() and not (artifact_path(p, 'result.json.gz')).exists()


def test_authenticated_prior_removal_is_integrated(tmp_path):
    p=run(tmp_path);seal=read(p/'artifacts.sha256.json')[artifact_name('trace.parquet')];(artifact_path(p, 'trace.parquet')).unlink()
    prepare(p,tmp_path,{'old/traces/instructions.parquet':seal})
    m=verify(p);assert m['previously_removed']=={artifact_name('trace.parquet'):seal}
    assert m['new_removed_bytes']==len(b'authenticated bus evidence')


def test_compressed_corruption_is_rejected(tmp_path):
    p=run(tmp_path);prepare(p,tmp_path,{})
    (artifact_path(p, 'result.json.gz', create=True)).write_bytes(b'corrupt')
    with pytest.raises(ValueError):verify(p)


def test_interrupted_authenticated_deletion_can_resume(tmp_path):
    p=run(tmp_path);source=(artifact_path(p, 'trace.parquet')).read_bytes();prepare(p,tmp_path,{})
    m=read(artifact_path(p, 'retention.json'));m['status']='prepared';(artifact_path(p, 'retention.json', create=True)).write_text(json.dumps(m))
    (artifact_path(p, 'trace.parquet', create=True)).write_bytes(source)
    prepare(p,tmp_path,{})
    assert verify(p)['status']=='bulk-pruned' and not (artifact_path(p, 'trace.parquet')).exists()


def test_active_prefix_failure_and_legacy_proofs_are_preserved(tmp_path):
    old=run(tmp_path,'old');active=run(tmp_path,'active');new=run(tmp_path,'new-probe');failed=run(tmp_path,'failed',success=False)
    legacy=run(tmp_path,'legacy');r=read(artifact_path(legacy, 'result.json'));del r['engine_source_commit'];(artifact_path(legacy, 'result.json', create=True)).write_text(json.dumps(r))
    (legacy/'run.json').unlink()
    args=Namespace(path=tmp_path,repository=tmp_path,prior_removals=[],keep=['active'],keep_prefix=['new-'],action='prune')
    inventory(args)
    assert (artifact_path(old, 'retention.json')).exists()
    for p in [active,new,failed,legacy]:assert (artifact_path(p, 'trace.parquet')).exists() and not (artifact_path(p, 'retention.json')).exists()
    args.action='verify';inventory(args)


def test_full_verifier_rejects_intentionally_pruned_evidence(tmp_path):
    from db2010.diagnostics.verify import verify as full_verify
    p=run(tmp_path);prepare(p,tmp_path,{})
    with pytest.raises(ValueError,match='bulk-pruned'):full_verify(p)


def test_inventory_does_not_prune_symlinked_run_directories(tmp_path):
    collection=tmp_path/'collection';collection.mkdir()
    run(collection,'active')
    outside=run(tmp_path,'outside')
    (collection/'alias').symlink_to(outside,target_is_directory=True)
    args=Namespace(path=collection,repository=tmp_path,prior_removals=[],
                   keep=['active'],keep_prefix=[],action='prune')
    inventory(args)
    assert artifact_path(outside,'main.bin').exists()
    assert artifact_path(outside,'trace.parquet').exists()
    assert not artifact_path(outside,'retention.json').exists()
    assert any(row['run']=='alias' for row in read(collection/'qemu-retention-index.json')['skipped'])
