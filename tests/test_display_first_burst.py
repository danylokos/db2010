"""Native STR channel-enable fixtures: known pixels and fail-before-effect guards."""
from support.guarded import run as guarded_run
from support.paths import RESOURCES
from db2010.diagnostics.artifacts import artifact as artifact_path
import json
import struct
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from db2010.execution import runner
from db2010.paths import ROOT
from test_runner import synthetic_preparation


def prepare_channel5(root,spec,output,variant,frame=False,stream=False):
    path,_=synthetic_preparation(root,spec,output)
    state=json.loads(path.read_text())
    table=json.loads((RESOURCES/'pdi-reset-programs.json').read_text())
    dma=bytearray.fromhex(table['programs'][0]['bytes_hex'])
    struct.pack_into('<H',dma,24,38720);struct.pack_into('<H',dma,28,2)
    dma.extend(bytes((-len(dma))%4))
    ops=[(0xf7000100+28+i,struct.unpack_from('<I',dma,i)[0]) for i in range(0,len(dma),4)]
    ops += [(0xf70001fc,0x1c1c1c),(0xf70001f8,0x180200),
            (0xf700017c,0x22000000),(0xf7000178,0x21000000),(0xf7000174,0),
            (0xf700018c,0x30000000),(0xf70001dc,5),(0xf7000304,0x120),
            (0xf2000030,1),(0xf20001a0,0x4c100000),
            (0xf20001a4,0xf7000308 if variant!='destination' else 0xf700030c),
            (0xf20001a8,0),(0xf20001ac,0x84489004),(0xf20001b0,0xea52)]
    code=[]
    for address,value in ops:
        code += [0xe59f0008,0xe59f1008,0xe5801000,0xea000001,address,value]
    code += [0xe59f0008,0xe59f1008,0xe59f2008,0xe12fff12,0xf20001b0,0xea53,0x447a7591]
    if frame:
        code.extend([0]*((2048-len(code)*4)//4));code.append(0xeafffffe)
    (artifact_path(output, 'code.bin', create=True)).write_bytes(struct.pack('<'+'I'*len(code),*code))
    original=json.loads((RESOURCES/'pdi-original.json').read_text())['sources']['dma_channel_enable']
    native=artifact_path(output, 'native-enable.bin', create=True)
    # Genuine STR followed by explicit fixture BX LR glue for continued execution.
    native.write_bytes(bytes.fromhex(original['hex'])[:2]+(bytes.fromhex('7047') if frame else bytes.fromhex(original['hex'])[2:]))
    source=artifact_path(output, 'source.bin', create=True)
    payload=(b'\x01'+bytes(15)) if variant=='nonzero' else bytes(15 if variant=='unknown' else 16)
    if frame:payload=bytes(16) if variant=='late_unknown' else bytes(16)+b'\x01'+bytes(77423) if variant=='late_nonzero' else bytes(77440)
    if variant=='stream_nonzero':payload=(bytes.fromhex('00f8e0071f00')*12907)[:77440]
    source.write_bytes(payload)
    state['regions'] += [
        {'name':'fixture.native','address':0x447a7000,'size':4096,'readonly':True,'seeds':[{'address':original['start'],'file':str(native)}]},
        {'name':'fixture.source','address':0x4c100000,'size':81920 if frame else 4096,'readonly':False,'seeds':[{'address':0x4c100000,'file':str(source)}]},
    ]
    state['instruction_limit']=6000 if frame else 1000
    if frame:state['registers'][14]=0x1800
    state['executable_ranges']=[{'start':0x1000,'end':0x1000+len(code)*4}, {'start':original['start'],'end':original['end']}]
    state['pdi']={'profile':'r61500-write-packets'}
    state['pl080_dma']={'profile':'cold-disabled-display-stream' if stream else 'cold-disabled-display-frame' if frame else 'cold-disabled-display-first-burst','address':0xf2000000,'channels':8}
    state['device_registers']=[{'address':0xf7000304,'width':4,'name':'fixture.stream',
        'initial_value':0,'read_or_mask':0,'allowed_writes':[0,0x20,0x120]}]
    path.write_text(json.dumps(state)+'\n');return path,{'seconds':5}


@pytest.mark.parametrize('variant', ['stream_nonzero', 'late_unknown'])
def test_stream_preserves_known_bytes_and_unknown_guard(tmp_path,monkeypatch,variant):
    def prepare(root,spec,output):return prepare_channel5(root,spec,output,variant,frame=True,stream=True)
    monkeypatch.setattr(runner,'prepare',prepare)
    result,failure=guarded_run(ROOT,Path('unused'),tmp_path/'run', diagnostics=True, trace=True)
    assert failure is None
    snap=result['snapshot']
    if variant=='late_unknown':
        assert snap['stop']['reason']=='display_dma_source_unknown'
        assert snap['pdi']['completed_frames']==0 and snap['pdi']['frame']['bytes']==16
    else:
        assert snap['stop']['reason']=='instruction_limit'
        assert snap['pdi']['completed_frames']==1
        assert bytes.fromhex(snap['pdi']['frames'][0]['pixels_hex'])==(artifact_path(tmp_path/'run', 'source.bin')).read_bytes()
        assert snap['pl080']['raw_tc']==0x20


@pytest.mark.parametrize('reenable', [False, True])
def test_second_frame_waits_for_native_enable(tmp_path,monkeypatch,reenable):
    def prepare(root,spec,output):
        path,limits=prepare_channel5(root,spec,output,'black',frame=True,stream=True)
        state=json.loads(path.read_text())
        # Explicit CPU fixture cleanup/control writes; normal boot uses its
        # original PDI/PL080 handlers, separately proved by full-run evidence.
        code=list(struct.unpack('<'+str((artifact_path(output, 'code.bin')).stat().st_size//4)+'I',(artifact_path(output, 'code.bin')).read_bytes()))[:-1]
        code += [0xe59f3008,0xe2533001,0x1afffffd,0xea000000,5000]
        ops=[(0xf2000008,0x20),(0xf70001f8,0x180208),(0xf70001dc,5),
             (0xf700017c,0x22000000),(0xf7000178,0x21000000),(0xf7000174,0),
             (0xf700018c,0x30000000),(0xf70001dc,5),
             (0xf20001a0,0x4c100000),(0xf20001b0,0xea52)]
        for address,value in ops:
            code += [0xe59f0008,0xe59f1008,0xe5801000,0xea000001,address,value]
        if reenable:
            return_address=0x1000+4*(len(code)+9)
            code += [0xe59f000c,0xe59f100c,0xe59f200c,0xe59fe00c,0xe12fff12,
                     0xf20001b0,0xea53,0x447a7591,return_address]
        code.append(0xeafffffe)
        (artifact_path(output, 'code.bin', create=True)).write_bytes(struct.pack('<'+'I'*len(code),*code))
        state['instruction_limit']=18000
        state['executable_ranges'][0]['end']=0x1000+len(code)*4
        path.write_text(json.dumps(state)+'\n')
        return path,limits
    monkeypatch.setattr(runner,'prepare',prepare)
    result,failure=guarded_run(ROOT,Path('unused'),tmp_path/'run', diagnostics=True, trace=True)
    assert failure is None
    snap=result['snapshot']
    assert snap['stop']['reason']=='instruction_limit'
    assert snap['pdi']['completed_frames']==(2 if reenable else 1)
    assert snap['pdi']['dma_bytes']==(0 if reenable else 77440)


@pytest.mark.parametrize('variant,reason', [
    ('black','display_dma_next_burst_unmodeled'),
    ('unknown','display_dma_source_unknown'),
    ('nonzero','display_dma_nonzero_pixels_unmodeled'),
    ('destination','display_dma_configuration_unmodeled'),
])
def test_native_channel5_enable(tmp_path,monkeypatch,variant,reason):
    def prepare(root,spec,output):return prepare_channel5(root,spec,output,variant)
    monkeypatch.setattr(runner,'prepare',prepare)
    result,failure=guarded_run(ROOT,Path('unused'),tmp_path/'run', diagnostics=True, trace=True)
    assert failure is None
    snap=result['snapshot'];assert snap['stop']['reason']==reason
    starts=[e for e in pq.read_table(artifact_path((tmp_path) / 'run', 'memory.parquet')).to_pylist()
            if e['write'] and e['address']==0xf20001b0 and e['value']==0xea53]
    packets=snap['pdi']['transfers']
    if variant=='black':
        assert len(starts)==1 and starts[0]['pc']==0x447a7590
        assert snap['registers'][15]==0x447a7592
        assert len(packets)==2 and packets[1]['bytes']==[0]*16 and packets[1]['rs']==[1]*16
        assert packets[1]['source_all_known'] and packets[1]['source']==0x4c100000
        assert snap['pdi']['dma_bytes']==77424
        assert snap['pl080']['channels'][5]['source']==0x4c100010
        assert snap['pl080']['raw_tc']==snap['pl080']['raw_error']==0
    else:
        assert starts==[] and len(packets)==1
        assert snap['registers'][15]==0x447a7590
        assert snap['pdi']['dma_bytes']==77440
        assert snap['pl080']['channels'][5]['source']==0x4c100000
        assert snap['pl080']['channels'][5]['configuration']==0xea52


@pytest.mark.parametrize('variant,reason',[
    ('black','instruction_limit'),
    ('late_unknown','display_dma_source_unknown'),
    ('late_nonzero','display_dma_nonzero_pixels_unmodeled'),
])
def test_remaining_frame_source_guards(tmp_path,monkeypatch,variant,reason):
    monkeypatch.setattr(runner,'prepare',lambda r,s,o:prepare_channel5(r,s,o,variant,frame=True))
    result,failure=guarded_run(ROOT,Path('unused'),tmp_path/'run', diagnostics=True, trace=True)
    assert failure is None
    snap=result['snapshot'];assert snap['stop']['reason']==reason
    frame=snap['pdi']['frame'];ch=snap['pl080']['channels'][5]
    if variant=='black':
        assert bytes.fromhex(frame['pixels_hex'])==bytes(77440)
        assert frame['bytes']==77440 and len(frame['remaining_bursts'])==4839
        assert snap['pdi']['dma_bytes']==0 and not snap['pdi']['dma_armed'] and snap['pdi']['dma_complete']
        assert ch['source']==0x4c100000+77440 and ch['configuration']==0xea52
        assert snap['pl080']['raw_tc']==0x20 and snap['pl080']['raw_error']==0
    else:
        assert frame['bytes']==16 and not frame['completed']
        assert snap['pdi']['dma_bytes']==77424 and snap['pdi']['dma_armed']
        assert ch['source']==0x4c100010 and ch['configuration']==0xea53
        assert snap['pl080']['raw_tc']==0
