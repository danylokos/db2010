"""Read-only execution observations; consumers never control the guest."""
from typing import Protocol


class Observer(Protocol):
    show_status: bool
    show_display: bool

    def phase(self, name: str): ...
    def progress(self, sample: dict): ...
    def uart(self, port: int, data: bytes): ...
    def display(self, panel: dict): ...
    def audio(self, packet): ...


class Observers:
    """Fan out observations on the producing thread; consumers must stay bounded."""
    def __init__(self, *consumers):
        self.consumers = consumers
        self.show_status = any(c.show_status for c in consumers)
        self.show_display = any(getattr(c, 'show_display', False) for c in consumers)

    def phase(self, name):
        for consumer in self.consumers:
            consumer.phase(name)

    def progress(self, sample):
        for consumer in self.consumers:
            if consumer.show_status:
                consumer.progress(sample)

    def uart(self, port, data):
        for consumer in self.consumers:
            consumer.uart(port, data)

    def display(self, panel):
        for consumer in self.consumers:
            if getattr(consumer, 'show_display', False):
                consumer.display(panel)

    def audio(self, packet):
        for consumer in self.consumers:
            callback = getattr(consumer, 'audio', None)
            if callback:
                callback(packet)
