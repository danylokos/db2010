import argparse
import json
import math
from pathlib import Path

from .paths import ROOT, DEFAULT_EXPERIMENT, NON_PROVISIONED_EXPERIMENT
from .execution.runner import run
from .execution.debug import DebugOptions
from .ui.console import Console


class DebugAction(argparse.Action):
    def __call__(self, parser, namespace, value, option_string=None):
        actions = list(getattr(namespace, self.dest, None) or [])
        actions.append(('script' if option_string == '--gdb-script' else 'command', value))
        setattr(namespace, self.dest, actions)


def main():
    parser = argparse.ArgumentParser(description='DB2010 Python host runner for the guarded native QEMU engine')
    parser.add_argument('--repo', type=Path, default=ROOT, help='emulator checkout (default: editable checkout or working directory)')
    parser.add_argument('--input-root', type=Path, help='root for relative firmware references (default: emulator checkout)')
    parser.add_argument('--experiment', type=Path,
                        help='experiment configuration; default: bundled verified boot profile (research 0272; configured W800 flash, full battery)')
    parser.add_argument('--snapshot', action='store_true', help='save a full native execution checkpoint')
    start = parser.add_mutually_exclusive_group()
    start.add_argument('--non-provisioned', '--unprovisioned', dest='non_provisioned', action='store_true',
                       help='cold boot original non-provisioned W800 images; default: provisioned saved settings')
    start.add_argument('--load-flash', metavar='RUN', help='cold boot using saved raw flash images')
    parser.add_argument('--save-flash', action='store_true', help='save committed NOR/NAND bytes for a new cold boot')
    start.add_argument('--restore', metavar='RUN', help='resume a checkpoint run label or directory')
    parser.add_argument('--allow-engine-mismatch', action='store_true',
                        help='attempt restore with a different QEMU binary; retain native schema/model checks')
    parser.add_argument('--record-audio', action='store_true', help='save native PCM/WAV and packet metadata')
    parser.add_argument('--diagnostics', action='store_true', help='save diagnostic state and full logs')
    parser.add_argument('--artifacts-dir', type=Path, help='run root (default: <repo>/artifacts)')
    parser.add_argument('--label', help='new run name; requires an artifact saving flag')
    parser.add_argument('--main', type=Path, metavar='IMAGE',
                        help='authenticated MAIN container/payload or expanded raw MAIN storage image')
    parser.add_argument('--fs', type=Path, metavar='IMAGE',
                        help='ready 11 MiB raw FS, used unchanged; historical stock containers also accepted')
    parser.add_argument('--gdfs', type=Path, metavar='IMAGE',
                        help='raw 1 MiB GDFS NOR bank at 0x45F00000; replaces the configured bank input')
    parser.add_argument('--nand-main', type=Path, metavar='IMAGE', help='raw 32 MiB NAND page data')
    parser.add_argument('--nand-spare', type=Path, metavar='IMAGE', help='raw 1 MiB NAND spare data')
    parser.add_argument('--trace', action='store_true', help='save instruction and memory traces')
    parser.add_argument('--ui', nargs='?', const=8765, type=int, metavar='PORT',
                        help='serve a local browser view (default port 8765; 0 selects a free port)')
    parser.add_argument('--uart', action='store_true',
                        help='stream live firmware UART text to stdout; does not change the serial mode')
    parser.add_argument('--uart-debug', action='store_true',
                        help='cold boot with verbose debug UART logging and stream it to stdout; '
                             'debug logging and default OBEX file transfer are mutually exclusive')
    parser.add_argument('--no-progress', action='store_true', help='disable interactive terminal progress')
    parser.add_argument('--json-summary', action='store_true',
                        help='print the final JSON summary to stderr instead of the concise stop line')
    parser.add_argument('--disassemble', action='store_true')
    parser.add_argument('--research-guards', action='store_true',
                        help='enable research validation (required for --limit; default: off)')
    parser.add_argument('--limit', type=int, metavar='N',
                        help='stop after N accepted instruction observations; requires --research-guards')
    parser.add_argument('--seconds', type=float,
                        help='wall-clock execution limit (default: no wall-clock limit)')
    parser.add_argument('--debug', action='store_true', help='start GDB; interactive unless commands/scripts are supplied')
    parser.add_argument('--gdb-command', dest='gdb_actions', action=DebugAction, metavar='COMMAND',
                        help='execute a GDB command; repeatable, in supplied order with scripts')
    parser.add_argument('--gdb-script', dest='gdb_actions', action=DebugAction, metavar='FILE',
                        help='execute a frozen GDB command file; repeatable')
    parser.add_argument('--gdb-interactive', action='store_true', help='keep an interactive prompt after supplied commands/scripts')
    parser.add_argument('--gdb', default='gdb-multiarch', metavar='PATH', help='ARM-capable GDB executable')
    args = parser.parse_args()
    if args.label is not None and not (args.diagnostics or args.trace or args.disassemble or args.snapshot or args.record_audio or args.save_flash):
        parser.error('--label requires an artifact saving flag')
    if args.non_provisioned and args.experiment:
        parser.error('--non-provisioned selects its own boot profile; cannot combine with --experiment')
    if args.restore and (args.experiment or args.main or args.fs or args.gdfs or args.nand_main or args.nand_spare):
        parser.error('--restore uses its saved configuration; image/experiment overrides are not supported')
    if args.restore and args.uart_debug:
        parser.error('--uart-debug requires a cold boot; --restore preserves its saved serial mode (use --uart to display it)')
    if args.load_flash and (args.main or args.fs or args.gdfs or args.nand_main or args.nand_spare):
        parser.error('--load-flash cannot combine with explicit image inputs')
    if args.allow_engine_mismatch and not args.restore:
        parser.error('--allow-engine-mismatch requires --restore')
    if args.limit is not None and not 0 < args.limit <= 2**64-1:
        parser.error('--limit must be a positive uint64 instruction count')
    if args.limit is not None and not args.research_guards:
        parser.error('--limit requires --research-guards; use --seconds to bound fast execution')
    if args.seconds is not None and (not math.isfinite(args.seconds) or args.seconds <= 0):
        parser.error('--seconds must be finite and positive')
    if args.ui is not None and not 0 <= args.ui <= 65535:
        parser.error('--ui port must be between 0 and 65535')
    root = args.repo.resolve()
    experiment = (args.experiment or (NON_PROVISIONED_EXPERIMENT if args.non_provisioned else DEFAULT_EXPERIMENT)).resolve()
    debugging = args.debug or args.gdb_actions or args.gdb_interactive
    debug = DebugOptions(args.gdb, args.gdb_interactive or not args.gdb_actions,
                         args.gdb_actions or []) if debugging else None
    console = Console(uart=args.uart or args.uart_debug, progress=not (debugging or args.no_progress or args.json_summary))
    images = {name: getattr(args, name.replace('-', '_')).resolve()
              for name in ('main', 'fs', 'gdfs', 'nand-main', 'nand-spare')
              if getattr(args, name.replace('-', '_')) is not None}
    browser = None
    observations = console
    if args.ui is not None:
        from .ui.browser import Browser
        from .ui.observer import Observers
        try:
            browser = Browser(args.ui)
        except OSError as exc:
            parser.error(f'cannot bind browser UI to 127.0.0.1:{args.ui}: {exc}')
        observations = Observers(console, browser)
        if not args.json_summary:
            console.write_summary(f'Browser: {browser.url} (final view stays open until Ctrl-C)')
    try:
        result, failure = run(root, experiment, None,
                              input_root=args.input_root, record_audio=args.record_audio, diagnostics=args.diagnostics, artifacts_dir=args.artifacts_dir, label=args.label,
                              snapshot=args.snapshot, restore=args.restore, save_flash=args.save_flash, load_flash=args.load_flash,
                              allow_engine_mismatch=args.allow_engine_mismatch,
                              uart_debug=args.uart_debug,
                              serial_service=browser.files.service if browser else None,
                              trace=args.trace or args.disassemble, disassemble=args.disassemble, seconds=args.seconds,
                              instruction_limit=args.limit, config_limits=False, bounded_history=True,
                              research_guards=args.research_guards,
                              debug=debug, console=observations, images=images or None,
                              keypad_input=getattr(browser, 'input', None))
        console.finish(result, failure, summary=not args.json_summary)
        if result.get('checkpoint_compatibility', {}).get('engine_mismatch') and not args.json_summary:
            console.write_summary('Checkpoint engine differs: --allow-engine-mismatch used; native load checks retained.')
        if result.get('run_directory') and not args.json_summary:
            console.write_summary(f'Artifacts: {result["run_directory"]}')
        if args.json_summary:
            console.write_summary(json.dumps({'success': result['success'], 'stop': result.get('snapshot', {}).get('stop'),
                              'error': result.get('error'), 'phases': result['phases'],
                              'execution_limits': result.get('execution_limits'),
                              'research_guards': result.get('research_guards'),
                              'observations': result.get('snapshot', {}).get('observations'),
                              'counter_kind': result.get('snapshot', {}).get('counter_kind'),
                              'checkpoint_compatibility': result.get('checkpoint_compatibility'),
                              'run_directory': result.get('run_directory'), 'checkpoint': result.get('checkpoint'),
                              'flash': result.get('flash'), 'artifact_errors': result.get('artifact_errors'),
                              'trace_rows': result.get('collector', {}).get('observations'),
                              'uart': result['uart'], **({'ui_url': browser.url} if browser else {})}, indent=2))
        if browser:
            browser.finish(result, failure)
            if not isinstance(failure, KeyboardInterrupt):
                try:
                    browser.wait()
                except KeyboardInterrupt:
                    pass
    finally:
        if browser:
            browser.close()
    if failure:
        raise SystemExit(130 if isinstance(failure, KeyboardInterrupt) else 1)
