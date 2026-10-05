import json
import sys
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from mediadock.app import create_app
from mediadock.engine import Engine
from mediadock.media import binary, capture, estimate, kill_tree, select_formats, shift_srt, spawn
from mediadock.models import Options, QueueItem, Settings, validate_url
from scripts.fixture_server import server


@pytest.fixture(scope='module')
def fixture_url():
    s = server(0)
    t = threading.Thread(target=s.serve_forever, daemon=True); t.start()
    yield f'http://127.0.0.1:{s.server_port}'
    s.shutdown(); s.server_close()


@pytest.fixture
def engine(tmp_path):
    e = Engine(tmp_path)
    e.save_settings(Settings(save_dir=str(tmp_path / 'outputs'), concurrency=2, subfolder='검증/미디어', by_channel=True))
    e.start()
    yield e
    e.close()


def wait_job(engine, jid, timeout=60):
    deadline = time.monotonic() + timeout
    observations = []
    while time.monotonic() < deadline:
        job = engine.get_job(jid)
        observations.append((job['status'], job['progress']))
        if job['status'] in ('completed','failed','cancelled'):
            return job, observations
        time.sleep(.1)
    pytest.fail('Timed out: ' + json.dumps(job))


@pytest.mark.parametrize('path',['../other','/absolute','x/../../z','C:/outside','x:ads'])
def test_subfolder_validation(path):
    with pytest.raises(ValidationError):
        Settings(subfolder=path)


@pytest.mark.parametrize('url',['file:///C:/secret','ftp://example.org/a','https://user:password@example.org','--help','https://example.org/\n'])
def test_url_validation(url):
    if url.endswith('\n'):
        assert validate_url(url) == url.strip()
    else:
        with pytest.raises(ValueError):
            validate_url(url)


def test_subtitle_clip():
    source='1\n00:00:01,000 --> 00:00:03,500\none\n\n2\n00:00:04,000 --> 00:00:07,000\ntwo\n'
    assert shift_srt(source,2,6) == '1\n00:00:00,000 --> 00:00:01,500\none\n\n2\n00:00:02,000 --> 00:00:04,000\ntwo\n'


def test_estimates_and_missing_format():
    info={'duration':8,'formats':[{'format_id':'low','height':180,'fps':15,'vcodec':'h264','acodec':'aac','filesize':1000},
                                {'format_id':'high','height':360,'fps':30,'vcodec':'h264','acodec':'none','tbr':500},
                                {'format_id':'audio','vcodec':'none','acodec':'aac','filesize':200}]}
    assert estimate(info,Options(height=180))['bytes']==1000
    assert estimate(info,Options())['bytes']==500200
    with pytest.raises(ValueError): select_formats(info,Options(height=1080))
    assert estimate({'formats':[{'format_id':'x','vcodec':None,'acodec':None}]},Options())['bytes'] is None


@pytest.mark.parametrize('options,codec,height,subtitle',[
    ({},'h264',360,False),
    ({'container':'mp4','height':180,'fps':15},'h264',180,False),
    ({'container':'mkv','subtitle_mode':'embed','subtitle_lang':'en'},'h264',360,True),
    ({'container':'mp4','subtitle_mode':'embed','subtitle_lang':'en','start':2,'end':6},'h264',360,True),
    ({'subtitle_mode':'burn','subtitle_lang':'en','start':2,'end':6},'h264',360,False),
    ({'subtitle_mode':'srt','subtitle_lang':'en','thumbnail':True,'start':2,'end':6},'h264',360,False),
    ({'kind':'audio','audio_format':'mp3','mp3_mode':'cbr','bitrate':128},'mp3',None,False),
    ({'kind':'audio','audio_format':'mp3','mp3_mode':'vbr','mp3_quality':0,'start':2,'end':6},'mp3',None,False),
    ({'kind':'audio','audio_format':'m4a','start':2,'end':6},'aac',None,False),
    ({'kind':'audio','audio_format':'original'},'aac',None,False),
    ({'kind':'audio','audio_format':'original','start':2,'end':6},'aac',None,False),
])
def test_real_pipeline(engine, fixture_url, options, codec, height, subtitle):
    info = engine.inspect(fixture_url+'/single.html')
    assert info['heights'] == [360,180]
    assert info['subtitles'][0]['code'] == 'en'
    item = QueueItem(media_id=info['media_id'], options=Options(**options))
    queued = engine.enqueue([item])
    assert not queued[0]['duplicate']
    assert engine.enqueue([item])[0]['duplicate']
    job, observed = wait_job(engine,queued[0]['id'])
    assert job['status']=='completed', job['error']
    streams=job['output_streams']
    assert streams[0]['codec']==codec
    if height: assert streams[0]['height']==height
    assert any(s['type']=='subtitle' for s in streams)==subtitle
    if options.get('fps'): assert streams[0]['fps']=='15/1'
    if options.get('end'): assert abs(job['output_duration']-4)<.15
    if subtitle and options.get('end'):
        embedded = capture([binary('ffmpeg'), '-v', 'error', '-i', job['files'][0]['path'], '-map', '0:s:0', '-f', 'srt', '-'], 30)
        assert '00:00:00,000 --> 00:00:01,500' in embedded
        assert '00:00:02,000 --> 00:00:04,000' in embedded
    assert all(Path(f['path']).is_file() for f in job['files'])
    assert any(status=='running' for status,_ in observed)
    if options.get('subtitle_mode')=='srt':
        srt=next(Path(f['path']) for f in job['files'] if f['name'].endswith('.srt'))
        assert '00:00:00,000 --> 00:00:01,500' in srt.read_text('utf-8')
        assert '00:00:02,000 --> 00:00:04,000' in srt.read_text('utf-8')
    if options.get('thumbnail'):
        thumb=next(Path(f['path']) for f in job['files'] if '.thumbnail.' in f['name'])
        assert thumb.read_bytes()==(Path(__file__).resolve().parents[1]/'test-results/fixtures/cover.jpg').read_bytes()
    assert engine.enqueue([item])[0]['duplicate']


def test_playlist_selection(engine,fixture_url):
    listing=engine.inspect(fixture_url+'/playlist.html')
    assert listing['type']=='playlist' and len(listing['entries'])==2
    chosen=listing['entries'][1]
    queued=engine.enqueue([QueueItem(media_id=chosen['media_id'],options=Options())])
    job,_=wait_job(engine,queued[0]['id'])
    assert job['status']=='completed',job['error']
    assert '(2)' in job['title']
    assert len(engine.jobs())==1


def test_cancel_retry_and_queue(engine,fixture_url):
    engine.save_settings(engine.settings.model_copy(update={'concurrency':1,'speed_kib':16}))
    info=engine.inspect(fixture_url+'/direct.html')
    first=engine.enqueue([QueueItem(media_id=info['media_id'],options=Options())])[0]['id']
    second=engine.enqueue([QueueItem(media_id=info['media_id'],options=Options(container='mkv'))])[0]['id']
    deadline=time.monotonic()+20
    while time.monotonic()<deadline:
        j=engine.get_job(first)
        if j['progress'] is not None and j['speed']:
            break
        time.sleep(.1)
    assert j['status']=='running' and j['speed']>0
    assert engine.get_job(second)['status']=='queued'
    engine.cancel(second);engine.cancel(first)
    j,_=wait_job(engine,first)
    assert j['status']=='cancelled' and not j['files']
    assert engine.get_job(second)['status']=='cancelled'
    engine.save_settings(engine.settings.model_copy(update={'speed_kib':0}))
    deadline=time.monotonic()+10
    while first in engine.active and time.monotonic()<deadline: time.sleep(.05)
    engine.retry(first)
    j,_=wait_job(engine,first)
    assert j['status']=='completed',j['error']


def test_restore_and_api_validation(tmp_path,fixture_url):
    home=tmp_path/'app';home.mkdir()
    app=create_app(home)
    with TestClient(app,base_url='http://127.0.0.1:8765') as client:
        state=client.get('/api/state').json()
        headers={'X-MediaDock-Token':state['token']}
        assert client.post('/api/inspect',json={'urls':[fixture_url+'/direct.html']}).status_code==403
        assert client.get('/api/state',headers={'Host':'foreign.example'}).status_code==403
        assert client.post('/api/inspect',json={'urls':[fixture_url+'/direct.html']},headers={**headers,'Origin':'https://foreign.example'}).status_code==403
        config={**state['settings'],'app_name':'Restored local app','save_dir':str(home/'outputs'),'concurrency':1,
                'defaults':Options(kind='audio',audio_format='m4a').model_dump()}
        assert client.put('/api/settings',json=config,headers=headers).status_code==200
        bad={**config,'filename':'{title.__class__}'}
        assert client.put('/api/settings',json=bad,headers=headers).status_code==422
        result=client.post('/api/inspect',json={'urls':[fixture_url+'/does-not-exist']},headers=headers).json()
        assert result[0]['type']=='error'
        result=client.post('/api/inspect',json={'urls':[fixture_url+'/direct.html']},headers=headers).json()[0]
        r=client.post('/api/jobs',json={'items':[{'media_id':result['media_id'],'options':Options().model_dump()}]},headers=headers)
        jid=r.json()[0]['id']
        job,_=wait_job(app.state.engine,jid)
        assert job['status']=='completed',job['error']
        response=client.get(f'/api/jobs/{jid}/files/0')
        assert response.status_code==200 and len(response.content)==job['files'][0]['size']
        assert client.get(f'/api/jobs/{jid}/files/-1').status_code==404
    app2=create_app(home)
    with TestClient(app2,base_url='http://127.0.0.1:8765') as client:
        restored=client.get('/api/state').json()
        assert restored['settings']['app_name']=='Restored local app'
        assert restored['settings']['defaults']['audio_format']=='m4a'
        assert restored['stats']['completed']==1
        assert client.get(f'/api/jobs/{jid}/files/0').status_code==200


def test_cancel_kills_process_tree():
    import os
    parent = spawn([sys.executable, '-u', '-c',
                    "import subprocess,sys,time; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); print(p.pid,flush=True); time.sleep(60)"])
    child_pid = int(parent.stdout.readline().strip())
    kill_tree(parent)
    assert parent.poll() is not None
    if os.name == 'nt':
        import ctypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel.OpenProcess(0x1000, False, child_pid)
        if handle:
            code = ctypes.c_ulong()
            kernel.GetExitCodeProcess(handle, ctypes.byref(code)); kernel.CloseHandle(handle)
            assert code.value != 259  # STILL_ACTIVE


def test_remember_disabled_and_crash_recovery(tmp_path,fixture_url):
    first=Engine(tmp_path)
    first.save_settings(Settings(save_dir=str(tmp_path/'outputs'),remember=False,defaults=Options(kind='audio')))
    info=first.inspect(fixture_url+'/direct.html')
    jid=first.enqueue([QueueItem(media_id=info['media_id'],options=Options())])[0]['id']
    first.update(jid,status='running')  # Persisted pre-crash state; no subprocess is launched by this fixture.
    first.close()
    restored=Engine(tmp_path)
    assert restored.settings.defaults.kind=='video'
    restored.start()
    assert restored.get_job(jid)['status']=='interrupted'
    restored.retry(jid)
    job,_=wait_job(restored,jid)
    assert job['status']=='completed',job['error']
    restored.close()


def test_two_workers_and_atomic_duplicate(engine,fixture_url):
    from concurrent.futures import ThreadPoolExecutor
    engine.save_settings(engine.settings.model_copy(update={'concurrency':2,'speed_kib':16}))
    info=engine.inspect(fixture_url+'/direct.html')
    item=QueueItem(media_id=info['media_id'],options=Options())
    with ThreadPoolExecutor(max_workers=4) as pool:
        results=list(pool.map(lambda _: engine.enqueue([item])[0],range(4)))
    assert sum(not r['duplicate'] for r in results)==1
    second=engine.enqueue([QueueItem(media_id=info['media_id'],options=Options(container='mkv'))])[0]['id']
    third=engine.enqueue([QueueItem(media_id=info['media_id'],options=Options(kind='audio'))])[0]['id']
    deadline=time.monotonic()+10
    while time.monotonic()<deadline and len(engine.active)<2:time.sleep(.05)
    assert len(engine.active)==2
    assert engine.get_job(third)['status']=='queued'
    engine.cancel(third)
    for jid in list(engine.active):engine.cancel(jid)
