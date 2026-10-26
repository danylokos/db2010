from support.guarded import run as guarded_run
from db2010.diagnostics.artifacts import artifact as artifact_path
import json
from pathlib import Path
import struct
import pytest
from db2010.execution import runner
from db2010.paths import ROOT
from test_runner import synthetic_preparation


@pytest.mark.parametrize('address',[0x2000,0x2004,0x2FFC])
def test_doubleword_store_load_roundtrip_and_full_bus_value(tmp_path,monkeypatch,address):
    def prepare(root,spec,output):
        path,_=synthetic_preparation(root,spec,output)
        # STRD r0,r1,[r2]; LDRD r4,r5,[r2]; stop. Start with unknown RAM:
        # both words must become known only after the successful native store.
        (artifact_path(output, 'code.bin', create=True)).write_bytes(struct.pack('<3I',0xE1C200F0,0xE1C240D0,0xEAFFFFFE))
        state=json.loads(path.read_text());state['registers'][:3]=[0x11223344,0x55667788,address]
        state['instruction_limit']=3
        state['executable_ranges']=[{'start':0x1000,'end':0x100C}]
        state['regions'].append({'name':'fixture.data','address':0x2000,'size':8192,'seeds':[]})
        path.write_text(json.dumps(state)+'\n');return path,{'seconds':5}
    monkeypatch.setattr(runner,'prepare',prepare)
    result,failure=guarded_run(ROOT,Path('unused'),tmp_path/'run', diagnostics=True, trace=True)
    assert failure is None and result['snapshot']['stop']['reason']=='instruction_limit'
    assert result['snapshot']['registers'][4:6]==[0x11223344,0x55667788]
    import pyarrow.parquet as pq
    events=pq.read_table(artifact_path(tmp_path/'run','memory.parquet')).to_pylist()
    assert [(e['address'],e['size'],e['value'],e['write']) for e in events]==[
        (address,8,0x5566778811223344,True),(address,8,0x5566778811223344,False)]


@pytest.mark.parametrize('word,readonly,seed_bytes,address,reason',[
    (0xE1C200D0,False,4,0x2000,'read_unknown_ram'),
    (0xE1C200F0,True,8,0x2000,'write_readonly_memory'),
    (0xE1C200D0,False,4,0x2FFC,'read_unmapped'),
    (0xE1C200F0,False,4,0x2FFC,'write_unmapped'),
    (0xE1C200D0,False,0,0xFE004000,'region_register_access_unmodeled'),
    (0xE1C200F0,False,0,0xFE004000,'region_register_access_unmodeled'),
])
def test_doubleword_guards_reject_before_effects(tmp_path,monkeypatch,word,readonly,seed_bytes,address,reason):
    def prepare(root,spec,output):
        path,_=synthetic_preparation(root,spec,output)
        (artifact_path(output,'code.bin',create=True)).write_bytes(struct.pack('<I',word))
        data=artifact_path(output,'data.bin',create=True);data.write_bytes(bytes(range(seed_bytes)))
        state=json.loads(path.read_text());state['registers'][:3]=[77,88,address]
        state['regions'].append({'name':'fixture.data','address':0x2000,'size':4096,
            'readonly':readonly,'seeds':[{'address':address,'file':str(data)}] if seed_bytes else []})
        state['region_registers']={'address':0xFE004000,'initial_words':[0]*24}
        path.write_text(json.dumps(state)+'\n');return path,{'seconds':5}
    monkeypatch.setattr(runner,'prepare',prepare)
    result,failure=guarded_run(ROOT,Path('unused'),tmp_path/'run',diagnostics=True,trace=True)
    assert failure is None and result['snapshot']['stop']['reason']==reason
    assert result['snapshot']['registers'][:3]==[77,88,address]
    assert result['memory_producer']['events']==0


def test_unknown_svc_vector_is_explicit_dependency(tmp_path,monkeypatch):
    def prepare(root,spec,output):
        path,_=synthetic_preparation(root,spec,output);(artifact_path(output, 'code.bin', create=True)).write_bytes(struct.pack('<I',0xEF000001))
        return path,{'seconds':5}
    monkeypatch.setattr(runner,'prepare',prepare)
    result,failure=guarded_run(ROOT,Path('unused'),tmp_path/'run', diagnostics=True, trace=True)
    assert failure is None and result['snapshot']['stop']['reason']=='exception_vector_unmodeled'
    assert result['snapshot']['stop']['address']==8


def test_big_endian_redirect_is_rejected_before_control_changes(tmp_path,monkeypatch):
    def prepare(root,spec,output):
        path,_=synthetic_preparation(root,spec,output);(artifact_path(output, 'code.bin', create=True)).write_bytes(struct.pack('<I',0xEE010F10))
        state=json.loads(path.read_text());state['registers'][0]=0x900F8
        path.write_text(json.dumps(state)+'\n');return path,{'seconds':5}
    monkeypatch.setattr(runner,'prepare',prepare)
    result,failure=guarded_run(ROOT,Path('unused'),tmp_path/'run', diagnostics=True, trace=True)
    assert failure is None and result['snapshot']['stop']['reason']=='cp15_big_endian_mapping_unmodeled'
    assert result['snapshot']['sctlr']==0x90078


@pytest.mark.parametrize('mapped_vector',[False,True])
def test_native_prefetch_abort_handler_or_explicit_missing_vector(tmp_path,monkeypatch,mapped_vector):
    def prepare(root,spec,output):
        path,_=synthetic_preparation(root,spec,output)
        # Enable the identity MMU; branch to known code in a section with a fault
        # descriptor. An original low abort handler reads IFSR and halts by guard.
        (artifact_path(output, 'code.bin', create=True)).write_bytes(struct.pack('<4I',0xEE020F10,0xEE031F10,0xEE012F10,0xE12FFF14))
        target=artifact_path(output, 'target.bin', create=True);target.write_bytes(struct.pack('<I',0xE3A0702A))
        state=json.loads(path.read_text());state['registers'][:5]=[0xFE000000,1,0x90079,0,0x100000]
        state['instruction_limit']=8;state['executable_ranges']=[{'start':0x1000,'end':0x1010},{'start':0x100000,'end':0x100004}]
        state['regions'].append({'name':'fixture.target','address':0x100000,'size':4096,'readonly':True,'seeds':[{'address':0x100000,'file':str(target)}]})
        if mapped_vector:
            vector=artifact_path(output, 'prefetch.bin', create=True);vector.write_bytes(struct.pack('<3I',0xEE158F30,0xE1A0900E,0xEAFFFFFE))
            state['regions'].append({'name':'fixture.abort','address':0,'size':4096,'readonly':True,'seeds':[{'address':0xC,'file':str(vector)}]})
            state['executable_ranges'].append({'start':0xC,'end':0x18})
        state['region_registers']={'address':0xFE004000,'initial_words':[0x80000000]+[0]*11+[0xC12]+[0]*11}
        state['section_table']={'address':0xFE000000,'profile':'identity-disjoint-sections'};state['section_table_update_policy']='tlb-boundary'
        path.write_text(json.dumps(state)+'\n');return path,{'seconds':5}
    monkeypatch.setattr(runner,'prepare',prepare)
    result,failure=guarded_run(ROOT,Path('unused'),tmp_path/'run', diagnostics=True, trace=True)
    assert failure is None
    if mapped_vector:
        assert result['snapshot']['stop']['reason']=='instruction_limit'
        assert result['snapshot']['registers'][8:10]==[5,0x100004]
        assert result['snapshot']['cpsr']==0xD7
    else:assert result['snapshot']['stop']['reason']=='exception_vector_unmodeled'


def test_multi_load_preserves_only_completed_bus_effects(tmp_path,monkeypatch):
    def prepare(root,spec,output):
        path,_=synthetic_preparation(root,spec,output)
        (artifact_path(output, 'code.bin', create=True)).write_bytes(struct.pack('<I',0xE8B10005))
        data=artifact_path(output, 'data.bin', create=True);data.write_bytes(struct.pack('<I',42))
        state=json.loads(path.read_text());state['registers'][:3]=[77,0x2FFC,88]
        state['regions'].append({'name':'fixture.data','address':0x2000,'size':8192,
                                'seeds':[{'address':0x2FFC,'file':str(data)}]})
        path.write_text(json.dumps(state)+'\n');return path,{'seconds':5}
    monkeypatch.setattr(runner,'prepare',prepare)
    result,failure=guarded_run(ROOT,Path('unused'),tmp_path/'run', diagnostics=True, trace=True)
    assert failure is None and result['snapshot']['stop']['reason']=='read_unknown_ram'
    assert result['snapshot']['registers'][:3]==[42,0x2FFC,88]
    import pyarrow.parquet as pq
    events=pq.read_table(artifact_path((tmp_path) / 'run', 'memory.parquet')).to_pylist()
    assert [(e['address'],e['value'],e['write']) for e in events]==[(0x2FFC,42,False)]


@pytest.mark.parametrize('store',[0xE5812000,0xE1C120F0])
def test_native_alignment_abort_does_not_commit_failed_store(tmp_path,monkeypatch,store):
    def prepare(root,spec,output):
        path,_=synthetic_preparation(root,spec,output)
        # SCTLR.A enables native alignment faults. The handler reads DFSR/DFAR
        # and skips the failed STR using the native banked LR/SPSR return path.
        (artifact_path(output, 'code.bin', create=True)).write_bytes(struct.pack('<4I',0xEE010F10,store,0xE5917000,0xEAFFFFFE))
        vector=artifact_path(output, 'abort.bin', create=True);vector.write_bytes(struct.pack('<3I',0xEE158F10,0xEE169F10,0xE25EF004))
        state=json.loads(path.read_text());state['instruction_limit']=6
        state['registers'][:3]=[0x9007A,0x2001,42]
        state['executable_ranges']=[{'start':0x1000,'end':0x1010},{'start':0x10,'end':0x1C}]
        state['regions'] += [
            {'name':'fixture.abort','address':0,'size':4096,'readonly':True,'seeds':[{'address':0x10,'file':str(vector)}]},
            {'name':'fixture.data','address':0x2000,'size':4096,'seeds':[]}]
        path.write_text(json.dumps(state)+'\n');return path,{'seconds':5}
    monkeypatch.setattr(runner,'prepare',prepare)
    result,failure=guarded_run(ROOT,Path('unused'),tmp_path/'run', diagnostics=True, trace=True)
    assert failure is None and result['snapshot']['stop']['reason']=='read_unknown_ram'
    assert result['snapshot']['registers'][7:10]==[0,1,0x2001]
    assert result['snapshot']['cpsr']==0xD3
    assert result['memory_producer']['events']==0
