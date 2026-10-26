"""Opt-in UART text and one terminal ticker, independent of saved evidence."""
import codecs
import os
import sys
import threading
import time


def is_terminal(stream):
    try:
        return stream.isatty()
    except (OSError, ValueError):
        return False


def same_terminal(first, second):
    if not (is_terminal(first) and is_terminal(second)):
        return False
    if first is second:
        return True
    try:
        a, b = os.fstat(first.fileno()), os.fstat(second.fileno())
        return (a.st_dev, a.st_ino, a.st_rdev) == (b.st_dev, b.st_ino, b.st_rdev)
    except (OSError, ValueError):
        return False


def safe_text(text):
    return ''.join(char if char.isprintable() else
                   (f'\\x{ord(char):02x}' if ord(char) <= 255 else f'\\u{ord(char):04x}')
                   for char in text)


class Console:
    show_display = False

    def display(self, panel):
        pass

    def __init__(self, output=None, status=None, *, uart=False, progress=True):
        self.output = output if output is not None else sys.stdout
        self.status = status if status is not None else sys.stderr
        self.uart_enabled = uart
        self.output_terminal = is_terminal(self.output)
        self.show_status = progress and is_terminal(self.status) and os.environ.get('TERM') != 'dumb'
        self.shared_terminal = same_terminal(self.output, self.status)
        self.started = time.perf_counter()
        self.lock = threading.Lock()
        self.decoders = [codecs.getincrementaldecoder('utf-8')('backslashreplace') for _ in range(4)]
        self.after_cr = [False]*4
        self.visible = self.partial = False
        self.phase_name = 'prepare'
        self.sample = None
        self.previous = None
        self.rate = 0.0
        self.port = 0
        self.output_open = self.status_open = True
        self.finished = False

    @staticmethod
    def _write(stream, text):
        try:
            stream.write(text)
            stream.flush()
            return True
        except (OSError, ValueError) as exc:
            # CPython flushes standard streams again at exit. Redirect a broken
            # pipe's descriptor so that flush cannot turn a clean run into 120.
            if isinstance(exc, BrokenPipeError) and (stream is sys.stdout or stream is sys.stderr):
                try:
                    with open(os.devnull, 'w') as sink:
                        os.dup2(sink.fileno(), stream.fileno())
                except (OSError, ValueError):
                    pass
            return False

    def _status_write(self, text):
        if self.status_open:
            self.status_open = self._write(self.status, text)
        if not self.status_open:
            self.show_status = self.visible = False
        return self.status_open

    def _clear(self):
        if self.visible:
            # Park at column zero so a resize can reflow the ticker below us;
            # erase those rows too before writing UART or a replacement ticker.
            self._status_write('\r\033[J')
            self.visible = False

    def _draw(self):
        if not self.show_status or (self.shared_terminal and self.partial):
            return
        self._clear()
        elapsed = time.perf_counter() - self.started
        text = f'{self.phase_name} {elapsed:.1f}s'
        if self.sample is not None:
            s = self.sample
            unit = 'icount' if s.get('counter_kind') == 'qemu-icount' else 'obs'
            text += (f'  {s["observations"]/1e6:.1f}M {unit}  {self.rate/1e6:.1f}M/s'
                     f'  PC {s["pc"]:08X}')
            extras = [f'UART {sum(s["uart_bytes"])}B']
            if 'timer_ticks' in s:
                virtual_seconds = s['virtual_ns']/1e9 if 'virtual_ns' in s else s['timer_ticks']/s['timer_hz']
                extras.append(f'IRQ {s["timer_irqs"]} virt {virtual_seconds:.1f}s')
        else:
            extras = []
        try:
            width = os.get_terminal_size(self.status.fileno()).columns
        except (OSError, ValueError):
            width = 80
        for extra in extras:
            if len(text) + len(extra) + 2 < width:
                text += '  ' + extra
        self.visible = self._status_write('\r\033[K' + text[:max(0, width-1)] + '\r')

    def phase(self, name):
        with self.lock:
            self.phase_name = name
            if name == 'run':
                self.previous = (time.perf_counter(), 0)
            self._draw()

    def progress(self, sample):
        with self.lock:
            now = time.perf_counter()
            if self.previous is not None:
                elapsed = now - self.previous[0]
                self.rate = max(0, sample['observations'] - self.previous[1]) / max(elapsed, 1e-9)
            self.previous = (now, sample['observations'])
            self.sample = sample
            self._draw()

    def _terminal_text(self, port, text):
        # Convert CR and CRLF into line breaks, including across recv boundaries.
        # Escape all other nonprintable firmware input, including ESC, backspace,
        # C1 controls and Unicode format controls. Raw capture is never changed.
        rendered = []
        for char in text:
            if char == '\r':
                rendered.append('\n')
            elif char == '\n':
                if not self.after_cr[port]:
                    rendered.append('\n')
            else:
                rendered.append(safe_text(char))
            self.after_cr[port] = char == '\r'
        return ''.join(rendered)

    def _uart_text(self, port, text):
        if self.output_terminal:
            text = self._terminal_text(port, text)
        if not text or not self.output_open:
            return
        if self.shared_terminal:
            self._clear()
        if port != self.port:
            # Label switches once, separating partial lines from different ports.
            text = ('\n' if self.partial else '') + f'[UART{port}] ' + text
            self.port = port
        self.output_open = self._write(self.output, text)
        if not self.output_open:
            self.partial = False
        else:
            self.partial = not text.endswith('\n')
        self._draw()

    def uart(self, port, data):
        if not self.uart_enabled:
            return
        with self.lock:
            if not self.finished and self.output_open:
                self._uart_text(port, self.decoders[port].decode(data))

    def write_summary(self, text):
        with self.lock:
            self._status_write(text + '\n')

    def finish(self, result, failure=None, *, summary=True):
        with self.lock:
            if self.finished:
                return
            self.finished = True
            if self.uart_enabled:
                for port, decoder in enumerate(self.decoders):
                    self._uart_text(port, decoder.decode(b'', final=True))
            self._clear()
            if self.output_terminal and self.partial and self.output_open:
                self.output_open = self._write(self.output, '\n')
                self.partial = False
            if summary:
                snapshot = result.get('snapshot', {})
                stop = snapshot.get('stop', {})
                elapsed = result.get('phases', {}).get('total_seconds', time.perf_counter()-self.started)
                if isinstance(failure, KeyboardInterrupt):
                    text = 'Interrupted'
                elif failure:
                    message = result.get('error', {}).get('message') or str(failure) or type(failure).__name__
                    text = f'Failed: {safe_text(message)[:512]}'
                else:
                    text = f'Stopped: {stop.get("reason", result.get("session_end", "complete"))}'
                pc = stop.get('pc')
                if pc is None and 'registers' in snapshot:
                    pc = snapshot['registers'][15]
                count = stop.get('observations', snapshot.get('observations'))
                if pc is not None and count is not None:
                    text += f'  PC {pc:08X}  {count:,} insn'
                self._status_write(text + f'  {elapsed:.1f}s\n')
                context = result.get('engine_error_context') or result.get('debugger', {}).get('error_context')
                if failure and context:
                    for line in context.splitlines()[-4:]:
                        self._status_write('  '+safe_text(line)[:512]+'\n')
