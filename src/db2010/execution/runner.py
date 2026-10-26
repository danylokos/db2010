"""Own and supervise one QEMU process, sockets and finalized run evidence."""
from ..diagnostics.artifacts import artifact as artifact_path, RunArtifacts, prepared_references, external_inputs
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, nullcontext
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
import time

from ..preparation.inputs import prepare
from ..paths import DEFAULT_EXPERIMENT, RESOURCES, emulator_build, qemu_source
from .qmp import QMP
from .flash import load_flash_images, save_flash_images, validate_storage
from .checkpoint import (source_run, load_checkpoint, copy_starting_inputs,
                         migrate_file, save_checkpoint, model_identity, starting_source)
from ..diagnostics.trace import TraceCollector
from ..diagnostics.memory import MemoryCollector, audit
from .debug import DebugSession
from .process_log import ProcessLog
from .audio import AudioCapture
from ..diagnostics.recording import save_diagnostics, save_provenance
from ..ui.observer import Observer
from ..ui.keypad import KeypadService
from .serial import SerialLink


def file_identity(path):
    with path.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    return {'size_bytes': path.stat().st_size, 'sha256': digest}


def run(root, experiment=None, output=None, *, trace=False, disassemble=False, seconds=None,
        instruction_limit=None, config_limits=False, bounded_history=False, debug=None, console: Observer | None = None,
        images=None, engine_binary=None, keypad_input=None, diagnostics=False,
        artifacts_dir=None, label=None, snapshot=False, restore=None, replay=None,
        allow_engine_mismatch=False, record_audio=False, serial_link=None, serial_service=None,
        save_flash=False, load_flash=None, nand_main=None, nand_spare=None, uart_debug=False,
        input_root=None, research_guards=False):
    trace = trace or disassemble
    families = (({'audio'} if record_audio else set()) | ({'diagnostics'} if diagnostics else set()) | ({'trace'} if trace else set())
                | ({'snapshot'} if snapshot else set()) | ({'flash'} if save_flash else set()))
    recording = diagnostics
    saving = bool(families)
    artifacts = None
    output_ready = False
    resources = ExitStack()
    started = time.perf_counter()
    result = {'format_version': 1, 'machine': 'db2010', 'created_utc': datetime.now(timezone.utc).isoformat(),
              'trace_enabled': trace, 'success': False, 'uart': {}, 'phases': {}}
    result['uart_ports'] = [0xFB060000, 0xFC040000, 0xFC030000, 0xFB040000]
    failure = None
    process = qmp = executor = None
    listeners, connections, futures = [], [], []
    connection_lock = threading.Lock()
    collector = memory_collector = debugger = engine_log = None
    input_service = None
    audio_capture = None
    audio_stopping = threading.Event()
    if keypad_input:
        keypad_input.enable(False)
    uart_counts = [0]*4
    uart_baseline = [0]*4
    attempted_families = set()
    def save_stopped_families():
        errors = []
        for family, enabled, save in (('snapshot', snapshot, save_checkpoint),
                                       ('flash', save_flash, save_flash_images)):
            if enabled and family not in attempted_families:
                attempted_families.add(family)
                try:
                    save(qmp, artifacts, result, canonical)
                except Exception as exc:
                    result.setdefault('artifact_errors', {})[family] = {
                        'type': type(exc).__name__, 'message': str(exc)}
                    errors.append(f'{family}: {exc}')
        if errors:
            raise RuntimeError('; '.join(errors))
    source = source_spec = None
    try:
        if type(research_guards) is not bool:
            raise ValueError('research_guards must be a boolean')
        if instruction_limit is not None and not research_guards:
            raise ValueError('instruction_limit requires --research-guards')
        result['research_guards'] = research_guards
        if uart_debug and (restore is not None or replay is not None):
            raise ValueError('debug UART selection requires a cold boot; restore/replay preserves its saved serial mode')
        if uart_debug and serial_link is not None:
            raise ValueError('debug UART logging and OBEX serial transport are mutually exclusive')
        images = dict(images or {})
        for name, value in (('nand-main', nand_main), ('nand-spare', nand_spare)):
            if value is not None:
                if name in images:
                    raise ValueError(f'duplicate image override: {name}')
                images[name] = value
        flash_lineage = None
        if load_flash is not None:
            if images or restore is not None or replay is not None:
                raise ValueError('load_flash cannot combine with image inputs, restore or replay')
            flash_source = source_run(load_flash, artifacts_dir or root/'artifacts')
            images = load_flash_images(flash_source)
            flash_lineage = {'mode': 'load-flash', 'source': str(flash_source),
                             'manifest': file_identity(flash_source/'flash/flash.json')}
        experiment = experiment if experiment is not None else DEFAULT_EXPERIMENT
        if replay is not None and restore is not None:
            raise ValueError('replay cannot override the saved start mode')
        if allow_engine_mismatch and restore is None and replay is None:
            raise ValueError('allow_engine_mismatch requires restore or replay')
        binary = Path(engine_binary) if engine_binary is not None else qemu_source(root)/'build/qemu-system-arm'
        if replay is not None:
            source = source_run(replay, artifacts_dir or root/'artifacts')
            restore = starting_source(source)
        if restore is not None:
            if images:
                raise ValueError('restore cannot override starting images')
            source = source_run(restore, artifacts_dir or root/'artifacts')
            validating = time.perf_counter()
            source_spec = load_checkpoint(source, binary, allow_engine_mismatch=allow_engine_mismatch,
                                          research_guards=research_guards)
            result['phases']['checkpoint_validation_seconds'] = time.perf_counter()-validating
            installed_engine = file_identity(binary)
            result['checkpoint_compatibility'] = {
                'allow_engine_mismatch': allow_engine_mismatch,
                'source_engine': source_spec['engine'], 'installed_engine': installed_engine,
                'engine_mismatch': source_spec['engine'] != installed_engine}
        lineage = flash_lineage if source is None else {
            'mode': 'restore',
            'source_id': json.loads((source/'run.json').read_text())['id'],
            'source_engine': source_spec['engine'],
            'source_run_manifest': file_identity(source/'run.json'),
            'source_checkpoint': file_identity(source/Path(source_spec['vmstate']).parent/'snapshot.json')}
        artifacts = RunArtifacts(root, families=families, destination=output,
                                 artifacts_dir=artifacts_dir, label=label, lineage=lineage)
        resources.callback(artifacts.close)
        if artifacts.manifest is not None:
            artifacts.manifest['research_guards'] = research_guards
            artifacts.manifest['emulator_root'] = str(Path(root).resolve())
            artifacts.manifest['input_root'] = str(Path(input_root or root).resolve())
        output = artifacts.path
        result['run_directory'] = str(output) if saving else None
        output_ready = True
        if console:
            console.phase('prepare')
        if debug is not None:
            debug_output = output if recording else Path(resources.enter_context(
                tempfile.TemporaryDirectory(prefix='db2010-debug-')))
            debugger = DebugSession(debug, debug_output, recording=recording)
            result['debug_session'] = True
        options = {'images': images} if images else {}
        if input_root is not None:
            options['input_root'] = Path(input_root).resolve()
        if not saving:
            options['recording'] = False
        if source is not None:
            state, spec = copy_starting_inputs(source, output, source_spec)
            if artifacts.manifest is not None:
                artifacts.manifest['starting_checkpoint'] = {
                    'path': str(source/Path(source_spec['vmstate']).parent/'snapshot.json'),
                    **file_identity(source/Path(source_spec['vmstate']).parent/'snapshot.json')}
            result['debugger_lineage'] = json.loads((source/'run.json').read_text()).get('debugger_controlled', False)
        else:
            state, spec = prepare(root, experiment, output, **options)
        if saving and not artifact_path(output, 'experiment.json').exists():
            artifact_path(output, 'experiment.json', create=True).write_text(json.dumps(spec)+'\n')
        if images and saving:
            result['image_overrides'] = json.loads((artifact_path(output, 'inputs.json')).read_text())['cli_image_overrides']
        prepared = json.loads(state.read_text())
        if uart_debug:
            if prepared.get('serial_lines', {}).get('profile') != 'port1-rx-levels':
                raise ValueError('debug UART selection requires the port1-rx-levels serial input profile')
            # Reuse the verified debug-cable inputs, leaving firmware to enable
            # logging itself. UART0 has one owner: debug output or AT/OBEX.
            prepared['serial_lines'] = {'profile': 'port1-rx-levels',
                                        'uart0_rx_level': 1, 'uart1_rx_level': 0}
            prepared.pop('serial_transport', None)
            # The data UART replaced two exact-register owners in 0260. Restore
            # their frozen 0255 contracts when switching back to polled debug.
            registers = json.loads((RESOURCES/'uart-debug-registers.json').read_text())
            addresses = {entry['address'] for entry in registers}
            prepared['device_registers'] = [entry for entry in prepared.get('device_registers', [])
                                            if entry['address'] not in addresses] + registers
            result['uart_debug'] = True
            if artifacts.manifest is not None:
                artifacts.manifest['uart_debug'] = True
        if save_flash:
            validate_storage(prepared)
        if serial_service is not None and 'serial_transport' in prepared:
            if serial_link is not None:
                raise ValueError('use one serial peer per runner')
            serial_link = SerialLink()
            serial_service.attach(serial_link)
        if serial_link is not None and 'serial_transport' not in prepared:
            raise ValueError('serial link requires an explicit serial transport profile')
        prepared['research_guards'] = research_guards
        prepared['record_diagnostics'] = recording
        result['diagnostics_enabled'] = recording
        result['timebase'] = prepared.get('timebase')
        # Preserve the frozen experiment. The CLI opts out of inherited research
        # budgets; the sealed prepared state records its explicit count override.
        if instruction_limit is not None or not config_limits:
            if instruction_limit is not None and (type(instruction_limit) is not int
                    or not 0 < instruction_limit <= 2**64-1):
                raise ValueError('instruction limit must be a positive uint64')
            if instruction_limit is None:
                prepared.pop('instruction_limit', None)
            else:
                prepared['instruction_limit'] = instruction_limit
        if not research_guards and 'instruction_limit' in prepared:
            raise ValueError('configured instruction_limit requires --research-guards; use config_limits=False for fast execution')
        if bounded_history:
            prepared['recording_history'] = {'profile': 'startup64-recent64-irq512'}
        state.write_text(json.dumps(prepared, indent=2)+'\n')
        result['phases']['prepare_seconds'] = time.perf_counter()-started
        limit = spec['seconds'] if config_limits and seconds is None else seconds
        if limit is not None and (not math.isfinite(limit) or limit <= 0):
            raise ValueError('wall-clock limit must be finite and positive')
        if debug and debug.interactive and seconds is None:
            limit = None
        result['execution_limits'] = {
            'instruction_limit': prepared.get('instruction_limit'),
            'seconds': limit,
            'configured_budgets_used': config_limits,
        }
        if bounded_history:
            result['recording_history'] = prepared['recording_history']
        plugin = emulator_build(root)/'plugins/trace.so'
        if saving:
            result['binary'] = file_identity(binary)
            build_identity = None
            for candidate in sorted((emulator_build(root)/'provenance/qemu').glob('*/build.json'), reverse=True):
                manifest = json.loads(candidate.read_text())
                if manifest['binary_sha256'] == result['binary']['sha256']:
                    build_identity = manifest
                    shutil.copyfile(candidate, artifact_path(output, 'qemu-build.json', create=True))
                    break
            if build_identity is None:
                raise ValueError('QEMU binary lacks matching build provenance')
            result['engine_source_commit'] = build_identity['source_commit']
            if trace:
                result['plugin'] = file_identity(plugin)
                plugin_identity = None
                for candidate in sorted((emulator_build(root)/'provenance/trace').glob('*/build.json'), reverse=True):
                    manifest = json.loads(candidate.read_text())
                    if (manifest['binary_sha256'] == result['plugin']['sha256']
                            and manifest.get('engine_binary_sha256') == result['binary']['sha256']):
                        plugin_identity = manifest
                        shutil.copyfile(candidate, artifact_path(output, 'trace-build.json', create=True))
                        shutil.copyfile(candidate.parent/manifest['source_file'],
                                        artifact_path(output, 'trace-producer.c', create=True))
                        break
                if plugin_identity is None:
                    raise ValueError('trace producer lacks matching build provenance')
                if file_identity(artifact_path(output, 'trace-producer.c'))['sha256'] != plugin_identity['source_sha256']:
                    raise ValueError('trace producer source differs from matched build provenance')
        with tempfile.TemporaryDirectory(prefix='db2010-run-') as temporary:
            temporary = Path(temporary)
            qmp_path = temporary/'qmp'
            trace_path = temporary/'trace'
            uart_paths = [temporary/f'uart{i}' for i in range(4)]
            audio_path = temporary/'audio'
            audio_enabled = prepared.get('dsp_peer', {}).get('profile') in (
                'wire20-modexp1024-mma-stream-peer', 'wire20-modexp1024-mma-midi-setup-peer',
                'wire20-modexp1024-mma-midi-peer')
            gdb_path = temporary/'gdb'
            # Interactive idle is unlimited by default, so capture sockets must
            # not time out while the debugger sits at its prompt.
            capture_timeout = None if limit is None or (debug and debug.interactive and seconds is None) else max(limit, 30)
            executor = ThreadPoolExecutor(max_workers=7, thread_name_prefix='db2010-capture')
            # Contained proofs stay relative; external images keep absolute paths.
            # Session endpoints and launch configuration remain temporary.
            canonical = prepared_references(prepared, output, absolute=False)
            state.write_text(json.dumps(canonical, indent=2)+'\n')
            if artifacts.manifest is not None:
                artifacts.manifest['external_inputs'] = external_inputs(canonical, output)
            launch_state = temporary/'state.json'
            launch_configuration = prepared_references(canonical, output, absolute=True)
            launch_configuration['checkpoint_model_identity'] = model_identity(canonical, output)
            if restore is not None:
                launch_configuration['checkpoint_tcm'] = source_spec['tcm_layout']
            launch_state.write_text(json.dumps(launch_configuration)+'\n')
            command = [str(binary), '-machine', f'db2010,state={launch_state},research-guards={"on" if research_guards else "off"}', '-accel', 'tcg,thread=single',
                       '-icount', 'shift=6,sleep=on,align=on',
                       '-nodefaults', '-display', 'none', '-monitor', 'none', '-serial', 'none',
                       '-S', '-qmp', f'unix:{qmp_path},server=on,wait=off']
            if restore is not None:
                command.extend(['-incoming', 'defer'])
            if debugger:
                command.extend(['-chardev', f'socket,path={gdb_path},server=on,wait=off,id=gdb',
                                '-gdb', 'chardev:gdb'])
            if trace:
                listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                listener.settimeout(30)
                listener.bind(str(trace_path));listener.listen(1)
                listeners.append(listener)
                collector = TraceCollector(output, disassemble=disassemble)
                future = executor.submit(collector.collect, listener, connections, connection_lock, capture_timeout)
                futures.append(future)
                memory_path = temporary/'memory'
                listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                listener.settimeout(30)
                listener.bind(str(memory_path));listener.listen(1)
                listeners.append(listener)
                memory_collector = MemoryCollector(output)
                futures.append(executor.submit(memory_collector.collect, listener, connections, connection_lock, capture_timeout))
                synthetic_trace = ',synthetic-kgen='+prepared['synthetic_kgen']['profile'] if 'synthetic_kgen' in prepared else ''
                if 'synthetic_sha' in prepared:
                    synthetic_trace += ',synthetic-sha='+prepared['synthetic_sha']['profile']
                if 'synthetic_etx' in prepared:
                    synthetic_trace += ',synthetic-etx='+prepared['synthetic_etx']['profile']
                command.extend(['-plugin', f'file={plugin},socket={trace_path},metrics={artifact_path(output, "producer.json", create=True)},'
                                f'memsocket={memory_path},memmetrics={artifact_path(output, "memory-producer.json", create=True)}'+synthetic_trace])
            for i, path in enumerate(uart_paths):
                command.extend(['-chardev', f'socket,id=uart{i},path={path},server=on,wait=off'])
                if recording:
                    (artifact_path(output, f'uart{i}.bin', create=True)).touch()
            if audio_enabled:
                command.extend(['-chardev', f'socket,id=audio,path={audio_path},server=on,wait=off'])
                audio_capture = AudioCapture(output/'audio' if record_audio else None,
                    getattr(console, 'audio', None) if console else None)
            elif record_audio:
                raise ValueError('selected model does not provide a PCM audio stream')
            if saving:
                launch_template = [item.replace(str(output)+'/', '').replace(str(temporary), '${SESSION}')
                                   for item in command]
                launch_template[0] = '${QEMU_ENGINE}'
                launch_template = [item.replace(str(plugin), '${TRACE_PRODUCER}')
                    .replace('state=${SESSION}/state.json', 'state=config/state.json') for item in launch_template]
                artifact_path(output, 'command.json', create=True).write_text(json.dumps(launch_template, indent=2)+'\n')

            def health():
                if engine_log:
                    engine_log.check()
                if process is not None and process.poll() is not None:
                    raise RuntimeError(f'QEMU exited unexpectedly: {process.returncode}')
                for future in futures:
                    if future.done() and future.exception() is not None:
                        raise RuntimeError(f'capture failed: {future.exception()}') from future.exception()

            def uart_collect(connection, index):
                destination = (artifact_path(output, f'uart{index}.bin', create=True)).open('ab') if recording else nullcontext()
                with connection, destination as file:
                    while True:
                        try:
                            data = connection.recv(65536)
                        except socket.timeout:
                            continue
                        if not data:
                            break
                        uart_counts[index] += len(data)
                        if file:
                            file.write(data)
                        if index == 0 and serial_link is not None:
                            serial_link.feed(data)
                        if console:
                            console.uart(index, data)

            launch = time.perf_counter()
            if console:
                console.phase('start')
            with ProcessLog(artifact_path(output, 'qemu.log', create=True) if recording else None) as engine_log:
                process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
                engine_log.start(process.stdout)
                try:
                    startup_deadline = time.monotonic()+30
                    while (not qmp_path.exists() or not all(path.exists() for path in uart_paths)
                           or (debugger and not gdb_path.exists())
                           or (audio_enabled and not audio_path.exists())):
                        health()
                        if time.monotonic() >= startup_deadline:
                            raise TimeoutError('QEMU startup deadline exceeded')
                        time.sleep(0.005)
                    qmp = QMP(qmp_path, startup_deadline, health)
                    result['qmp_greeting'] = qmp.greeting
                    for i, path in enumerate(uart_paths):
                        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                        connection.settimeout(0.2)
                        connection.connect(str(path))
                        with connection_lock:
                            connections.append(connection)
                        if i == 0 and serial_link is not None:
                            serial_link.attach(connection)
                        futures.append(executor.submit(uart_collect, connection, i))
                    if audio_capture:
                        futures.append(executor.submit(audio_capture.collect, audio_path, audio_stopping, connections, connection_lock))
                        if not audio_capture.ready.wait(5):
                            raise TimeoutError('audio collector connection deadline exceeded')
                    if restore is not None:
                        restoring = time.perf_counter()
                        migrate_file(qmp, source/source_spec['vmstate'], incoming=True)
                        result['phases']['checkpoint_restore_seconds'] = time.perf_counter()-restoring
                    capture = qmp.snapshot if recording else qmp.summary
                    result['initial_snapshot'] = capture(time.monotonic()+5)
                    uart_baseline = result['initial_snapshot'].get('uart_bytes', [0]*4)
                    result['capture_baseline'] = qmp.summary(time.monotonic()+5)
                    if result['capture_baseline'].get('research_guards') is not research_guards:
                        raise ValueError('engine research guard mode differs from requested mode')
                    guarded_restore = restore is not None and 'stop' in result['capture_baseline']
                    result['phases']['startup_seconds'] = time.perf_counter()-launch
                    executing = time.perf_counter()
                    if keypad_input and 'keypad' in prepared:
                        input_stream = resources.enter_context(artifact_path(output, 'keypad-input.log', create=True).open('w')) if recording else None
                        input_service = KeypadService(keypad_input, input_stream, stop_on_event=debugger is None)
                        keypad_input.take() # discard preparation releases
                        keypad_input.enable(True)
                    artifacts.status('running', result)
                    if console:
                        console.phase('run')
                    deadline = time.monotonic()+limit if limit is not None else math.inf
                    display = console.display if console and getattr(console, 'show_display', False) else None
                    consumer_progress = console.progress if console and console.show_status else None
                    def publish_progress(sample):
                        if serial_service is not None:
                            serial_service.progress(sample)
                        if consumer_progress is not None:
                            consumer_progress(sample)
                    progress = publish_progress if serial_service is not None or consumer_progress is not None else None
                    def service_inputs(qmp, until):
                        if input_service is not None: input_service.poll(qmp, until)
                        if serial_link is not None: serial_link.poll(qmp, until)
                    input_poll = service_inputs if input_service is not None or serial_link is not None else None
                    observations = qmp.observations(progress, display)
                    def observe(until):
                        qmp.observe(until, progress, display)
                    if guarded_restore:
                        result['session_end'] = 'restored_guarded_boundary'
                    elif debugger:
                        debug_limit = None if debug.interactive and seconds is None else limit
                        debugger.supervise(gdb_path, qmp, health, debug_limit,
                                           observations=observations, interrupt_exits=display is not None,
                                           service=input_poll)
                        # GDB detach/quit can resume the target. Explicitly stop
                        # before reading state or shutting down capture.
                        qmp.execute('stop', time.monotonic()+5)
                        result['session_end'] = 'debugger_exit'
                    else:
                        qmp.execute('cont', deadline)
                        qmp.wait_stop(deadline, observations=observations,
                                      service=input_poll)
                    if input_service:
                        input_service.close(qmp, time.monotonic()+5)
                        result['keypad_input_transitions'] = input_service.total
                    result['phases']['emulation_seconds'] = time.perf_counter()-executing
                    ending = time.perf_counter()
                    if console:
                        console.phase('capture')
                    result['snapshot'] = capture(time.monotonic()+5)
                    if progress or display:
                        observe(time.monotonic()+5)
                    result['qmp_status'] = qmp.execute('query-status', time.monotonic()+5)
                    if result['qmp_status']['running'] or (not debugger and 'stop' not in result['snapshot']):
                        raise ValueError('expected stopped model-boundary state')
                    if result['snapshot'].get('stop', {}).get('reason') == 'uart_host_capture_failed':
                        raise RuntimeError('native UART lost its host capture channel')
                    save_stopped_families()
                    # Quit is the normal producer EOF/flush barrier. Health no
                    # longer requires a running process after its acceptance.
                    qmp.health = lambda: None
                    audio_stopping.set()
                    qmp.execute('quit', time.monotonic()+5)
                    process.wait(timeout=10)
                    if process.returncode:
                        raise RuntimeError(f'QEMU exit {process.returncode}')
                    for future in futures:
                        future.result(timeout=10)
                    if 'uart_bytes' in result['snapshot']:
                        if [now-base for now,base in zip(result['snapshot']['uart_bytes'],uart_baseline)] != uart_counts:
                            raise ValueError('native UART/host capture byte count differs')
                    result['phases']['shutdown_seconds'] = time.perf_counter()-ending
                    if trace:
                        metrics = json.loads((artifact_path(output, 'producer.json')).read_text())
                        if metrics['observations'] != collector.rows:
                            raise ValueError('producer/Parquet row count differs')
                        result['producer'] = metrics
                        result['collector'] = collector.metrics
                        metrics = json.loads((artifact_path(output, 'memory-producer.json')).read_text())
                        if metrics['events'] != memory_collector.rows:
                            raise ValueError('memory producer/Parquet count differs')
                        result['memory_producer'] = metrics
                        result['memory_collector'] = memory_collector.metrics
                        result['display_audit'] = audit(output)
                    result['success'] = True
                except BaseException as exc:
                    failure = exc
                    result['error'] = {'type': type(exc).__name__, 'message': str(exc)}
                    # Try to retain a stopped CPU state; force process-group
                    # termination if QMP/producer backpressure prevents it.
                    if qmp is not None and process.poll() is None:
                        try:
                            qmp.health = lambda: None
                            qmp.execute('stop', time.monotonic()+2)
                            if input_service:
                                input_service.close(qmp, time.monotonic()+2)
                            capture = qmp.snapshot if recording else qmp.summary
                            result['snapshot'] = capture(time.monotonic()+2)
                            if console and getattr(console, 'show_display', False):
                                console.phase('capture')
                                qmp.observe(time.monotonic()+2, console.progress, console.display)
                            save_stopped_families()
                            qmp.execute('quit', time.monotonic()+2)
                            process.wait(timeout=2)
                        except BaseException:
                            result['forced_termination'] = True
                finally:
                    audio_stopping.set()
                    if serial_link is not None:
                        serial_link.close()
                    if process.poll() is None:
                        os.killpg(process.pid, signal.SIGKILL);process.wait()
                    result['qemu_returncode'] = process.returncode
                    if qmp:
                        result['qmp_events'] = qmp.events
                        qmp.close()
                    for listener in listeners:
                        try:
                            listener.shutdown(socket.SHUT_RDWR)
                        except OSError:
                            pass
                        listener.close()
                    with connection_lock:
                        for connection in connections:
                            try:
                                connection.shutdown(socket.SHUT_RDWR)
                            except OSError:
                                pass
                    for future in futures:
                        try:
                            future.result(timeout=5)
                        except BaseException as exc:
                            result.setdefault('capture_errors', []).append({'type': type(exc).__name__, 'message': str(exc)})
            executor.shutdown(wait=True)
            executor = None
    except BaseException as exc:
        failure = failure or exc
        result['success'] = False
        result.setdefault('error', {'type': type(exc).__name__, 'message': str(exc)})
    finally:
        if serial_service is not None:
            serial_service.detach()
        if serial_link is not None:
            serial_link.close()
        if keypad_input:
            keypad_input.enable(False)
        finalizing = time.perf_counter()
        try:
            if executor:
                for listener in listeners:
                    try:
                        listener.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    listener.close()
                executor.shutdown(wait=True)
            if debugger:
                result['debugger'] = debugger.record
                debugger.close()
                if saving and output_ready and not recording:
                    controls = dict(debugger.record)
                    controls.pop('command', None) # generated session sockets are temporary
                    for action in controls['actions']:
                        if action['kind'] == 'script':
                            source_script = debug_output/action['artifact']
                            target = output/'provenance/debug'/source_script.name
                            target.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copyfile(source_script, target)
                            action['artifact'] = target.relative_to(output).as_posix()
                    (output/'provenance').mkdir(exist_ok=True)
                    (output/'provenance/debug-control.json').write_text(json.dumps(controls, indent=2)+'\n')
            if audio_capture:
                result['audio'] = audio_capture.close()
            result['phases']['execution_and_capture_seconds'] = finalizing-started
            if failure and engine_log and engine_log.tail:
                result['engine_error_context'] = engine_log.text()
            for i, count in enumerate(uart_counts):
                result['uart'][str(i)] = {'size_bytes': count}
                path = artifact_path(output, f'uart{i}.bin') if output_ready else None
                if recording and path is not None and path.exists():
                    result['uart'][str(i)] = file_identity(path)
            if collector:
                result['collector'] = collector.metrics
            if memory_collector:
                result['memory_collector'] = memory_collector.metrics
            if recording and output_ready:
                if console:
                    console.phase('save')
                save_diagnostics(root, output, result, started, finalizing)
            if saving and output_ready:
                save_provenance(root, output)
                artifacts.finish(result)
        except BaseException as exc:
            failure = failure or exc
            result['success'] = False
            result.setdefault('error', {'type': type(exc).__name__, 'message': str(exc)})
            result.setdefault('finalization_errors', []).append({'type': type(exc).__name__, 'message': str(exc)})
            if recording and output_ready:
                try:
                    (output/'artifacts.sha256.json').unlink(missing_ok=True)
                    (artifact_path(output, 'result.json', create=True)).write_text(json.dumps(result, indent=2)+'\n')
                except OSError:
                    pass
        finally:
            if not saving or failure:
                result['phases']['finalization_seconds'] = time.perf_counter()-finalizing
                result['phases']['total_seconds'] = time.perf_counter()-started
            resources.close()
    return result, failure

