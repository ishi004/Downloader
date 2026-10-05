import hmac
import ipaddress
import json
import shutil
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .engine import Engine
from .media import estimate
from .models import ROOT, InspectRequest, Options, QueueRequest, Settings


def create_app(home=ROOT):
    engine = Engine(home)
    inspection_slots = threading.BoundedSemaphore(2)
    live_port = engine.settings.port

    @asynccontextmanager
    async def lifespan(app):
        engine.start()
        yield
        engine.close()

    app = FastAPI(title='MediaDock', lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.engine = engine

    @app.middleware('http')
    async def local_only(request: Request, call_next):
        host = request.headers.get('host', '')
        allowed = {f'127.0.0.1:{live_port}', f'localhost:{live_port}', f'[::1]:{live_port}'}
        peer = request.client.host if request.client else ''
        try:
            local = ipaddress.ip_address(peer).is_loopback
        except ValueError:
            local = peer == 'testclient'
        if host not in allowed or not local:
            return JSONResponse({'detail': '이 앱은 로컬 컴퓨터에서만 접근할 수 있습니다.'}, status_code=403)
        origin = request.headers.get('origin')
        if origin and origin not in {f'http://{host}', f'https://{host}'}:
            return JSONResponse({'detail': '다른 웹사이트의 요청은 허용하지 않습니다.'}, status_code=403)
        if request.headers.get('sec-fetch-site') == 'cross-site':
            return JSONResponse({'detail': '다른 웹사이트의 요청은 허용하지 않습니다.'}, status_code=403)
        if request.method not in ('GET', 'HEAD'):
            if not hmac.compare_digest(request.headers.get('x-mediadock-token', ''), engine.token):
                return JSONResponse({'detail': '요청 권한이 만료됐습니다. 페이지를 새로고침하세요.'}, status_code=403)
            size = request.headers.get('content-length', '0')
            if not size.isdigit() or int(size) > 262144:
                return JSONResponse({'detail': '요청이 너무 큽니다.'}, status_code=413)
            body = await request.body()
            if len(body) > 262144:
                return JSONResponse({'detail': '요청이 너무 큽니다.'}, status_code=413)
        response = await call_next(request)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' https: http: data:; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        response.headers['Cache-Control'] = 'no-store'
        return response

    @app.exception_handler(ValueError)
    async def bad_value(request, exc):
        return JSONResponse({'detail': str(exc)}, status_code=400)

    @app.exception_handler(KeyError)
    async def missing(request, exc):
        return JSONResponse({'detail': str(exc)}, status_code=404)

    @app.get('/api/state')
    def state():
        jobs = engine.jobs()
        stats = {s: sum(j['status'] == s for j in jobs) for s in ('queued','running','processing','completed','failed','cancelled','interrupted')}
        stats['active'] = stats['running'] + stats['processing']
        stats['bytes'] = sum(f['size'] for j in jobs if j['status'] == 'completed' for f in j['files'])
        try:
            free = shutil.disk_usage(engine.settings.save_dir).free
        except OSError:
            free = None
        return {'token': engine.token, 'settings': engine.settings.model_dump(), 'stats': stats, 'free_bytes': free,
                'health': engine.health, 'live_port': live_port}

    @app.get('/api/jobs')
    def jobs():
        return engine.jobs()

    @app.put('/api/settings')
    def settings(value: Settings):
        try:
            engine.save_settings(value)
        except OSError as e:
            raise HTTPException(400, '저장 경로에 쓸 수 없습니다: ' + str(e))
        return {'settings': value.model_dump(), 'restart_required': value.port != live_port}

    @app.post('/api/inspect')
    def inspect(body: InspectRequest):
        if not inspection_slots.acquire(blocking=False):
            raise HTTPException(429, '다른 링크를 조회 중입니다. 잠시 후 다시 시도하세요.')
        try:
            result = []
            for url in body.urls:
                try:
                    result.append(engine.inspect(url, body.playlist))
                except Exception as e:
                    result.append({'type': 'error', 'url': url, 'error': str(e)[-2500:]})
            return result
        finally:
            inspection_slots.release()

    @app.post('/api/estimate/{media_id}')
    def estimate_media(media_id: str, opts: Options):
        return estimate(engine.get_info(media_id), opts)

    @app.post('/api/jobs')
    def enqueue(body: QueueRequest):
        return engine.enqueue(body.items)

    @app.post('/api/jobs/{jid}/cancel')
    def cancel(jid: str):
        return engine.cancel(jid)

    @app.post('/api/jobs/{jid}/retry')
    def retry(jid: str):
        engine.retry(jid)
        return {'ok': True}

    @app.get('/api/jobs/{jid}/files/{index}')
    def download(jid: str, index: int):
        job = engine.get_job(jid)
        if job['status'] != 'completed' or index < 0 or index >= len(job['files']):
            raise HTTPException(404, '완료된 파일을 찾을 수 없습니다.')
        f = job['files'][index]
        path = Path(f['path']).resolve()
        if not path.is_relative_to(Path(job['settings']['save_dir']).resolve()) or not path.is_file():
            raise HTTPException(404, '저장 파일이 이동 또는 삭제되었습니다. 기록은 유지됩니다.')
        return FileResponse(path, filename=f['name'])

    app.mount('/', StaticFiles(directory=ROOT / 'web', html=True), name='web')
    return app
