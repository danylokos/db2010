"""Bounded cold input for native DTCM; guest writes and unknown neighbors."""
from support.guarded import run as guarded_run
from db2010.diagnostics.artifacts import artifact as artifact_path
import json
from pathlib import Path
import struct

import pytest

from db2010.execution import runner
from db2010.paths import ROOT
from db2010.preparation.inputs import reset_record_input
from test_runner import synthetic_preparation

PROFILE = {'profile':'cold-zero-reset-record','address':0x42000604,'size':28,'fill':0}


def execute(tmp_path, monkeypatch, words, *, enabled=True, address=0x42000604, base=0x42000000):
    def prepare(root,spec,output):
        path,_=synthetic_preparation(root,spec,output)
        code=[0xee090f11,*words,0xeafffffe]
        (artifact_path(output, 'code.bin', create=True)).write_bytes(struct.pack('<'+str(len(code))+'I',*code))
        state=json.loads(path.read_text());state['instruction_limit']=len(code)
        state['executable_ranges'][0]['end']=0x1000+4*len(code)
        state['registers'][:3]=[base|0x1d,address,0x5a]
        if enabled:
            reset_record_input(PROFILE);state['dtcm_reset_record']=PROFILE
        path.write_text(json.dumps(state)+'\n');return path,{'seconds':5}
    monkeypatch.setattr(runner,'prepare',prepare)
    result,failure=guarded_run(ROOT,Path('unused'),tmp_path/'run', diagnostics=True)
    assert failure is None
    return result['snapshot']


def test_all_seven_words_are_known_zero_and_guest_writes_take_effect(tmp_path,monkeypatch):
    words=[0xe5913000|((reg-3)*4)|((reg-3)<<12) for reg in range(3,10)]
    # Native store replaces the declared input; re-writing CP15 enable does not clear it.
    words += [0xe5812000,0xee090f11,0xe591a000]
    snap=execute(tmp_path,monkeypatch,words)
    assert snap['stop']['reason']=='instruction_limit'
    assert snap['registers'][3:10]==[0]*7 and snap['registers'][10]==0x5a


@pytest.mark.parametrize('address,word,unknown',[
    (0x42000603,0xe5d13000,0x42000603),
    (0x42000620,0xe5d13000,0x42000620),
    (0x42000602,0xe5913000,0x42000602),
    (0x4200061e,0xe5913000,0x42000620),
])
def test_unknown_neighbors_and_crossing_reads_still_stop(tmp_path,monkeypatch,address,word,unknown):
    snap=execute(tmp_path,monkeypatch,[word],address=address)
    assert snap['stop']['reason']=='read_unknown_ram' and snap['stop']['address']==unknown


def test_default_still_guards_allocator_zero(tmp_path,monkeypatch):
    snap=execute(tmp_path,monkeypatch,[0xe5d13000],enabled=False)
    assert snap['stop']['reason']=='read_unknown_ram' and snap['stop']['address']==0x42000604


def test_declared_input_cannot_follow_an_unproven_dtcm_mapping(tmp_path,monkeypatch):
    snap=execute(tmp_path,monkeypatch,[],base=0x43000000)
    assert snap['stop']['reason']=='dtcm_reset_record_mapping_unmodeled'


@pytest.mark.parametrize('change',[{'size':29},{'size':0},{'address':0x42000600},
    {'fill':255},{'fill':False},{'profile':'watchdog'},{'extra':0}])
def test_host_rejects_wider_or_different_inputs(change):
    with pytest.raises(ValueError):reset_record_input({**PROFILE,**change})
