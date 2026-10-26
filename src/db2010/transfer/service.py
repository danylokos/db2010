"""Serialize live native file operations and retain bounded transfer results."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
import hashlib
from pathlib import Path
import tempfile
import threading
import time
import uuid

from .obex import ObexSession, ProtocolError, RemoteError, TransferCancelled, path_segments

MAX_FILE = 32*1024*1024
MAX_JOBS = 8


class Busy(Exception): pass
class SessionChanged(Exception): pass


@dataclass
class Job:
    id: str
    epoch: str
    kind: str
    path: tuple
    file: Path
    total: int | None = None
    phase: str = 'queued'
    count: int = 0
    error: str | None = None
    digest: str | None = None
    replace: bool = False
    created: float = field(default_factory=time.monotonic)
    cancel: threading.Event = field(default_factory=threading.Event)
    finished: float | None = None

    def public(self):
        return dict(id=self.id,epoch=self.epoch,kind=self.kind,path=list(self.path),
            phase=self.phase,bytes=self.count,total=self.total,error=self.error,sha256=self.digest,
            cancel_requested=self.cancel.is_set())


class FileService:
    def __init__(self):
        self.lock = threading.RLock()
        self.operation = threading.Lock()
        self.executor = ThreadPoolExecutor(max_workers=1,thread_name_prefix='db2010-files')
        self.temporary = tempfile.TemporaryDirectory(prefix='db2010-files-')
        self.epoch = uuid.uuid4().hex
        self.link = self.session = None
        self.jobs = {}
        self.active = False
        self.ready = False
        self.link_generation = None

    def attach(self, link):
        self.detach()
        with self.lock:
            self.epoch = uuid.uuid4().hex
            self.link = link
            self.session = ObexSession(link)
            self.link_generation = link.generation
            self.active = True
            self.ready = False

    def progress(self, sample):
        with self.lock:
            if self.link is not None and 'serial_cable_present' in sample:
                self.link.observe_cable(sample['serial_cable_present'])
            self._sync_cable()
            self.ready = bool(self.link and self.link.present and
                              (self.ready or sample.get('serial_input_ready')))

    def _sync_cable(self):
        if self.link is None or self.link.generation == self.link_generation:
            return
        self.link_generation = self.link.generation
        self.epoch = uuid.uuid4().hex
        self.ready = False
        self.session.connected = False
        self.session.failed = True
        for job in self.jobs.values():
            if job.finished is None and job.phase in ('receiving','upload-body'):
                self.fail_receiving(job.epoch,job.id,'Serial cable changed')
        if self.link.present:
            self.session = ObexSession(self.link)

    def detach(self):
        with self.lock:
            self.active = False
            self.ready = False
            if self.link is not None: self.link.close()
            for job in self.jobs.values():
                if job.finished is None:
                    job.cancel.set()
                    if job.phase in ('receiving','upload-body'):
                        self.fail_receiving(job.epoch,job.id,'Phone session ended')

    def state(self):
        with self.lock:
            self._sync_cable()
            self._prune()
            return dict(epoch=self.epoch,enabled=self.link is not None,running=self.active,
                available=self.active and self.ready and self.link.present and not self.link.closed,
                connected=bool(self.active and self.session and self.session.connected),
                busy=self.operation.locked(),jobs=[j.public() for j in self.jobs.values() if j.epoch==self.epoch])

    def _check(self, epoch):
        self._sync_cable()
        if epoch != self.epoch or not self.active or self.link.closed:
            raise SessionChanged('Phone session ended or changed')
        if not self.link.present:
            raise SessionChanged('Serial cable is disconnected')
        if not self.ready:
            raise SessionChanged('Phone connection is starting')

    def _enter(self, epoch):
        with self.lock:
            self._prune()
            self._check(epoch)
            if not self.operation.acquire(blocking=False):
                raise Busy('Another file operation is in progress')
            return self.session

    def connect(self, epoch):
        session = self._enter(epoch)
        try:
            session.connect()
            return self.state()
        finally:
            self.operation.release()

    def disconnect(self, epoch):
        session = self._enter(epoch)
        try:
            session.disconnect()
        finally:
            self.operation.release()

    def list(self, epoch, path):
        parts = path_segments(path)
        session = self._enter(epoch)
        try:
            return session.list(parts)
        finally:
            self.operation.release()

    def _prune(self):
        now = time.monotonic()
        for key,job in list(self.jobs.items()):
            if job.phase in ('receiving','upload-body') and now-job.created > 300:
                job.cancel.set()
                self.fail_receiving(job.epoch,key,'Upload deadline exceeded')
            if job.finished is not None and (job.epoch != self.epoch or now-job.finished > 300):
                job.file.unlink(missing_ok=True)
                del self.jobs[key]

    def reserve(self, epoch, kind, path, *, size=None, replace=False):
        parts = path_segments(path)
        if not parts or kind not in ('upload','download'):
            raise ValueError('File path required')
        if kind == 'upload' and (type(size) is not int or not 0 <= size <= MAX_FILE):
            raise ValueError('File exceeds32 MiB transfer limit')
        session = self._enter(epoch)
        try:
            session._require()
            with self.lock:
                self._prune()
                if len(self.jobs) >= MAX_JOBS:
                    oldest = next((j for j in self.jobs.values() if j.finished is not None),None)
                    if oldest is None: raise Busy('Transfer queue is full')
                    oldest.file.unlink(missing_ok=True)
                    del self.jobs[oldest.id]
                key = uuid.uuid4().hex
                job = Job(key,epoch,kind,parts,Path(self.temporary.name)/key,total=size,
                          phase='receiving' if kind=='upload' else 'queued',replace=replace)
                self.jobs[key] = job
                if kind == 'download': self.executor.submit(self._work,job,session)
                return job.public()
        except BaseException:
            self.operation.release()
            raise

    def job(self, epoch, key):
        with self.lock:
            if epoch != self.epoch: raise SessionChanged('Phone session changed')
            job = self.jobs[key]
            if job.epoch != epoch: raise SessionChanged('Phone session changed')
            return job

    def begin_receiving(self, epoch, key):
        with self.lock:
            self._check(epoch)
            job = self.job(epoch,key)
            if job.kind != 'upload' or job.phase != 'receiving':
                raise Busy('Upload is already receiving a body or has ended')
            job.phase = 'upload-body'
            return job

    def receive_progress(self, epoch, key, count):
        with self.lock:
            self._check(epoch)
            job = self.job(epoch,key)
            if job.cancel.is_set(): raise TransferCancelled('Upload cancelled')
            if job.phase != 'upload-body' or count > job.total:
                raise ValueError('Upload body size differs')
            job.count = count

    def commit_upload(self, epoch, key, count, digest):
        with self.lock:
            self._check(epoch)
            job = self.job(epoch,key)
            if job.cancel.is_set(): raise TransferCancelled('Upload cancelled')
            if job.phase != 'upload-body' or count != job.total:
                raise ValueError('Upload body size differs')
            job.digest = digest
            job.phase = 'queued'
            self.executor.submit(self._work,job,self.session)

    def fail_receiving(self, epoch, key, error):
        with self.lock:
            job = self.jobs.get(key)
            if job is None or job.epoch != epoch or job.phase not in ('receiving','upload-body'): return
            job.error = str(error)
            job.phase = 'cancelled' if job.cancel.is_set() else 'error'
            job.finished = time.monotonic()
            job.file.unlink(missing_ok=True)
            self.operation.release()

    def cancel(self, epoch, key):
        with self.lock:
            job = self.job(epoch,key)
            if job.finished is None:
                job.cancel.set()
                if job.phase in ('receiving','upload-body'): self.fail_receiving(epoch,key,'Upload cancelled')

    def _work(self, job, session):
        timer = threading.Timer(300,job.cancel.set)
        timer.daemon = True
        timer.start()
        def progress(count,total):
            if count > MAX_FILE:
                job.cancel.set()
                raise ValueError('File exceeds32 MiB transfer limit')
            with self.lock:
                self._check(job.epoch)
                job.count = count
                job.total = total
        try:
            with self.lock:
                self._check(job.epoch)
                job.phase = 'uploading' if job.kind=='upload' else 'downloading'
                job.count = 0
            if job.cancel.is_set(): raise TransferCancelled('Transfer cancelled')
            if job.kind == 'upload':
                if not job.replace and any(e['name']==job.path[-1] for e in session.list(job.path[:-1])):
                    raise FileExistsError('File exists; choose Replace to overwrite it')
                with job.file.open('rb') as stream:
                    session.upload(job.path,stream,job.total,progress,job.cancel)
                with self.lock: job.phase='verifying';job.count=0
                digest = hashlib.sha256()
                class Sink:
                    def write(self,data): digest.update(data);return len(data)
                session.download(job.path,Sink(),progress,job.cancel)
                if digest.hexdigest() != job.digest:
                    raise ProtocolError('Uploaded file verification failed')
                job.file.unlink(missing_ok=True)
            else:
                with job.file.open('wb') as stream:
                    session.download(job.path,stream,progress,job.cancel)
                with job.file.open('rb') as stream:
                    job.digest = hashlib.file_digest(stream,'sha256').hexdigest()
            with self.lock: job.phase='done'
        except BaseException as exc:
            with self.lock:
                job.phase = 'cancelled' if isinstance(exc,TransferCancelled) or job.cancel.is_set() else 'error'
                job.error = str(exc)
                job.file.unlink(missing_ok=True)
                if not isinstance(exc,(RemoteError,FileExistsError,TransferCancelled)):
                    session.connected = False
                    session.failed = True
        finally:
            timer.cancel()
            with self.lock: job.finished=time.monotonic()
            self.operation.release()

    def close(self):
        self.detach()
        self.executor.shutdown(wait=True)
        self.temporary.cleanup()
