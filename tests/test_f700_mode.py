"""Original mode0 instructions save the latch and set only bits0..2."""
from support.guarded import run as guarded_run
from support.paths import RESOURCES
from db2010.diagnostics.artifacts import artifact as artifact_path
import json
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from db2010.execution import runner
from db2010.paths import ROOT
from test_runner import synthetic_preparation

ORIGINAL=json.loads((RESOURCES/'f700-mode-original.json').read_text())


@pytest.mark.parametrize('extended',[False,True])
def test_original_zero_mode_keeps_saved_word_and_exact_guard(tmp_path,monkeypatch,extended):
    def prepare(root,spec,output):
        path,_=synthetic_preparation(root,spec,output)
        code=artifact_path(output, 'mode.bin', create=True);code.write_bytes(bytes.fromhex(ORIGINAL['hex']))
        state=json.loads(path.read_text());state['entry']=ORIGINAL['start'];state['cpsr']=0xf3
        state['instruction_limit']=ORIGINAL['instruction_count']
        state['regions']=[{'name':'fixture.original','address':0x44ad8000,'size':4096,'readonly':True,
                          'seeds':[{'address':ORIGINAL['start'],'file':str(code)}]},
                         {'name':'fixture.saved','address':0x2000,'size':4096,'seeds':[]}]
        state['executable_ranges']=[{'start':ORIGINAL['start'],'end':ORIGINAL['end']}]
        state['registers'][4:6]=[0xf7000300,0x2000]
        state['device_registers']=[{'address':0xf7000300,'width':4,'initial_value':0x2e0,
                                   'read_or_mask':0,'access':'rw','name':'fixture.control',
                                   'allowed_writes':[0x2e0,*([0x2e7] if extended else [])]}]
        path.write_text(json.dumps(state)+'\n');return path,{'seconds':5}
    monkeypatch.setattr(runner,'prepare',prepare)
    result,failure=guarded_run(ROOT,Path('unused'),tmp_path/'run', diagnostics=True, trace=True);assert failure is None
    snap=result['snapshot'];assert snap['stop']['reason']==('instruction_limit' if extended else 'device_register_command_unmodeled')
    assert snap['device_registers'][str(0xf7000300)]==(0x2e7 if extended else 0x2e0)
    events=pq.read_table(artifact_path((tmp_path) / 'run', 'memory.parquet')).to_pylist()
    assert [(e['address'],e['size'],e['value'],e['write']) for e in events if e['address'] in (0x2000,0xf7000300)]==[
        (0xf7000300,4,0x2e0,False),(0x2000,4,0x2e0,True),*([(0xf7000300,4,0x2e7,True)] if extended else [])]
