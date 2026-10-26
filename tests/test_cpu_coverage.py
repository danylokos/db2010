from support.guarded import run as guarded_run

from db2010.diagnostics.artifacts import artifact as artifact_path
import json
from pathlib import Path
import struct
import pytest

from db2010.execution import runner
from db2010.paths import ROOT
from test_runner import synthetic_preparation


def test_original_copy_guard_rejects_changed_guest_written_code(tmp_path, monkeypatch):
    def prepare(root, spec, output):
        path, _ = synthetic_preparation(root, spec, output)
        # Store a changed MOV to RAM, then branch there. Validity alone permits
        # the write; original-code proof must reject its fetch before MOV effects.
        (artifact_path(output, 'code.bin', create=True)).write_bytes(struct.pack('<2I', 0xE5810000, 0xE12FFF11))
        proof = artifact_path(output, 'expected.bin', create=True);proof.write_bytes(struct.pack('<I', 0xE3A0202A))
        state = json.loads(path.read_text())
        state['executable_ranges'] = [{'start':0x1000,'end':0x1008}, {'start':0x2000,'end':0x2004}]
        state['regions'].append({'name':'fixture.copy','address':0x2000,'size':4096,'seeds':[]})
        state['execution_proofs'] = [{'address':0x2000,'file':str(proof)}]
        state['registers'][0:3] = [0xE3A02063, 0x2000, 77]
        path.write_text(json.dumps(state)+'\n')
        return path, {'seconds':5}
    monkeypatch.setattr(runner, 'prepare', prepare)
    result, failure = guarded_run(ROOT, Path('unused'), tmp_path/'run', diagnostics=True, trace=True)
    assert failure is None and result['snapshot']['stop']['reason'] == 'fetch_original_copy_mismatch'
    assert result['snapshot']['registers'][2] == 77


def test_proof_does_not_make_unknown_ram_executable(tmp_path, monkeypatch):
    def prepare(root, spec, output):
        path, _ = synthetic_preparation(root, spec, output)
        (artifact_path(output, 'code.bin', create=True)).write_bytes(struct.pack('<I', 0xE12FFF11))
        proof = artifact_path(output, 'expected.bin', create=True);proof.write_bytes(struct.pack('<I', 0xE3A0202A))
        state = json.loads(path.read_text());state['registers'][1] = 0x2000
        state['regions'].append({'name':'fixture.copy','address':0x2000,'size':4096,'seeds':[]})
        state['executable_ranges'].append({'start':0x2000,'end':0x2004})
        state['execution_proofs'] = [{'address':0x2000,'file':str(proof)}]
        path.write_text(json.dumps(state)+'\n')
        return path, {'seconds':5}
    monkeypatch.setattr(runner, 'prepare', prepare)
    result, failure = guarded_run(ROOT, Path('unused'), tmp_path/'run', diagnostics=True, trace=True)
    assert failure is None and result['snapshot']['stop']['reason'] == 'fetch_unknown_bytes'


@pytest.mark.parametrize('exchange',[False,True])
def test_native_thumb_bl_across_page_boundary_and_return(tmp_path, monkeypatch,exchange):
    def prepare(root, spec, output):
        path, _ = synthetic_preparation(root, spec, output)
        call = artifact_path(output, 'call.bin', create=True);call.write_bytes(struct.pack('<4H', 0xF000,0xE80F if exchange else 0xF80F,0x2221,0xE7FE))
        sub = artifact_path(output, 'sub.bin', create=True);sub.write_bytes(struct.pack('<2I',0xE3A01055,0xE12FFF1E) if exchange else struct.pack('<2H',0x2155,0x4770))
        state=json.loads(path.read_text());state.update(entry=0x1FFE,cpsr=0xF3,instruction_limit=6)
        state['regions']=[{'name':'fixture.code','address':0x1000,'size':8192,'readonly':True,
                          'seeds':[{'address':0x1FFE,'file':str(call)},{'address':0x2020,'file':str(sub)}]}]
        state['executable_ranges']=[{'start':0x1FFE,'end':0x2006},{'start':0x2020,'end':0x2028 if exchange else 0x2024}]
        path.write_text(json.dumps(state)+'\n');return path,{'seconds':5}
    monkeypatch.setattr(runner, 'prepare', prepare)
    result,failure=guarded_run(ROOT,Path('unused'),tmp_path/'run', diagnostics=True, trace=True)
    assert failure is None and result['snapshot']['stop']['reason']=='instruction_limit'
    assert result['snapshot']['registers'][1:3]==[0x55,0x21]
    assert result['snapshot']['registers'][14]==0x2003
    import pyarrow.parquet as pq
    rows=pq.read_table(artifact_path((tmp_path) / 'run', 'trace.parquet')).to_pylist()
    assert [(r['pc'],r['size'],r['bytes']) for r in rows[:2]]==[(0x1FFE,2,'00f0'),(0x2000,2,'0fe8' if exchange else '0ff8')]
    assert rows[1]['registers']['lr']==0x2002
    assert [r['pc'] for r in rows[:5]]==[0x1FFE,0x2000,0x2020,0x2024 if exchange else 0x2022,0x2002]
    assert rows[2]['thumb']==(not exchange)


def test_multi_store_preserves_only_completed_bus_effects(tmp_path,monkeypatch):
    def prepare(root,spec,output):
        path,_=synthetic_preparation(root,spec,output)
        (artifact_path(output, 'code.bin', create=True)).write_bytes(struct.pack('<I',0xE8A10005))
        state=json.loads(path.read_text());state['registers'][0:3]=[0x12345678,0x2FFC,0xABCDEF]
        state['regions'] += [
            {'name':'fixture.ram','address':0x2000,'size':4096,'seeds':[]},
            {'name':'fixture.protected','address':0x3000,'size':4096,'readonly':True,'seeds':[]}]
        path.write_text(json.dumps(state)+'\n');return path,{'seconds':5}
    monkeypatch.setattr(runner,'prepare',prepare)
    result,failure=guarded_run(ROOT,Path('unused'),tmp_path/'run', diagnostics=True, trace=True)
    assert failure is None and result['snapshot']['stop']['reason']=='write_readonly_memory'
    assert result['snapshot']['registers'][1]==0x2FFC
    import pyarrow.parquet as pq
    events=pq.read_table(artifact_path((tmp_path) / 'run', 'memory.parquet')).to_pylist()
    assert [(r['address'],r['value'],r['write']) for r in events]==[(0x2FFC,0x12345678,True)]


@pytest.mark.parametrize('domain,attribute,fault',[(1,0x12,13),(1,0xC32,25)])
def test_native_mmu_permission_abort_and_retry(tmp_path,monkeypatch,domain,attribute,fault):
    def prepare(root,spec,output):
        path,_=synthetic_preparation(root,spec,output)
        # Native data permission abort; original handler grants domain manager
        # access and returns using banked LR/SPSR to retry the rejected load.
        (artifact_path(output, 'code.bin', create=True)).write_bytes(struct.pack('<5I',0xEE020F10,0xEE031F10,0xEE012F10,0xE5947000,0xEAFFFFFE))
        vector=artifact_path(output, 'abort.bin', create=True);vector.write_bytes(struct.pack('<5I',0xEE158F10,0xEE169F10,0xE3A0100F,0xEE031F10,0xE25EF008))
        data=artifact_path(output, 'data.bin', create=True);data.write_bytes(struct.pack('<I',42))
        state=json.loads(path.read_text());state['instruction_limit']=11
        state['registers'][:8]=[0xFE000000,domain,0x90079,0,0x100000,0,0,77]
        state['executable_ranges']=[{'start':0x1000,'end':0x1014},{'start':0x10,'end':0x24}]
        state['regions'] += [
            {'name':'fixture.abort','address':0,'size':4096,'readonly':True,'seeds':[{'address':0x10,'file':str(vector)}]},
            {'name':'fixture.data','address':0x100000,'size':4096,'seeds':[{'address':0x100000,'file':str(data)}]}]
        state['region_registers']={'address':0xFE004000,'initial_words':[0x80000000,0x80010001]+[0]*10+[0xC12,attribute]+[0]*10}
        state['section_table']={'address':0xFE000000,'profile':'identity-disjoint-sections'}
        state['section_table_update_policy']='tlb-boundary'
        path.write_text(json.dumps(state)+'\n');return path,{'seconds':5}
    monkeypatch.setattr(runner,'prepare',prepare)
    result,failure=guarded_run(ROOT,Path('unused'),tmp_path/'run', diagnostics=True, trace=True)
    assert failure is None and result['snapshot']['stop']['reason']=='instruction_limit'
    assert result['snapshot']['registers'][7:10]==[42,fault,0x100000]
    assert result['snapshot']['cpsr']==0xD3


def test_cross_region_read_stops_without_destination_effects(tmp_path, monkeypatch):
    def prepare(root,spec,output):
        path,_=synthetic_preparation(root,spec,output)
        (artifact_path(output, 'code.bin', create=True)).write_bytes(struct.pack('<I',0xE5912000))
        data=artifact_path(output, 'data.bin', create=True);data.write_bytes(b'\x44\x33')
        state=json.loads(path.read_text());state['registers'][1:3]=[0x2FFE,77]
        state['regions'].append({'name':'fixture.data','address':0x2000,'size':4096,
                                'seeds':[{'address':0x2FFE,'file':str(data)}]})
        path.write_text(json.dumps(state)+'\n');return path,{'seconds':5}
    monkeypatch.setattr(runner,'prepare',prepare)
    result,failure=guarded_run(ROOT,Path('unused'),tmp_path/'run', diagnostics=True, trace=True)
    assert failure is None and result['snapshot']['stop']['reason']=='read_unmapped'
    assert result['snapshot']['registers'][2]==77
