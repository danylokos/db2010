"""Same-origin file-browser endpoints with bounded native-transfer spooling."""
import asyncio
import hashlib
import json
from urllib.parse import quote

from aiohttp import web

from ..transfer.obex import ProtocolError, RemoteError, TransferCancelled
from ..transfer.service import FileService, Busy, SessionChanged


class FilesAPI:
    def __init__(self, origin):
        self.origin = origin
        self.service = FileService()

    def routes(self, app):
        for method,path,callback in [
            ('GET','/api/files/state',self.state),
            ('POST','/api/files/connect',self.connect),
            ('POST','/api/files/disconnect',self.disconnect),
            ('GET','/api/files/list',self.list),
            ('POST','/api/files/jobs',self.reserve),
            ('GET','/api/files/jobs/{key}',self.job),
            ('PUT','/api/files/jobs/{key}/body',self.upload),
            ('POST','/api/files/jobs/{key}/cancel',self.cancel),
            ('GET','/api/files/jobs/{key}/content',self.content)]:
            app.router.add_route(method,path,self.endpoint(callback))

    def endpoint(self, callback):
        async def handle(request):
            origin = self.origin(request)
            supplied = request.headers.get('Origin')
            if (origin is None or (supplied is not None and supplied != origin) or
                    (request.method not in ('GET','HEAD') and supplied != origin)):
                raise web.HTTPForbidden(text='same-origin loopback browser required')
            try:
                return await callback(request)
            except (Busy,SessionChanged,FileExistsError) as exc:
                return web.json_response({'error':str(exc)},status=409,headers={'Cache-Control':'no-store'})
            except RemoteError as exc:
                return web.json_response({'error':str(exc),'remote_status':exc.status},status=403,headers={'Cache-Control':'no-store'})
            except (ProtocolError,TimeoutError,EOFError,OSError) as exc:
                return web.json_response({'error':str(exc)},status=502,headers={'Cache-Control':'no-store'})
            except (ValueError,TypeError,TransferCancelled) as exc:
                return web.json_response({'error':str(exc)},status=400,headers={'Cache-Control':'no-store'})
            except KeyError:
                return web.json_response({'error':'Transfer not found'},status=404,headers={'Cache-Control':'no-store'})
        return handle

    async def metadata(self, request):
        raw = bytearray()
        async with asyncio.timeout(10):
            async for chunk in request.content.iter_chunked(8192):
                raw.extend(chunk)
                if len(raw)>8192: raise ValueError('Request metadata is too large')
        data = json.loads(raw)
        if not isinstance(data,dict): raise ValueError('Request object required')
        return data

    def epoch(self, request):
        return request.query.get('epoch','')

    async def state(self, request):
        return web.json_response(self.service.state(),headers={'Cache-Control':'no-store'})

    async def connect(self, request):
        data = await self.metadata(request)
        state = await asyncio.to_thread(self.service.connect,data.get('epoch'))
        return web.json_response(state)

    async def disconnect(self, request):
        data = await self.metadata(request)
        await asyncio.to_thread(self.service.disconnect,data.get('epoch'))
        return web.json_response({'disconnected':True})

    async def list(self, request):
        raw = request.query.get('path','[]')
        if len(raw)>8192: raise ValueError('Path is too long')
        path = json.loads(raw)
        if not isinstance(path,list): raise ValueError('Path array required')
        entries = await asyncio.to_thread(self.service.list,self.epoch(request),path)
        return web.json_response({'path':path,'entries':entries},headers={'Cache-Control':'no-store'})

    async def reserve(self, request):
        data = await self.metadata(request)
        if not isinstance(data.get('path'),list) or type(data.get('replace',False)) is not bool:
            raise ValueError('Path array and replacement flag required')
        result = self.service.reserve(data.get('epoch'),data.get('kind'),data['path'],
            size=data.get('size'),replace=data.get('replace',False))
        return web.json_response(result,status=202)

    async def job(self, request):
        with self.service.lock:
            result = self.service.job(self.epoch(request),request.match_info['key']).public()
        return web.json_response(result,headers={'Cache-Control':'no-store'})

    async def upload(self, request):
        epoch,key = self.epoch(request),request.match_info['key']
        job = self.service.begin_receiving(epoch,key)
        digest = hashlib.sha256()
        count = 0
        try:
            async with asyncio.timeout(300):
                with job.file.open('wb') as stream:
                    async for chunk in request.content.iter_chunked(65536):
                        count += len(chunk)
                        self.service.receive_progress(epoch,key,count)
                        await asyncio.to_thread(stream.write,chunk)
                        digest.update(chunk)
            self.service.commit_upload(epoch,key,count,digest.hexdigest())
        except BaseException as exc:
            self.service.fail_receiving(epoch,key,exc)
            raise
        return web.json_response(job.public(),status=202)

    async def cancel(self, request):
        data = await self.metadata(request)
        self.service.cancel(data.get('epoch'),request.match_info['key'])
        return web.json_response({'cancel_requested':True})

    async def content(self, request):
        epoch = self.epoch(request)
        job = self.service.job(epoch,request.match_info['key'])
        if job.kind!='download' or job.phase!='done': raise Busy('Download is not ready')
        response = web.StreamResponse(headers={'Content-Type':'application/octet-stream',
            'Content-Length':str(job.file.stat().st_size),'Cache-Control':'no-store',
            'X-Content-Type-Options':'nosniff',
            'Content-Disposition':"attachment; filename*=UTF-8''"+quote(job.path[-1],safe='')})
        with job.file.open('rb') as stream:
            await response.prepare(request)
            while chunk := await asyncio.to_thread(stream.read,65536):
                if epoch != self.service.epoch:
                    response.force_close()
                    break
                await response.write(chunk)
            await response.write_eof()
        return response
