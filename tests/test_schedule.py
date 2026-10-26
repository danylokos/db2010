"""Deterministic host schedules, including the real normal and GDB loop bodies."""
from types import SimpleNamespace

import pytest

from db2010.execution.schedule import Schedule, LCD_INTERVAL, PUBLICATION_INTERVAL
from db2010.execution.qmp import QMP
from db2010.execution.debug import DebugSession, DebugOptions


class Clock:
    now = 0.0
    def __call__(self):
        return self.now
    def sleep(self, delay):
        assert delay >= 0
        self.now += delay


def test_independent_deadlines_and_skipped_overruns():
    clock = Clock()
    schedule = Schedule(clock)
    samples = []
    schedule.add('progress', PUBLICATION_INTERVAL, lambda until: samples.append(('progress', clock())))
    schedule.add('display', LCD_INTERVAL, lambda until: samples.append(('display', clock())))
    for i in range(60):
        clock.now = i/60 + 1e-12
        schedule.run_due(10)
    assert [t for name,t in samples if name == 'progress'] == pytest.approx([0, .25, .5, .75])
    assert len([t for name,t in samples if name == 'display']) == 60
    clock.now = 4.001
    schedule.run_due(10)
    assert samples[-2:] == [('progress', 4.001), ('display', 4.001)]
    assert schedule.next_deadline(10) == pytest.approx(4+1/60)
    before = samples[:]
    schedule.run_due(10)
    assert samples == before  # No catch-up burst.
    assert schedule.next_deadline(4.005) == 4.005


def test_callback_cost_skips_slots_without_delaying_other_subsystems():
    clock = Clock()
    schedule = Schedule(clock)
    samples = []
    def expensive(until):
        samples.append(('display', clock()))
        clock.sleep(.11)
    schedule.add('display', LCD_INTERVAL, expensive)
    schedule.add('progress', PUBLICATION_INTERVAL, lambda until: samples.append(('progress', clock())))
    schedule.run_due(1)
    assert samples == [('display', 0), ('progress', .11)]
    assert schedule.entries['display'][1] == pytest.approx(7/60)
    assert schedule.entries['progress'][1] == .25


@pytest.mark.parametrize('debug', [False, True])
@pytest.mark.parametrize('overrun', [False, True])
def test_both_supervisors_use_60hz_display_4hz_progress_and_keypad(tmp_path, monkeypatch, debug, overrun):
    # Use the actual supervisors with fake process/socket waits. No timing
    # assertions depend on how quickly CI can execute QMP or launch GDB.
    import time
    clock = Clock()
    monkeypatch.setattr(time, 'monotonic', clock)
    monkeypatch.setattr(time, 'sleep', clock.sleep)
    qmp = QMP.__new__(QMP)
    qmp.events = []
    samples = {'progress': [], 'display': [], 'keypad': []}
    def observe(until, progress=None, display=None):
        name = 'progress' if progress else 'display'
        samples[name].append(clock())
        if overrun and display and len(samples['display']) == 1:
            clock.sleep(.08)
    qmp.observe = observe
    schedule = qmp.observations(progress=lambda s: None, display=lambda s: None)
    service = lambda *args: samples['keypad'].append(clock())
    if debug:
        from db2010.execution import debug as module
        process = SimpleNamespace(pid=123, returncode=0,
                                  poll=lambda: 0 if clock() >= .999 else None)
        monkeypatch.setattr(module.subprocess, 'Popen', lambda *a, **kw: process)
        session = DebugSession.__new__(DebugSession)
        session.options = DebugOptions(interactive=True)
        session.executable = 'fake-gdb'
        session.recording = False
        session.output = tmp_path
        session.actions = []
        session.record = {}
        session.save = session.close = lambda: None
        qmp.execute = lambda *a: {}
        session.supervise('fake-stub', qmp, lambda: None, None,
                          observations=schedule, service=service)
    else:
        def receive(until):
            clock.now = until
            if clock() >= .999:
                return {'event': 'STOP'}
            raise TimeoutError
        qmp.receive = receive
        qmp.wait_stop(5, observations=schedule, service=service)
    assert samples['progress'] == pytest.approx([0, .25, .5, .75])
    expected_display = [0]+[i/60 for i in range(5,60)] if overrun else [i/60 for i in range(60)]
    expected_keypad = [.08]+[i*.025 for i in range(4,40)] if overrun else [i*.025 for i in range(40)]
    assert samples['display'] == pytest.approx(expected_display)
    assert samples['keypad'] == pytest.approx(expected_keypad)
