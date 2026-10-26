"""Clock selection is MMIO driven and survives native migration and UI sampling."""
from support.guarded import run as guarded_run
import copy
import json
import time

import pytest

from support.cpu_clock_fixtures import configured, select, run
from test_native_checkpoint import engine, stopped, migration, command, comparable


def test_native_clock_selection_guards_and_reset(tmp_path):
    assert run(tmp_path)['passed'] >= 20


def test_runner_retains_selected_clock_without_exposing_it_in_views(tmp_path, monkeypatch):
    from io import StringIO
    from db2010.execution import runner
    from db2010.paths import ROOT
    from db2010.ui.browser import Browser
    from db2010.ui.console import Console
    from db2010.ui.observer import Observers
    from support.fixtures import freeze_state
    from test_console import Terminal
    monkeypatch.setenv('TERM', 'xterm')
    def prepare(root, spec, output, **options):
        return freeze_state(configured(select(104)), output), {'seconds':5}
    monkeypatch.setattr(runner, 'prepare', prepare)
    view = Browser(0)
    status = Terminal()
    console = Console(StringIO(), status)
    try:
        result, failure = guarded_run(ROOT, tmp_path/'unused', seconds=5,
                                     console=Observers(console, view))
        assert failure is None
        assert result['snapshot']['cpu_hz'] == 104000000
        assert console.sample['cpu_hz'] == 104000000
        console.finish(result)
        view.finish(result, failure)
        assert 'CPU' not in status.getvalue()
        assert 'cpu_hz' not in view.status
    finally:
        view.close()


@pytest.mark.parametrize('prefix', [6, 9, 15, 21, 30, 36])
def test_restore_clock_selection_and_transition(tmp_path, prefix):
    state = configured(select(104)+select(52)+select(13))
    state['record_diagnostics'] = False
    with engine(state, tmp_path/'whole') as vm:
        whole = stopped(vm)
    first = copy.deepcopy(state)
    first['instruction_limit'] = prefix
    checkpoint = tmp_path/'clock.vmstate'
    with engine(first, tmp_path/'prefix') as vm:
        before = stopped(vm)
        assert json.loads(command(vm, 'qom-get', {'path':'/machine', 'property':'progress'}))['cpu_hz'] == before['cpu_hz']
        migration(vm, 'migrate', checkpoint)
    resumed = copy.deepcopy(state)
    resumed['instruction_limit'] -= prefix
    with engine(resumed, tmp_path/'resumed', incoming=True) as vm:
        migration(vm, 'migrate-incoming', checkpoint)
        # Observe immediately after load, before cont or any new MMIO.
        initial = vm.snapshot(time.monotonic()+5)
        assert initial['cpu_hz'] == before['cpu_hz']
        assert initial['syscon'] == before['syscon']
        final = stopped(vm)
    assert comparable(final) == comparable(whole)
    assert final['cpu_hz'] == 13000000
