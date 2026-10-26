"""Host checkpoint publication, portability, source isolation and validation."""
from support.guarded import run as guarded_run
import sys
from support.paths import RESOURCES
import json
from pathlib import Path
import shutil
import struct

import pytest

from db2010.execution import runner
from db2010.execution.checkpoint import load_checkpoint
from db2010.diagnostics.artifacts import artifact, files, identity, seal, write_json
from db2010.paths import ROOT, DEFAULT_EXPERIMENT
from db2010.diagnostics.retention import prepare as prune
from test_runner import synthetic_preparation

BINARY=ROOT/'qemu/build/qemu-system-arm'


from support.controlled_clock import controlled_clock

pytestmark = pytest.mark.usefixtures('controlled_clock')

def preparation(root, experiment, output, **options):
    path,spec=synthetic_preparation(root,experiment,output)
    state=json.loads(path.read_text())
    # UART, then RAM-only arithmetic, then repeat. All CPU work is native.
    artifact(output,'code.bin').write_bytes(struct.pack('<3I',0xe1c100b2,0xe2800001,0xeafffffc))
    state['registers'][:2]=[65,0xfb060000]
    state['executable_ranges'][0]['end']=0x100c
    state['uart_transmit']={'profile':'polled-tx-immediate-drain','free_slots':1}
    path.write_text(json.dumps(state))
    return path,spec


def saved(tmp_path, monkeypatch, **options):
    monkeypatch.setattr(runner,'prepare',preparation)
    result,error=guarded_run(ROOT,Path('unused'),tmp_path/'saved',snapshot=True,
        instruction_limit=4,config_limits=False,**options)
    assert error is None,result.get('error')
    assert result['checkpoint']=='snapshots/final/snapshot.json'
    return tmp_path/'saved',result


def test_full_resume_uart_baselines_and_external_parent(tmp_path,monkeypatch):
    path,first=saved(tmp_path,monkeypatch,trace=True)
    moved=tmp_path/'moved run';shutil.move(path,moved)
    prune(moved,tmp_path,{})
    original={name:identity(moved/name) for name in files(moved)}
    load_checkpoint(moved,BINARY)
    resumed,error=guarded_run(ROOT,Path('unused'),tmp_path/'resumed',restore=moved,
        diagnostics=True,trace=True,snapshot=True,instruction_limit=4,config_limits=False)
    assert error is None,resumed.get('error')
    assert resumed['snapshot']['observations']==8
    assert resumed['capture_baseline']['uart_bytes']==[2,0,0,0]
    assert artifact(tmp_path/'resumed','uart0.bin').read_bytes()==b'C'
    assert resumed['uart']['0']['size_bytes']==1
    whole,error=guarded_run(ROOT,Path('unused'),diagnostics=True,label='whole',
        artifacts_dir=tmp_path,instruction_limit=8,config_limits=False)
    assert error is None
    assert resumed['snapshot']==whole['snapshot']
    assert {name:identity(moved/name) for name in files(moved)}==original
    manifest=json.loads((tmp_path/'resumed/run.json').read_text())
    assert manifest['lineage']['mode']=='restore'
    assert manifest['lineage']['source_id']=='saved'
    assert manifest['starting_checkpoint']=={'path':str(moved/'snapshots/final/snapshot.json'),
        **identity(moved/'snapshots/final/snapshot.json')}
    assert not (tmp_path/'resumed/inputs/start').exists()
    assert set(p.name for p in (tmp_path/'resumed/snapshots/final').iterdir())=={'snapshot.json','state.vmstate'}
    # Replay requires the referenced parent; final restore does not.
    from db2010.diagnostics.retention import regeneration_command
    import shlex,subprocess
    continued=tmp_path/'resumed'
    process=subprocess.run(shlex.split(regeneration_command(continued)),cwd=ROOT,
        capture_output=True,text=True,timeout=30)
    assert process.returncode==0,process.stderr
    from db2010.diagnostics.artifacts import read_json
    assert read_json(tmp_path/'resumed-regenerated')['snapshot']==resumed['snapshot']
    shutil.rmtree(moved)
    load_checkpoint(continued,BINARY)
    result,error=guarded_run(ROOT,None,replay=continued)
    assert 'missing starting checkpoint' in str(error)
    result,error=guarded_run(ROOT,None,restore=continued,instruction_limit=1,config_limits=False)
    assert error is None


@pytest.mark.parametrize('change', ['schema','build','vmstate','missing','model','manifest'])
@pytest.mark.parametrize('allow_engine_mismatch', [False, True])
def test_invalid_sources_are_rejected_before_allocating_run(tmp_path,monkeypatch,change,allow_engine_mismatch):
    path,_=saved(tmp_path,monkeypatch)
    checkpoint=path/'snapshots/final/snapshot.json'
    metadata=json.loads(checkpoint.read_text())
    if change=='schema':metadata['schema_version']=999
    elif change=='build':metadata['engine']['sha256']='0'*64
    elif change=='vmstate':(path/metadata['vmstate']).write_bytes(b'corrupt')
    elif change=='missing':(path/metadata['vmstate']).unlink()
    elif change=='model':metadata['model_identity']='0'*64
    elif change=='manifest':(path/'run.json').write_text('{broken')
    checkpoint.write_text(json.dumps(metadata))
    if change in ('schema','build','model'):seal(path)
    _,error=guarded_run(ROOT,Path('unused'),artifacts_dir=tmp_path,label='rejected',
        restore=path,snapshot=True,instruction_limit=1,config_limits=False,
        allow_engine_mismatch=allow_engine_mismatch)
    assert error is not None
    assert not (tmp_path/'rejected').exists()


def test_engine_override_keeps_provenance_and_saves_current_engine(tmp_path,monkeypatch):
    path,_=saved(tmp_path,monkeypatch)
    # Controlled metadata fixture for a different source build; native VMState
    # stays valid. The real old-build firmware check is retained separately.
    cp=path/'snapshots/final/snapshot.json';spec=json.loads(cp.read_text())
    old_engine={**spec['engine'],'sha256':'1'*64}
    spec['engine']=old_engine;write_json(cp,spec)
    manifest=json.loads((path/'run.json').read_text());manifest['engine']=old_engine
    write_json(path/'run.json',manifest);seal(path)
    before={name:identity(path/name) for name in files(path)}
    _,error=guarded_run(ROOT,Path('unused'),tmp_path/'strict',snapshot=True,
        instruction_limit=4,config_limits=False,restore=path)
    assert '--allow-engine-mismatch' in str(error) and not (tmp_path/'strict').exists()
    run=tmp_path/'override'
    result,error=guarded_run(ROOT,Path('unused'),run,snapshot=True,
        instruction_limit=4,config_limits=False,allow_engine_mismatch=True,restore=path)
    assert error is None,result
    assert result['snapshot']['observations']==8
    manifest=json.loads((run/'run.json').read_text())
    assert manifest['lineage']['source_engine']==old_engine
    assert manifest['checkpoint_compatibility']==result['checkpoint_compatibility']=={
        'allow_engine_mismatch':True,'source_engine':old_engine,
        'installed_engine':identity(BINARY),'engine_mismatch':True}
    assert load_checkpoint(run,BINARY)['engine']==identity(BINARY)
    assert {name:identity(path/name) for name in files(path)}==before
    # Replay uses the older *starting* engine identity, not the new final engine.
    from db2010.diagnostics.retention import regeneration_command
    import shlex,subprocess
    moved=tmp_path/'moved override';shutil.move(run,moved)
    assert load_checkpoint(moved,BINARY,starting=True,allow_engine_mismatch=True)['engine']==old_engine
    with pytest.raises(ValueError,match='engine differs'):
        load_checkpoint(moved,BINARY,starting=True)
    process=subprocess.run(shlex.split(regeneration_command(moved)),cwd=ROOT,
        capture_output=True,text=True,timeout=30)
    assert process.returncode==0,process.stderr
    regenerated=json.loads((tmp_path/'moved override-regenerated/run.json').read_text())
    assert regenerated['outcome']==manifest['outcome']
    assert regenerated['checkpoint_compatibility']==manifest['checkpoint_compatibility']


def test_engine_override_requires_checkpoint_start(tmp_path):
    _,error=guarded_run(ROOT,Path('unused'),tmp_path/'invalid',snapshot=True,allow_engine_mismatch=True)
    assert 'requires restore' in str(error) and not (tmp_path/'invalid').exists()


def test_engine_override_keeps_native_checkpoint_version_check(tmp_path,monkeypatch):
    path,_=saved(tmp_path,monkeypatch)
    cp=path/'snapshots/final/snapshot.json';spec=json.loads(cp.read_text())
    vmstate=path/spec['vmstate'];raw=vmstate.read_bytes()
    marker=struct.pack('>II',0x44424350,24)
    assert raw.count(marker)==1
    vmstate.write_bytes(raw.replace(marker,struct.pack('>II',0x44424350,999)))
    # Retain valid host schemas and checksums so the native version check itself
    # must reject this incompatible state, even with the engine override.
    spec['files'][spec['vmstate']]=identity(vmstate)
    write_json(cp,spec);seal(path)
    result,error=guarded_run(ROOT,Path('unused'),tmp_path/'incompatible',restore=path,
        snapshot=True,instruction_limit=1,config_limits=False,allow_engine_mismatch=True)
    assert error is not None
    assert 'invalid DB2010 checkpoint state' in result['engine_error_context']
    assert not (tmp_path/'incompatible/snapshots/final').exists()


@pytest.mark.parametrize('start', ['--restore',None])
def test_cli_engine_override_is_explicit(monkeypatch,start):
    from db2010 import cli
    calls=[]
    def run(*args,**options):
        calls.append(options)
        return {'success':True,'phases':{},'uart':{}},None
    monkeypatch.setattr(cli,'run',run)
    monkeypatch.setattr('sys.argv',['db2010', '--research-guards','--allow-engine-mismatch']+([start,'source'] if start else []))
    if start:
        cli.main()
        assert calls[0]['allow_engine_mismatch'] is True
    else:
        with pytest.raises(SystemExit) as error:cli.main()
        assert error.value.code==2 and not calls


@pytest.mark.parametrize('stage', ['migration', 'publication'])
def test_interrupted_checkpoint_is_not_published(tmp_path,monkeypatch,stage):
    import db2010.execution.checkpoint as checkpoint
    monkeypatch.setattr(runner,'prepare',preparation)
    def failed(*args,**options):raise OSError('controlled failed VM state write')
    if stage=='migration': monkeypatch.setattr(checkpoint,'migrate_file',failed)
    else: monkeypatch.setattr(checkpoint.os,'rename',failed)
    result,error=guarded_run(ROOT,Path('unused'),tmp_path/'failed',snapshot=True,
        instruction_limit=4,config_limits=False)
    assert error is not None and 'controlled failed' in str(error)
    assert not (tmp_path/'failed/snapshots/final').exists()
    assert not list((tmp_path/'failed/snapshots').iterdir())
    assert json.loads((tmp_path/'failed/run.json').read_text())['status']=='failed'
    assert 'checkpoint' not in result


@pytest.mark.parametrize('stop', ['interrupt','timeout','guard','debugger','debugger_error'])
def test_every_orderly_stop_saves_exactly_one_checkpoint(tmp_path,monkeypatch,stop):
    from db2010.execution.debug import DebugOptions
    monkeypatch.setattr(runner,'prepare',preparation)
    options={}
    if stop in ('interrupt','timeout'):
        def stopped(*args,**kw):
            if stop=='interrupt':raise KeyboardInterrupt()
            raise TimeoutError('controlled wall limit')
        monkeypatch.setattr(runner.QMP,'wait_stop',stopped)
    elif stop=='guard':
        def guarded(root,experiment,output,**kw):
            path,spec=preparation(root,experiment,output)
            artifact(output,'code.bin').write_bytes(struct.pack('<I',0xe5930000))
            return path,spec
        monkeypatch.setattr(runner,'prepare',guarded)
    else:
        options['debug']=DebugOptions('gdb-multiarch',False,[('command',
            'not-a-valid-gdb-command' if stop=='debugger_error' else 'info registers')])
    result,error=guarded_run(ROOT,Path('unused'),tmp_path/'saved',snapshot=True,
        instruction_limit=4,config_limits=False,**options)
    assert (tmp_path/'saved/snapshots/final/snapshot.json').is_file(),result
    assert len(list((tmp_path/'saved/snapshots').iterdir()))==1
    assert not (tmp_path/'saved/diagnostics').exists()
    if stop=='guard':
        previous=result['snapshot']['stop']
        result,error=guarded_run(ROOT,Path('unused'),restore=tmp_path/'saved',
            instruction_limit=10,config_limits=False)
        assert error is None,result.get('error')
        assert result['snapshot']['stop']==previous
        assert result['session_end']=='restored_guarded_boundary'


def test_diagnostics_do_not_enter_the_native_execution_checkpoint(tmp_path,monkeypatch):
    from support.checkpoint_audit import execution_stream
    from db2010.diagnostics.artifacts import load_run
    monkeypatch.setattr(runner,'prepare',preparation)
    for name,diagnostics in [('compact',False),('diagnostic',True)]:
        result,error=guarded_run(ROOT,Path('unused'),tmp_path/name,diagnostics=diagnostics,
            snapshot=True,instruction_limit=4,config_limits=False)
        assert error is None,result.get('error')
    assert execution_stream(tmp_path/'compact/snapshots/final/state.vmstate')==execution_stream(tmp_path/'diagnostic/snapshots/final/state.vmstate')


@pytest.mark.parametrize('allow_engine_mismatch', [False, True])
def test_resealed_changed_input_is_rejected_by_native_model_identity(tmp_path,monkeypatch,allow_engine_mismatch):
    from db2010.execution.checkpoint import model_identity
    path,_=saved(tmp_path,monkeypatch)
    code=artifact(path,'code.bin');raw=bytearray(code.read_bytes());raw[4]^=1;code.write_bytes(raw)
    state=json.loads(artifact(path,'state.json').read_text())
    checkpoint=path/'snapshots/final/snapshot.json';metadata=json.loads(checkpoint.read_text())
    metadata['model_identity']=model_identity(state,path)
    metadata['required_files']['inputs/code.bin']=identity(code)
    write_json(checkpoint,metadata);seal(path)
    result,error=guarded_run(ROOT,Path('unused'),tmp_path/'rejected',restore=path,snapshot=True,
        instruction_limit=1,config_limits=False,allow_engine_mismatch=allow_engine_mismatch)
    assert error is not None
    assert 'invalid DB2010 checkpoint state' in result['engine_error_context']
    assert not (tmp_path/'rejected/snapshots/final').exists()


def test_finished_browser_interrupt_does_not_save_again(tmp_path):
    import signal,subprocess,time
    destination=tmp_path/'browser'
    process=subprocess.Popen([sys.executable,'-B','-m','db2010', '--research-guards',
        '--snapshot','--artifacts-dir',str(tmp_path),'--label','browser',
        '--experiment',str(DEFAULT_EXPERIMENT),
        '--limit','1','--ui','0','--json-summary'],cwd=ROOT,
        stdout=subprocess.PIPE,stderr=subprocess.PIPE,start_new_session=True)
    try:
        deadline=time.monotonic()+30
        while not (destination/'artifacts.sha256.json').exists():
            assert process.poll() is None,process.communicate()[1]
            assert time.monotonic()<deadline
            time.sleep(.02)
        before=identity(destination/'snapshots/final/state.vmstate')
        seal_before=(destination/'artifacts.sha256.json').read_bytes()
        # CLI prints the summary after finalization, then enters the browser wait.
        import select
        assert select.select([process.stderr],[],[],10)[0]
        while True:
            line=process.stderr.readline()
            if line.strip()==b'}':break
            assert line
        process.send_signal(signal.SIGINT)
        _,stderr=process.communicate(timeout=15)
        assert process.returncode==0,stderr
        assert identity(destination/'snapshots/final/state.vmstate')==before
        assert (destination/'artifacts.sha256.json').read_bytes()==seal_before
        assert len(list((destination/'snapshots').iterdir()))==1
    finally:
        if process.poll() is None:
            process.send_signal(signal.SIGINT)
            try:process.communicate(timeout=15)
            except subprocess.TimeoutExpired:process.kill();process.wait()


@pytest.mark.parametrize('change', ['changed', 'missing'])
def test_external_images_are_authenticated_before_allocation(tmp_path, monkeypatch, change):
    external = tmp_path/'supplied image.bin'
    external.write_bytes(struct.pack('<3I',0xe1c100b2,0xe2800001,0xeafffffc))
    def prepare(root, experiment, output, **options):
        path, spec = preparation(root, experiment, output)
        state = json.loads(path.read_text())
        state['regions'][0]['seeds'][0]['file'] = str(external)
        artifact(output,'code.bin').unlink()
        path.write_text(json.dumps(state))
        return path, spec
    monkeypatch.setattr(runner, 'prepare', prepare)
    path = tmp_path/'source'
    result, error = guarded_run(ROOT,None,path,snapshot=True,instruction_limit=4,config_limits=False)
    assert error is None
    manifest = json.loads((path/'run.json').read_text())
    assert manifest['external_inputs'] == {str(external):identity(external)}
    assert not (path/'inputs').exists() or not list((path/'inputs').iterdir())
    if change == 'changed': external.write_bytes(b'changed')
    else: external.unlink()
    _, error = guarded_run(ROOT,None,tmp_path/'rejected',restore=path,snapshot=True)
    assert 'external input' in str(error) and str(external) in str(error)
    assert not (tmp_path/'rejected').exists()


def test_restore_does_not_read_unrelated_diagnostics_or_trace(tmp_path,monkeypatch):
    path,_=saved(tmp_path,monkeypatch,diagnostics=True,trace=True)
    artifact(path,'result.json').unlink()
    artifact(path,'trace.parquet').write_bytes(b'unrelated corrupted diagnostic trace')
    import db2010.diagnostics.artifacts as artifacts
    def scan(*args): raise AssertionError('restore must not scan the whole run')
    monkeypatch.setattr(artifacts,'files',scan)
    load_checkpoint(path,BINARY)
    from db2010.diagnostics.verify import verify
    with pytest.raises((AssertionError,ValueError)): verify(path)


def test_schema1_and_cold_boot_option_are_retired(tmp_path,monkeypatch):
    path,_=saved(tmp_path,monkeypatch)
    checkpoint=path/'snapshots/final/snapshot.json'
    spec=json.loads(checkpoint.read_text());spec['schema_version']=1
    checkpoint.write_text(json.dumps(spec))
    with pytest.raises(ValueError,match='format version: 1'):
        load_checkpoint(path,BINARY)
    from db2010 import cli
    monkeypatch.setattr(sys,'argv',['db2010', '--research-guards','--cold-boot',str(path)])
    with pytest.raises(SystemExit) as exc:cli.main()
    assert exc.value.code==2


@pytest.mark.parametrize('name', ['config/state.json', 'config/experiment.json', 'inputs/code.bin'])
def test_missing_required_dependency_fails_before_execution(tmp_path,monkeypatch,name):
    path,_=saved(tmp_path,monkeypatch)
    (path/name).unlink()
    _,error=guarded_run(ROOT,None,tmp_path/'rejected',restore=path,snapshot=True)
    assert str(path/name) in str(error)
    assert not (tmp_path/'rejected').exists()


def test_changed_replay_parent_is_rejected(tmp_path,monkeypatch):
    path,_=saved(tmp_path,monkeypatch)
    continued=tmp_path/'continued'
    result,error=guarded_run(ROOT,None,continued,restore=path,snapshot=True,
                            instruction_limit=1,config_limits=False)
    assert error is None
    checkpoint=path/'snapshots/final/snapshot.json'
    checkpoint.write_text(checkpoint.read_text()+' ')
    seal(path)
    _,error=guarded_run(ROOT,None,tmp_path/'rejected',replay=continued,snapshot=True)
    assert 'starting checkpoint identity differs' in str(error)
    assert str(checkpoint) in str(error)
    assert not (tmp_path/'rejected').exists()
    load_checkpoint(continued,BINARY) # final restore does not follow history
