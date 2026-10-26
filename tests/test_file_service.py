import asyncio
import hashlib
import time

from aiohttp import ClientSession
import pytest

from db2010.execution.serial import SerialLink
from db2010.transfer.service import FileService, Busy, SessionChanged, MAX_FILE
from db2010.ui.browser import Browser


class Phone:
    connected = True
    failed = False
    def __init__(self): self.files={}; self.read_sizes=[]
    def connect(self): self.connected=True
    def disconnect(self): self.connected=False
    def _require(self): assert self.connected
    def list(self,path):
        return [dict(name=name,directory=False,size=len(data)) for name,data in self.files.items()]
    def upload(self,path,source,size,progress,cancel):
        output=bytearray()
        while data:=source.read(4096):
            self.read_sizes.append(len(data));output.extend(data);progress(len(output),size)
        self.files[path[-1]]=bytes(output)
        progress(size,size)
    def download(self,path,destination,progress,cancel):
        data=self.files[path[-1]]
        for i in range(0,len(data),511):
            destination.write(data[i:i+511]);progress(min(i+511,len(data)),len(data))
        if not data: progress(0,0)


def attach(service):
    service.attach(SerialLink())
    service.progress({'serial_input_ready':True})
    service.session=Phone()
    return service.epoch


def finished(service,epoch,key):
    until=time.monotonic()+3
    while True:
        with service.lock:
            job=service.job(epoch,key)
            if job.finished is not None:return job
        assert time.monotonic()<until
        time.sleep(.005)


def test_upload_readback_verification_download_and_single_owner():
    service=FileService()
    try:
        epoch=attach(service);data=bytes(range(256))*40
        record=service.reserve(epoch,'upload',['file.bin'],size=len(data))
        job=service.job(epoch,record['id']);job.file.write_bytes(data)
        with pytest.raises(Busy): service.list(epoch,[])
        service.begin_receiving(epoch,job.id)
        with pytest.raises(Busy):service.begin_receiving(epoch,job.id)
        service.receive_progress(epoch,job.id,len(data))
        service.commit_upload(epoch,job.id,len(data),hashlib.sha256(data).hexdigest())
        assert finished(service,epoch,job.id).phase=='done'
        assert not job.file.exists() and max(service.session.read_sizes)<=4096
        download=service.reserve(epoch,'download',['file.bin'])
        result=finished(service,epoch,download['id'])
        assert result.file.read_bytes()==data
        assert result.digest==hashlib.sha256(data).hexdigest()
    finally:service.close()


def test_reservation_cancel_detach_stale_epoch_and_file_bounds():
    service=FileService()
    try:
        epoch=attach(service)
        with pytest.raises(ValueError):service.reserve(epoch,'upload',['file'],size=MAX_FILE+1)
        first=service.reserve(epoch,'upload',['file'],size=1)
        service.cancel(epoch,first['id'])
        assert not service.operation.locked()
        second=service.reserve(epoch,'upload',['file'],size=1)
        service.detach()
        assert service.job(epoch,second['id']).phase=='cancelled' and not service.operation.locked()
        attach(service)
        with pytest.raises(SessionChanged):service.job(epoch,first['id'])
    finally:service.close()


def test_http_origin_body_mismatch_cleanup_and_old_session():
    view=Browser(0)
    try:
        epoch=attach(view.files.service)
        async def check():
            async with ClientSession() as http:
                async with http.post(view.url+'/api/files/connect',json={'epoch':epoch}) as response:
                    assert response.status==403
                async with http.post(view.url+'/api/files/connect',json={'epoch':epoch},headers={'Origin':'https://example.com'}) as response:
                    assert response.status==403
                async with http.post(view.url+'/api/files/jobs',json={'epoch':epoch,'kind':'upload','path':['short.bin'],'size':2},headers={'Origin':view.url}) as response:
                    assert response.status==202;job=await response.json()
                async with http.put(view.url+f'/api/files/jobs/{job["id"]}/body?epoch={epoch}',data=b'x',headers={'Origin':view.url}) as response:
                    assert response.status==400
                assert not view.files.service.operation.locked()
                assert view.files.service.job(epoch,job['id']).phase=='error'
                attach(view.files.service)
                async with http.get(view.url+f'/api/files/jobs/{job["id"]}?epoch={epoch}') as response:
                    assert response.status==409
        asyncio.run(check())
    finally:view.close()
