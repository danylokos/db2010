"""The production producer distinguishes synthetic API entries from ROM code."""
from support.guarded import run as guarded_run
from db2010.diagnostics.artifacts import artifact as artifact_path
import json
import struct
import pytest
from pathlib import Path

import pyarrow.parquet as pq

from db2010.execution import runner
from db2010.paths import ROOT
from support.fixtures import freeze_state
from support.kgen_fixtures import state


@pytest.mark.parametrize("kind", ["init", "final"])
def test_synthetic_api_observation_is_explicit_and_not_a_rom_instruction(tmp_path,monkeypatch,kind):
    def prepare(root,spec,output):
        if kind=='init':s=state('init')
        else:
            from support.kgen_final_fixtures import state as final_state
            from support.kgen_candidate import initialize
            s=final_state(initialize());s['regions'][0]['seeds'][0]['data']=struct.pack('<HH',0x4790,0xe7fe)
        return freeze_state(s,output),{'seconds':10}
    monkeypatch.setattr(runner,'prepare',prepare)
    output=tmp_path/'run'
    result,failure=guarded_run(ROOT,Path('unused'),output, diagnostics=True, trace=True)
    assert failure is None
    assert result['snapshot']['synthetic_kgen']['init_calls' if kind=='init' else 'final_calls']==1
    profile='sha1-le92-init-update-8040' if kind=='init' else 'sha1-le92-init-update-final-8040'
    assert result['producer']['synthetic_api_observations']==1
    assert result['producer']['synthetic_kgen_profile']==profile
    pcs=pq.read_table(artifact_path(output, 'trace.parquet'),columns=['pc']).column('pc').to_pylist()
    caller=0x44d9f9ce if kind=='init' else 0x44d9fa92
    assert pcs and all(pc in (caller,caller+2) for pc in pcs)
    assert result['memory_producer']['events']==0  # API effects are not invented bus stores.
    command=json.loads((artifact_path(output, 'command.json')).read_text())
    assert any(('synthetic-kgen='+profile) in part for part in command)


def test_synthetic_etx_status_effect_is_not_a_rom_instruction_or_cpu_store(tmp_path,monkeypatch):
    from support.etx_fixtures import state as etx_state
    def prepare(root,spec,output):
        return freeze_state(etx_state(),output),{'seconds':10}
    monkeypatch.setattr(runner,'prepare',prepare)
    output=tmp_path/'run';result,failure=guarded_run(ROOT,Path('unused'),output, diagnostics=True, trace=True)
    assert failure is None and result['snapshot']['synthetic_etx']['completed_calls']==1
    assert result['snapshot']['device_registers'][str(0xf9030004)]==1
    assert result['producer']['synthetic_api_observations']==result['producer']['synthetic_etx_observations']==1
    assert result['producer']['synthetic_etx_profile']=='gdfs-static-8040-status-success'
    pcs=pq.read_table(artifact_path(output, 'trace.parquet'),columns=['pc']).column('pc').to_pylist()
    assert pcs and all(pc in (0x4490d1ae,0x4490d1b0)for pc in pcs)
    assert result['memory_producer']['events']==0
    command=json.loads((artifact_path(output, 'command.json')).read_text())
    assert any('synthetic-etx=gdfs-static-8040-status-success'in part for part in command)
