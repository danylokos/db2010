"""Own GDB attachment; keep firmware preparation and supervision in the host."""
from ..diagnostics.artifacts import artifact as artifact_path
from dataclasses import dataclass, field
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import time

from .process_log import ProcessLog
from .schedule import Schedule


@dataclass
class DebugOptions:
    executable: str = 'gdb-multiarch'
    interactive: bool = True
    actions: list = field(default_factory=list)  # Ordered ('command'|'script', value).


class DebugSession:
    def __init__(self, options, output, *, recording=True):
        self.options, self.output, self.recording = options, output, recording
        self.process = None
        executable = shutil.which(str(options.executable))
        if not executable:
            raise FileNotFoundError('ARM debugger unavailable; install gdb-multiarch or supply --gdb PATH')
        self.executable = str(Path(executable).resolve())
        def probe(*arguments):
            return subprocess.check_output([self.executable, '-nx', '-nh', *arguments],
                                           text=True, stderr=subprocess.STDOUT, timeout=10)
        self.record = {'mode': 'interactive' if options.interactive else 'batch',
                       'intervention': True, 'actions': [],
                       'scope': 'Debugger-controlled execution; not a canonical baseline run. Guest guards remain active.'}
        if recording:
            with Path(self.executable).open('rb') as file:
                digest = hashlib.file_digest(file, 'sha256').hexdigest()
            self.record.update(executable=self.executable, sha256=digest,
                version=probe('--version'), configuration=probe('--configuration'),
                arm_support=probe('-batch', '-ex', 'set architecture arm', '-ex', 'show architecture'),
                interactive_capture='GDB output log and saved command history; not a complete terminal recording.')
        self.actions = []
        for index, (kind, value) in enumerate(options.actions):
            if kind == 'command':
                self.actions.extend(['-ex', value])
                self.record['actions'].append({'kind': kind, 'value': value})
            elif kind == 'script':
                source = Path(value).resolve()
                copied = artifact_path(output, f'gdb-script-{index:03d}.gdb', create=True)
                shutil.copyfile(source, copied)
                self.actions.extend(['-x', str(copied)])
                self.record['actions'].append({'kind': kind, 'source': str(source), 'artifact': copied.relative_to(output).as_posix()})
            else:
                raise ValueError('unknown debugger action')
        self.save()

    def save(self):
        if self.recording:
            (artifact_path(self.output, 'debug-session.json', create=True)).write_text(json.dumps(self.record, indent=2)+'\n')

    def supervise(self, stub, qmp, health, limit, observations=None, interrupt_exits=False, service=None):
        command = [self.executable, '-nx', '-nh', '-iex', 'set auto-load off']
        if not self.options.interactive:
            command.append('-batch')
        setup = ['set pagination off', 'set confirm off', 'set architecture arm', 'set endian little',
                 'set remotetimeout 5', 'set history save off']
        if self.recording:
            setup.extend(['set logging file '+str(artifact_path(self.output, 'gdb-output.log', create=True)),
                          'set logging overwrite on', 'set logging enabled on',
                          'set history filename '+str(artifact_path(self.output, 'gdb-history', create=True)),
                          'set history save on', 'set history size unlimited'])
        setup.append('target remote '+str(stub))
        setup.extend(['define hook-quit', 'if $_inferior_thread_count > 0',
                      'disconnect', 'end', 'end'])
        for option, value in zip(self.actions[::2], self.actions[1::2], strict=True):
            # GDB's source command consumes the remainder as a filename;
            # quotation marks would become literal filename characters.
            setup.append('source '+str(value) if option == '-x' else value)
        if not self.options.interactive:
            setup.extend(['if $_inferior_thread_count > 0', 'disconnect', 'end'])
        script = artifact_path(self.output, 'gdb-session.gdb', create=True)
        script.write_text('\n'.join(setup)+'\n')
        # A single command file aborts on command errors. Separate -ex options
        # continue after errors and can conceal a failed attach/setup in batch.
        command.extend(['-x', str(script)])
        self.record.update(command=command, wall_limit_seconds=limit,
                           timeout_policy='Entire debugger session; interactive has no idle deadline unless --seconds is supplied.')
        self.save()
        # The child has its own process group. Parent terminal interrupts are
        # forwarded to GDB, whose remote protocol interrupts the guest normally.
        logs = ExitStack()
        log = None
        try:
            if not self.options.interactive:
                log = logs.enter_context(ProcessLog(artifact_path(self.output, 'gdb-console.log', create=True) if self.recording else None))
            self.process = subprocess.Popen(command, stdin=None if self.options.interactive else subprocess.DEVNULL,
                stdout=None if self.options.interactive else subprocess.PIPE,
                stderr=subprocess.STDOUT, start_new_session=True,
                env={**os.environ, 'GDBHISTFILE':str(artifact_path(self.output, 'gdb-history', create=True)) if self.recording else os.devnull, 'GDBHISTSIZE':'-1'})
            if log:
                log.start(self.process.stdout)
            self.record['pid'] = self.process.pid
            self.save()
            deadline = None if limit is None else time.monotonic()+limit
            schedule = observations if observations is not None else Schedule()
            schedule.add('debug-status', 0.05,
                         lambda until: qmp.execute('query-status', min(until, time.monotonic()+2)))
            if service:
                schedule.add('keypad', 0.025,
                             lambda until: service(qmp, min(until, time.monotonic()+2)))
            while self.process.poll() is None:
                try:
                    health()
                    if log:
                        log.check()
                    if deadline is not None and time.monotonic() >= deadline:
                        raise TimeoutError('GDB session deadline exceeded')
                    # Same observation deadlines as normal execution, including
                    # while GDB has the guest stopped. QMP stays on this thread.
                    until = min(deadline if deadline is not None else float('inf'), time.monotonic()+2)
                    schedule.run_due(until)
                    time.sleep(schedule.delay(deadline if deadline is not None else float('inf')))
                except KeyboardInterrupt:
                    if not self.options.interactive or interrupt_exits:
                        raise
                    self.process.send_signal(signal.SIGINT)
            self.record['returncode'] = self.process.returncode
            if self.process.returncode:
                raise RuntimeError(f'GDB exited with status {self.process.returncode}')
        finally:
            try:
                self.close()
            finally:
                logs.close()
                if log and self.process is not None and self.process.returncode:
                    self.record['error_context'] = log.text()
                    self.save()

    def close(self):
        if self.process is not None:
            if self.process.poll() is None:
                # A script may run shell children; terminate the whole owned
                # debugger group before closing QEMU and capture sockets.
                os.killpg(self.process.pid, signal.SIGTERM)
                try:
                    self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    os.killpg(self.process.pid, signal.SIGKILL)
                    self.process.wait()
                self.record['terminated_by_runner'] = True
            self.record['returncode'] = self.process.returncode
        self.save()
