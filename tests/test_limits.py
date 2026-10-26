"""Explicit CLI budgets, uncapped execution and frozen configuration provenance."""
from support.guarded import run as guarded_run
from db2010.paths import DEFAULT_EXPERIMENT
import sys
from support.paths import RESOURCES
from db2010.diagnostics.artifacts import artifact as artifact_path, RunArtifacts
import json
from pathlib import Path
import shlex
import os
import select
import signal
import subprocess
import time

import pytest

from db2010 import cli
from db2010.execution import runner
from db2010.paths import ROOT
from test_runner import synthetic_preparation
from db2010.diagnostics.retention import regeneration_command


@pytest.mark.parametrize('extra', [[], ['--limit', '12'], ['--seconds', '2']])
def test_cli_does_not_inherit_configured_limits(tmp_path, monkeypatch, extra):
    captured = []

    def run(*args, **options):
        captured.append(options)
        return {'success': True, 'phases': {}, 'uart': {}}, None

    monkeypatch.setattr(cli, 'run', run)
    monkeypatch.setattr('sys.argv', ['db2010', '--research-guards', '--diagnostics', '--artifacts-dir', str(tmp_path), '--label', 'run', *extra])
    cli.main()
    assert captured[0]['config_limits'] is False
    assert captured[0]['bounded_history'] is True
    assert captured[0]['instruction_limit'] == (12 if '--limit' in extra else None)
    assert captured[0]['seconds'] == (2 if '--seconds' in extra else None)


@pytest.mark.parametrize('extra', [['--limit', '0'], ['--limit', '-1'], ['--limit', str(2**64)],
                                  ['--seconds', '0'], ['--seconds', 'nan'], ['--seconds', 'inf']])
def test_invalid_limits_fail_before_output_creation(tmp_path, monkeypatch, extra):
    output=tmp_path/'run'
    monkeypatch.setattr('sys.argv', ['db2010', '--research-guards', '--diagnostics', '--artifacts-dir', str(output.parent), '--label', output.name, *extra])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2 and not output.exists()


def test_native_uncapped_count_uses_only_explicit_wall_timeout(tmp_path, monkeypatch):
    def prepare(root, experiment, output):
        path, spec=synthetic_preparation(root,experiment,output)
        state=json.loads(path.read_text());state['instruction_limit']=3
        path.write_text(json.dumps(state)+'\n')
        return path,spec
    monkeypatch.setattr(runner,'prepare',prepare)
    result,failure=guarded_run(ROOT,Path('unused'),tmp_path/'run', diagnostics=True,trace=False,
                              config_limits=False,seconds=0.1)
    assert isinstance(failure,TimeoutError)
    assert result['snapshot']['observations']>3
    assert result['execution_limits']=={'instruction_limit':None,'seconds':0.1,'configured_budgets_used':False}
    assert 'instruction_limit' not in json.loads((artifact_path((tmp_path) / 'run', 'state.json')).read_text())
    assert result['qemu_returncode'] is not None


def test_native_explicit_count_overrides_prepared_budget_without_wall_cap(tmp_path,monkeypatch):
    monkeypatch.setattr(runner,'prepare',synthetic_preparation)
    result,failure=guarded_run(ROOT,Path('unused'),tmp_path/'run', diagnostics=True,trace=False,
                              config_limits=False,instruction_limit=3)
    assert failure is None and result['snapshot']['stop']['reason']=='instruction_limit'
    assert result['snapshot']['stop']['observations']==3
    assert result['execution_limits']=={'instruction_limit':3,'seconds':None,'configured_budgets_used':False}


@pytest.mark.parametrize('research_guards', [False, True])
def test_uncapped_cli_interrupt_preserves_stopped_state_and_exit130(tmp_path, research_guards):
    output=tmp_path/'interrupted'
    process=subprocess.Popen([sys.executable,'-B','-m','db2010',
                              *(['--research-guards'] if research_guards else []),
                              '--experiment',str(DEFAULT_EXPERIMENT),'--uart-debug','--diagnostics','--artifacts-dir',str(output.parent),'--label',output.name],cwd=ROOT,
                             stdout=subprocess.PIPE,stderr=subprocess.PIPE,start_new_session=True)
    try:
        # Wait for genuine live UART so the signal tests active execution,
        # rather than interrupting preprocessing or a debugger prompt.
        deadline=time.monotonic()+20
        while not select.select([process.stdout],[],[],0.1)[0]:
            assert process.poll() is None and time.monotonic()<deadline
        assert os.read(process.stdout.fileno(),4096)
        process.send_signal(signal.SIGINT)
        _,stderr=process.communicate(timeout=15)
        assert process.returncode==130
        result=json.loads((artifact_path(output, 'result.json')).read_text())
        assert result['snapshot']['research_guards'] is research_guards
        assert result['error']['type']=='KeyboardInterrupt' and result['snapshot']['observations']>0
        assert result['qemu_returncode']==0 and (output/'artifacts.sha256.json').exists()
        assert result['execution_limits']['instruction_limit'] is None
        assert result['execution_limits']['seconds'] is None
        assert result['recording_history']['profile']=='startup64-recent64-irq512'
        assert b'Interrupted' in stderr and b'"success"' not in stderr
    finally:
        if process.poll() is None:
            # Let the runner reap its separately supervised QEMU group even
            # when a synchronization assertion fails before the normal signal.
            process.send_signal(signal.SIGINT)
            try:
                process.communicate(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid,signal.SIGKILL)
                process.wait()


@pytest.mark.parametrize('traced',[False,True])
def test_retention_regeneration_preserves_effective_limits(tmp_path,traced):
    (artifact_path(tmp_path, 'experiment.json', create=True)).write_text(json.dumps({'instruction_limit':320000000,'seconds':300}))
    (artifact_path(tmp_path, 'state.json', create=True)).write_text(json.dumps({'instruction_limit':320000000}))
    result={'execution_limits':{'instruction_limit':12,'seconds':2}}
    result['trace_enabled']=traced
    (artifact_path(tmp_path, 'result.json', create=True)).write_text(json.dumps(result))
    context=RunArtifacts(tmp_path, families=['diagnostics']+(['trace'] if traced else []), destination=tmp_path/'run-manifest')
    context.manifest['limits']=result['execution_limits']
    (tmp_path/'run.json').write_text(json.dumps(context.manifest))
    command=shlex.split(regeneration_command(tmp_path))
    assert ('--trace' in command)==traced
    assert command[command.index('--limit')+1]=='12'
    assert command[command.index('--seconds')+1]=='2'
    result['execution_limits']={'instruction_limit':None,'seconds':None}
    context.manifest['limits']=result['execution_limits']
    (tmp_path/'run.json').write_text(json.dumps(context.manifest))
    (artifact_path(tmp_path, 'result.json', create=True)).write_text(json.dumps(result))
    command=shlex.split(regeneration_command(tmp_path))
    assert '--limit' not in command and '--seconds' not in command
