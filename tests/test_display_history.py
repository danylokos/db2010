"""Native repeated DMA completion must outlive diagnostic retention windows."""
from support.guarded import run as guarded_run
import json
import struct
from pathlib import Path
import pytest
from db2010.execution.qmp import MAX_FRAME_BYTES, QMP
from db2010.execution import runner
from db2010.diagnostics.artifacts import artifact
from support.fixtures import qemu_run, ROOT
from test_display_first_burst import prepare_channel5


@pytest.mark.parametrize('pixels,count,interrupt,omit', [(8,2200,False,False),(8,2200,False,True),(38720,300,False,False),(38720,300,True,False)])
def test_recurring_display_history_preserves_protocol_and_latest_pixels(tmp_path,monkeypatch,pixels,count,interrupt,omit):
    path,_=prepare_channel5(ROOT,Path('unused'),tmp_path/'inputs','black',frame=True,stream=True)
    state=json.loads(path.read_text())
    binary=artifact(tmp_path/'inputs','code.bin')
    setup=list(struct.unpack('<'+str(binary.stat().st_size//4)+'I',binary.read_bytes()))[:-1]
    setup=setup[:setup.index(0xe12fff12)+4]
    for i in range(0,len(setup)-5,6):
        if setup[i+4]==0xf7000134:setup[i+5]=(setup[i+5]&0xffff0000)|pixels
    # Small updates exercise the count window; full-screen updates exercise the
    # byte window with both transferred pixels and the completed panel retained.
    program=bytes.fromhex('1f1d09001f2090991f289c9917980c001705150517f81f001f08')+bytes(2)
    ops=[(0xf7000100+i,int.from_bytes(program[i:i+4],'little')) for i in range(0,len(program),4)]
    ops += [(0xf70001f8,0x200),(0xf7000174,0x30720300),(0xf7000178,0x00af4400),
            (0xf700017c,0x00db4500),(0xf700018c,0xccc00000),(0xf70001dc,1)]
    code=[]
    for address,value in ops:code += [0xe59f0008,0xe59f1008,0xe5801000,0xea000001,address,value]
    code += setup
    code += [0]*((2048-len(code)*4)//4)
    poll=0x1000+len(code)*4
    code += [0xe59f000c,0xe5901000,0xe3110020,0x0afffffb,0xea000000,0xf2000004,
             0xe2544001,0]
    done_branch=len(code)-1
    code += [0xe59f0004,0xe5804000,0xea000000,0x4c100000]
    for address,value in [(0xf2000008,0x20),(0xf70001f8,0x180208),(0xf70001dc,5),
            (0xf700017c,0x22000000),(0xf7000178,0x21000000),(0xf7000174,0),
            (0xf700018c,0x30000000),(0xf70001dc,5),(0xf20001a0,0x4c100000),(0xf20001b0,0xea52)]:
        code += [0xe59f0008,0xe59f1008,0xe5801000,0xea000001,address,value]
    code += [0xe59f000c,0xe59f100c,0xe59f200c,0xe59fe00c,0xe12fff12,
             0xf20001b0,0xea53,0x447a7591,poll]
    done=0x1000+len(code)*4
    code[done_branch]=0x0a000000|((done-(0x1000+done_branch*4+8))//4&0xffffff)
    code.append(0xeafffffe)
    binary.write_bytes(struct.pack('<'+'I'*len(code),*code))
    state['executable_ranges'][0]['end']=0x1000+len(code)*4
    state['instruction_limit']=1000000 if pixels==8 else 10000000
    state['pl080_dma']['profile']='cold-disabled-display-partial-fa-tx'
    state['registers'][4]=count
    state['recording_history']={'profile':'startup64-recent64-irq512'}
    state['record_diagnostics']=not omit
    if pixels==8:
        for region in state['regions']:
            for seed in region.get('seeds',[]):seed['data']=Path(seed.pop('file')).read_bytes()
        result=qemu_run(state,tmp_path/'vm',trace=False,reset=True,timeout=45)
        assert result['after_reset']['pdi']['completed_frames']==0
        assert result['after_reset']['pdi']['frames']==[]
    else:
        # Exercise the production client's frame bound and snapshot deadline.
        path.write_text(json.dumps(state)+'\n')
        def prepared(root,spec,output,**options):
            import shutil
            from db2010.diagnostics.artifacts import prepared_references
            output.mkdir(exist_ok=True)
            for group in ('inputs','config','provenance'):
                source=tmp_path/'inputs'/group
                if source.exists():shutil.copytree(source,output/group,dirs_exist_ok=True)
            frozen=prepared_references(state,tmp_path/'inputs',absolute=False)
            target=artifact(output,'state.json',create=True)
            target.write_text(json.dumps(frozen))
            return target,{'seconds':45}
        monkeypatch.setattr(runner,'prepare',prepared)
        replies=[]
        receive=QMP.receive
        def record_reply(self,deadline):
            response=receive(self,deadline)
            replies.append(len(json.dumps(response).encode()))
            return response
        monkeypatch.setattr(QMP,'receive',record_reply)
        if interrupt:
            wait_stop=QMP.wait_stop
            def interrupted(self,deadline,**options):
                wait_stop(self,deadline,**options)
                raise KeyboardInterrupt()
            monkeypatch.setattr(QMP,'wait_stop',interrupted)
        capture,failure=guarded_run(ROOT,Path('unused'),tmp_path/'capture', diagnostics=True)
        assert isinstance(failure,KeyboardInterrupt) if interrupt else failure is None
        assert capture['success'] is (not interrupt)
        assert capture['qemu_returncode']==0
        assert max(replies)<512*1024
        result=capture['snapshot']
    reason=result['stop']['reason']
    assert reason=='instruction_limit'
    pdi=result['pdi']
    assert pdi['completed_frames']==count
    assert len(pdi['frames'])==(1 if omit else 256)
    assert pdi['frames_dropped']==count-len(pdi['frames'])
    if pixels==8 and not omit:
        assert len(pdi['frames'])==256
        assert len(pdi['transfers'])==4096
        assert pdi['transfers_total']-pdi['transfers_dropped']==4096
    assert pdi['stop_commands']==count-1 and result['pl080']['raw_tc']==32
    if omit:
        assert pdi['transfers']==[] and pdi['frame']['remaining_bursts']==[]
        assert 'update_hex' not in pdi['frames'][-1]
    else:
        assert pdi['frames'][-1]['update_hex'].startswith('01000000')
    assert pdi['frames'][-1]['panel_known_pixels']==pixels
    # Include the outer QMP string escaping used by the production client.
    envelope=len(json.dumps({'return':json.dumps(result)}).encode())
    assert (envelope>MAX_FRAME_BYTES) == (pixels==38720)
