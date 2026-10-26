"""Bounded ordered input channel, separate from read-only observations."""
from collections import deque
import json
import threading
import time

BUTTONS = frozenset((*map(str, range(10)), 'star', 'hash', 'soft-left', 'soft-right',
                     'walkman', 'up', 'right', 'down', 'left', 'select', 'back', 'clear',
                     'volume-up', 'volume-down', 'play-pause', 'camera-focus', 'camera', 'power'))


class InputOverflow(ValueError):
    pass


class KeypadInput:
    def __init__(self, capacity=256):
        self.lock = threading.Lock()
        self.queue = deque()
        self.capacity = capacity
        self.enabled = False
        # Reserved emergency slot: it follows all already accepted transitions.
        self.release_pending = False

    def enable(self, enabled):
        with self.lock:
            self.enabled = enabled
            if not enabled:
                self.queue.clear()
                self.release_pending = True

    def submit(self, message):
        if (not isinstance(message, dict) or set(message) != {'type', 'button', 'pressed'}
                or message['type'] != 'keypad' or message['button'] not in BUTTONS
                or type(message['pressed']) is not bool):
            raise ValueError('invalid keypad command')
        with self.lock:
            if not self.enabled:
                return
            if self.release_pending or len(self.queue) >= self.capacity:
                self.release_pending = True
                raise InputOverflow('keypad queue full')
            self.queue.append((message['button'], message['pressed'], time.monotonic()))

    def release_all(self):
        with self.lock:
            if len(self.queue) < self.capacity and not self.release_pending:
                self.queue.append((None, False, time.monotonic()))
            else:
                self.release_pending = True

    def take(self):
        with self.lock:
            commands = list(self.queue)
            self.queue.clear()
            if self.release_pending:
                commands.append((None, False, time.monotonic()))
                self.release_pending = False
            return commands


class KeypadService:
    """Called only by the runner's QMP owner, including during GDB sessions."""
    def __init__(self, channel, stream=None, *, stop_on_event=False):
        self.channel, self.stream = channel, stream
        self.held = set()
        self.total = 0
        self.stop_on_event = stop_on_event
        self.closing = False

    def poll(self, qmp, deadline):
        for button, pressed, requested in self.channel.take():
            if self.stop_on_event and not self.closing and any(e.get('event') == 'STOP' for e in qmp.events):
                break
            target = set(self.held)
            if button is None:
                target.clear()
            elif pressed:
                target.add(button)
            else:
                target.discard(button)
            if target == self.held:
                continue
            qmp.execute('qom-set', deadline, {'path': '/machine/keypad', 'property': 'pressed',
                                            'value': json.dumps(sorted(target))})
            self.held = target
            self.total += 1
            if self.stream:
                # Getter snapshots the last setter's exact guest virtual timestamp.
                state = json.loads(qmp.execute('qom-get', deadline,
                    {'path': '/machine/keypad', 'property': 'input-state'}))
                self.stream.write(json.dumps({'sequence': self.total, 'button': button,
                    'pressed': pressed, 'held': sorted(target), 'requested_monotonic': requested,
                    'applied_monotonic': time.monotonic(), 'virtual_ns': state['applied_at']})+'\n')
                self.stream.flush()

    def close(self, qmp, deadline):
        self.closing = True
        self.channel.enable(False)
        self.poll(qmp, deadline)
