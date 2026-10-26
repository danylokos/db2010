"""Host observation deadlines; these never advance emulated device time."""
import math
import time

LCD_INTERVAL = 1 / 60
PUBLICATION_INTERVAL = 1 / 4


class Schedule:
    """Independent periodic callbacks, skipping missed slots after each callback.

    Callbacks run on the owner thread. Adding another subsystem requires only
    its interval and callback; buffering/transport remain the consumer's concern.
    """
    def __init__(self, clock=None):
        self.clock = clock or time.monotonic
        self.entries = {}

    def add(self, name, interval, callback):
        if interval <= 0:
            raise ValueError('schedule interval must be positive')
        self.entries[name] = [interval, self.clock(), callback]

    def run_due(self, deadline):
        for entry in self.entries.values():
            interval, due, callback = entry
            if self.clock() >= due:
                callback(deadline)
                # Keep the original cadence, without queuing missed work.
                now = self.clock()
                entry[1] = due + (math.floor((now - due) / interval) + 1) * interval

    def next_deadline(self, deadline):
        return min([deadline, *(entry[1] for entry in self.entries.values())])

    def delay(self, deadline):
        return max(0, self.next_deadline(deadline) - self.clock())
