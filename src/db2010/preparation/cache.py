"""Immutable content-addressed host-generated engine inputs."""
import os
from pathlib import Path
import tempfile

from ..diagnostics.artifacts import identity
from ..paths import emulator_build


def cached_input(root, data):
    import hashlib
    expected = {'size_bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
    directory = emulator_build(root)/'inputs'
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory/(expected['sha256']+'.bin')
    if not destination.exists():
        descriptor, name = tempfile.mkstemp(prefix='.input-', dir=directory)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
                os.fchmod(stream.fileno(), 0o444)
            try:
                os.link(temporary, destination)
            except FileExistsError:
                pass
            descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try: os.fsync(descriptor)
            finally: os.close(descriptor)
        finally:
            temporary.unlink(missing_ok=True)
    if identity(destination) != expected:
        raise ValueError(f'cached input identity differs: {destination}')
    return destination.resolve()
