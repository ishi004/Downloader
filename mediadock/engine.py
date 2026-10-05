import hashlib
import json
import os
import secrets
import shutil
import sqlite3
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from . import media
from .models import ROOT, Options, Settings, safe_name, validate_url


class Cancelled(Exception):
    pass


class Engine:
    def __init__(self, home=ROOT):
        self.home = Path(home)
        self.data = self.home / 'data'
        self.data.mkdir(parents=True, exist_ok=True)
        self.config = self.home / 'config.json'
        self.settings = Settings.model_validate_json(self.config.read_text('utf-8')) if self.config.exists() else Settings()
        self.token = secrets.token_urlsafe(32)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.data / 'history.sqlite3', check_same_thread=False)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, fingerprint TEXT, status TEXT, created REAL, body TEXT)')
        self.db.execute('CREATE INDEX IF NOT EXISTS fingerprint_idx ON jobs(fingerprint)')
        self.db.execute('CREATE TABLE IF NOT EXISTS metadata (id TEXT PRIMARY KEY, body TEXT, created REAL)')
        self.db.commit()
        self.active = {}
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.health = media.diagnostics()
        self.scheduler = None

    def save_settings(self, settings):
        path = Path(settings.save_dir)
        path.mkdir(parents=True, exist_ok=True)
        test = path / ('.mediadock-' + uuid.uuid4().hex)
        with test.open('x') as f:
            f.write('')
        test.unlink()
        stored = settings.model_dump()
        if not settings.remember:
            stored['defaults'] = Options().model_dump()
        with self.lock:
            tmp = self.config.with_suffix('.tmp')
            tmp.write_text(json.dumps(stored, ensure_ascii=False, indent=2), 'utf-8')
            tmp.replace(self.config)
            self.settings = settings
        self.wake.set()

    def start(self):
        # Do not run two schedulers against the same SQLite history.
        self.guard = (self.data / 'instance.lock').open('a+b')
        try:
            if os.name == 'nt':
                import msvcrt
                self.guard.seek(0); self.guard.write(b'0'); self.guard.flush(); self.guard.seek(0)
                msvcrt.locking(self.guard.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.guard.close()
            raise RuntimeError('같은 폴더에서 MediaDock이 이미 실행 중입니다.')
        for job in self.jobs():
            if job['status'] in ('running', 'processing', 'cancelling'):
                self.update(job['id'], status='interrupted', error='앱이 종료되어 중단됐습니다. 재시도할 수 있습니다.', stage='중단됨')
        if not self.config.exists():
            self.save_settings(self.settings)
        self.scheduler = threading.Thread(target=self.schedule, daemon=True)
        self.scheduler.start()

    def close(self):
        self.stop.set(); self.wake.set()
        with self.lock:
            ids = list(self.active)
        for jid in ids:
            self.cancel(jid)
        if self.scheduler:
            self.scheduler.join(timeout=5)
        deadline = time.time() + 25
        while self.active and time.time() < deadline:
            time.sleep(.05)
        if hasattr(self, 'guard'):
            self.guard.close()
        self.db.close()

    def jobs(self):
        with self.lock:
            return [json.loads(row[0]) for row in self.db.execute('SELECT body FROM jobs ORDER BY created DESC').fetchall()]

    def get_job(self, jid):
        with self.lock:
            row = self.db.execute('SELECT body FROM jobs WHERE id=?', (jid,)).fetchone()
        if not row:
            raise KeyError('작업을 찾을 수 없습니다.')
        return json.loads(row[0])

    def update(self, jid, **updates):
        with self.lock:
            job = self.get_job(jid)
            job.update(updates)
            job['updated'] = time.time()
            self.db.execute('UPDATE jobs SET status=?, body=? WHERE id=?', (job['status'], json.dumps(job, ensure_ascii=False), jid))
            self.db.commit()
        return job

    def store_info(self, info, url):
        # Generic HTML5 playlist entries may need their resolved direct source URL to select exactly one item.
        key = uuid.uuid4().hex
        info['_app_source_url'] = url
        with self.lock:
            self.db.execute('INSERT INTO metadata VALUES (?,?,?)', (key, json.dumps(info), time.time()))
            self.db.execute('DELETE FROM metadata WHERE created < ?', (time.time() - 7 * 86400,))
            self.db.commit()
        return media.media_summary(info, url, key)

    def get_info(self, key):
        with self.lock:
            row = self.db.execute('SELECT body FROM metadata WHERE id=?', (key,)).fetchone()
        if not row:
            raise ValueError('조회 정보가 만료되었습니다. 링크를 다시 조회하세요.')
        return json.loads(row[0])

    def inspect(self, url, playlist=True):
        info = media.inspect_url(validate_url(url), playlist)
        if info.get('_type') in ('playlist', 'multi_video'):
            entries = []
            for i, entry in enumerate(info.get('entries') or []):
                if not entry:
                    continue
                link = entry.get('webpage_url') or entry.get('url')
                if not link or not link.startswith(('http://', 'https://')):
                    if entry.get('ie_key') == 'Youtube' and entry.get('id'):
                        link = 'https://www.youtube.com/watch?v=' + entry['id']
                    else:
                        continue
                item = {'url': link, 'title': entry.get('title') or f'항목 {i+1}', 'duration': entry.get('duration'),
                        'thumbnail': entry.get('thumbnail') or next((t['url'] for t in reversed(entry.get('thumbnails') or []) if t.get('url')), None)}
                if entry.get('formats'):
                    entry['_app_playlist_index'] = i + 1
                    item = self.store_info(entry, url)
                entries.append(item)
            return {'type': 'playlist', 'title': info.get('title') or '재생목록', 'entries': entries,
                    'limited': len(info.get('entries') or []) >= 200, 'url': url}
        if info.get('is_live'):
            raise ValueError('진행 중인 라이브 방송은 지원하지 않습니다. 종료된 영상 링크를 사용하세요.')
        return {'type': 'media', **self.store_info(info, url)}

    def enqueue(self, items):
        if not self.health['ready']:
            raise ValueError('필수 도구 점검에 실패했습니다. 도구 상태를 확인하세요.')
        prepared = []
        for item in items:
            info = self.get_info(item.media_id)
            opts = item.options
            media.select_formats(info, opts)
            if info.get('is_live'):
                raise ValueError('라이브 방송은 지원하지 않습니다.')
            if info.get('duration') and (opts.start >= info['duration'] or (opts.end and opts.end > info['duration'] + .05)):
                raise ValueError('구간 시간이 영상 길이를 벗어납니다.')
            if opts.subtitle_mode != 'none':
                tracks = info.get('automatic_captions' if opts.auto_subtitles else 'subtitles', {})
                if not tracks.get(opts.subtitle_lang):
                    raise ValueError('선택한 언어·종류의 자막이 없습니다.')
            prepared.append((info, opts))
        results = []
        with self.lock:
            for info, opts in prepared:
                settings = self.settings.model_dump()
                output = {k: settings[k] for k in ('save_dir', 'subfolder', 'by_channel', 'filename')}
                identity = [info.get('extractor_key') or info.get('extractor'), info.get('id') or info['_app_source_url']]
                fingerprint = hashlib.sha256(json.dumps([identity, opts.normalized(), output], sort_keys=True).encode()).hexdigest()
                rows = self.db.execute("SELECT body FROM jobs WHERE fingerprint=? AND status IN ('queued','running','processing','cancelling','completed')", (fingerprint,)).fetchall()
                duplicates = [json.loads(r[0]) for r in rows]
                duplicate = next((j for j in duplicates if j['status'] != 'completed' or all(Path(f['path']).is_file() for f in j['files'])), None)
                if duplicate:
                    results.append({'id': duplicate['id'], 'duplicate': True}); continue
                jid = uuid.uuid4().hex
                job = {'id': jid, 'fingerprint': fingerprint, 'status': 'queued', 'created': time.time(), 'updated': time.time(),
                       'url': info['_app_source_url'], 'title': info.get('title') or '제목 없음',
                       'playlist_index': info.get('_app_playlist_index'),
                       'channel': info.get('channel') or info.get('uploader') or '미분류', 'video_id': info.get('id') or jid[:8],
                       'thumbnail': info.get('thumbnail'), 'duration': info.get('duration'), 'options': opts.model_dump(),
                       'settings': settings, 'stage': '대기 중', 'progress': None, 'speed': None, 'eta': None,
                       'files': [], 'error': None, 'warnings': []}
                self.db.execute('INSERT INTO jobs VALUES (?,?,?,?,?)', (jid, fingerprint, 'queued', job['created'], json.dumps(job, ensure_ascii=False)))
                self.db.commit()
                results.append({'id': jid, 'duplicate': False})
        self.wake.set()
        return results

    def schedule(self):
        while not self.stop.is_set():
            with self.lock:
                slots = self.settings.concurrency - len(self.active)
                queued = [j for j in reversed(self.jobs()) if j['status'] == 'queued'][:max(0, slots)]
                for job in queued:
                    ctx = {'cancel': threading.Event(), 'process': None}
                    self.active[job['id']] = ctx
                    self.update(job['id'], status='running', stage='정보 새로 확인 중')
                    threading.Thread(target=self.run_job, args=(job['id'], ctx), daemon=True).start()
            self.wake.wait(.5); self.wake.clear()

    def cancel(self, jid):
        with self.lock:
            job = self.get_job(jid)
            if job['status'] not in ('queued', 'running', 'processing', 'cancelling'):
                return job
            ctx = self.active.get(jid)
            self.update(jid, status='cancelling' if ctx else 'cancelled', stage='취소 중' if ctx else '취소됨')
            if ctx:
                ctx['cancel'].set()
                process = ctx['process']
            else:
                process = None
        media.kill_tree(process)
        return self.get_job(jid)

    def retry(self, jid):
        with self.lock:
            job = self.get_job(jid)
            if job['status'] not in ('failed', 'cancelled', 'interrupted') or jid in self.active:
                raise ValueError('실패·취소·중단된 작업만 재시도할 수 있습니다.')
            for other in self.jobs():
                if other['id'] != jid and other['fingerprint'] == job['fingerprint'] and other['status'] in ('queued','running','processing','completed'):
                    raise ValueError('같은 출력 설정의 작업이 이미 있습니다.')
            self.update(jid, status='queued', stage='대기 중', error=None, progress=None, speed=None, eta=None)
        self.wake.set()

    def command(self, jid, ctx, args, cwd, callback=None, timeout=86400, capture_output=False):
        with self.lock:
            if ctx['cancel'].is_set():
                raise Cancelled()
            p = media.spawn(args, cwd)
            ctx['process'] = p
        timed_out = threading.Event()
        def expire():
            timed_out.set(); media.kill_tree(p)
        timer = threading.Timer(timeout, expire); timer.daemon = True; timer.start()
        lines = []
        captured, captured_size = [], 0
        try:
            for line in p.stdout:
                if ctx['cancel'].is_set():
                    raise Cancelled()
                line = line.strip()
                if capture_output:
                    captured_size += len(line)
                    if captured_size > 32 * 1024 * 1024:
                        raise RuntimeError('도구의 구조화된 응답이 허용 크기를 초과했습니다.')
                    captured.append(line)
                if callback:
                    callback(line)
                lines.append(line)
                if len(lines) > 80:
                    lines.pop(0)
            p.wait()
            if ctx['cancel'].is_set():
                raise Cancelled()
            if timed_out.is_set():
                raise RuntimeError('작업 시간 제한을 초과했습니다.')
            if p.returncode:
                raise RuntimeError('\n'.join(lines)[-3000:])
            return '\n'.join(captured if capture_output else lines)
        finally:
            timer.cancel()
            media.kill_tree(p)
            with self.lock:
                ctx['process'] = None

    def run_job(self, jid, ctx):
        work = self.data / 'work' / jid
        published = []
        try:
            work.mkdir(parents=True, exist_ok=True)
            job = self.get_job(jid); opts = Options(**job['options'])
            # Refresh expiring URLs and actual available formats immediately before execution.
            playlist_args = ['--playlist-items', str(job['playlist_index'])] if job.get('playlist_index') else ['--no-playlist']
            output = self.command(jid, ctx, media.ytdlp_base() + ['--dump-single-json', '--skip-download', '--no-warnings'] + playlist_args + ['--', job['url']], work, timeout=120, capture_output=True)
            info = json.loads(output)
            if info.get('_type') in ('playlist', 'multi_video'):
                children = [i for i in info.get('entries', []) if i]
                if not job.get('playlist_index') or len(children) != 1:
                    raise ValueError('개별 영상 링크를 다시 조회한 뒤 선택하세요.')
                info = children[0]
            if info.get('is_live'):
                raise ValueError('진행 중인 라이브 방송은 지원하지 않습니다.')
            selector = media.format_selector(info, opts)
            args = media.ytdlp_base() + playlist_args + ['--newline', '--progress', '--progress-delta', '.3',
                    '--progress-template', 'download:MD_PROGRESS:%(progress)j', '--print', 'after_move:MD_FILE:%(filepath)j',
                    '--no-simulate', '--concurrent-fragments', '1', '-f', selector, '--merge-output-format', 'mkv', '-o', 'source.%(ext)s',
                    '--no-write-info-json', '--no-write-playlist-metafiles']
            if self.settings.speed_kib:
                args += ['--limit-rate', str(self.settings.speed_kib * 1024)]
            if opts.thumbnail:
                if not info.get('thumbnail') and not info.get('thumbnails'):
                    raise ValueError('이 링크는 원본 썸네일을 제공하지 않습니다.')
                args += ['--write-thumbnail']
            if opts.subtitle_mode != 'none':
                field = 'automatic_captions' if opts.auto_subtitles else 'subtitles'
                if not info.get(field, {}).get(opts.subtitle_lang):
                    raise ValueError('선택한 자막을 더 이상 제공하지 않습니다.')
                args += ['--write-auto-subs' if opts.auto_subtitles else '--write-subs',
                         '--sub-langs', '^' + __import__('re').escape(opts.subtitle_lang) + '$',
                         '--sub-format', 'srt/vtt/ttml/best', '--convert-subs', 'srt']
            source_paths = []
            def download_progress(line):
                if line.startswith('MD_PROGRESS:'):
                    try:
                        p = json.loads(line[len('MD_PROGRESS:'):])
                        total = p.get('total_bytes') or p.get('total_bytes_estimate')
                        pct = min(100, p.get('downloaded_bytes', 0) / total * 100) if total else None
                        self.update(jid, stage='원본 다운로드', progress=pct, speed=p.get('speed'), eta=p.get('eta'))
                    except (ValueError, TypeError):
                        pass
                elif line.startswith('MD_FILE:'):
                    source_paths.append(Path(json.loads(line[len('MD_FILE:'):])))
            self.update(jid, stage='원본 다운로드', progress=None)
            self.command(jid, ctx, args + ['--', job['url']], work, download_progress)
            if not source_paths:
                raise RuntimeError('다운로드 결과 파일을 확인하지 못했습니다.')
            src = source_paths[-1]
            if not src.is_absolute():
                src = work / src
            if not src.resolve().is_relative_to(work.resolve()) or not src.is_file():
                raise RuntimeError('다운로드 파일 경로 검증에 실패했습니다.')
            details = json.loads(self.command(jid, ctx, [media.binary('ffprobe'), '-v', 'error', '-show_format', '-show_streams', '-of', 'json', str(src)], work, timeout=30, capture_output=True))
            duration = float(details['format'].get('duration') or info.get('duration') or 0)
            if duration and (opts.start >= duration or (opts.end and opts.end > duration + .1)):
                raise ValueError('실제 파일의 재생 시간을 벗어난 구간입니다.')
            duration = (opts.end if opts.end is not None else duration) - opts.start
            sub = None
            if opts.subtitle_mode != 'none':
                srt = list(work.glob('source.*.srt'))
                if not srt:
                    raise RuntimeError('요청한 SRT 자막을 만들지 못했습니다.')
                sub = work / 'captions.srt'
                sub.write_text(media.shift_srt(srt[0].read_text('utf-8-sig'), opts.start, opts.end), 'utf-8')
                if not sub.read_text('utf-8').strip() and opts.subtitle_mode in ('embed', 'burn'):
                    raise ValueError('선택 구간에는 표시할 자막이 없습니다. 별도 SRT 또는 자막 없음을 선택하세요.')
            self.update(jid, status='processing', stage='변환·구간 처리', progress=None, speed=None, eta=None)
            streams = details['streams']
            audio = next((s for s in streams if s['codec_type'] == 'audio'), None)
            video = next((s for s in streams if s['codec_type'] == 'video'), None)
            cut = bool(opts.start or opts.end is not None)
            if opts.kind == 'audio':
                if not audio:
                    raise ValueError('이 파일에는 음원이 없습니다.')
                ext = opts.audio_format
                if ext == 'original':
                    ext = {'aac':'m4a','mp3':'mp3','opus':'opus','vorbis':'ogg','flac':'flac','alac':'m4a','pcm_s16le':'wav'}.get(audio['codec_name'], 'mka')
            else:
                ext = opts.container if opts.container != 'auto' else ('mp4' if opts.subtitle_mode == 'burn' or cut else src.suffix.lstrip('.'))
                if ext not in ('mp4','mkv','webm','mov'):
                    ext = 'mkv'
                if opts.subtitle_mode == 'embed' and ext not in ('mp4','mkv'):
                    ext = 'mkv'
            dest = work / ('result.' + ext)
            cmd = [media.binary('ffmpeg'), '-hide_banner', '-nostdin', '-y', '-loglevel', 'warning', '-progress', 'pipe:1']
            # Input seeking + decode yields accurate cuts when transcoding video/audio.
            if opts.start:
                cmd += ['-ss', str(opts.start)]
            cmd += ['-i', str(src)]
            if sub and opts.subtitle_mode == 'embed':
                cmd += ['-i', 'captions.srt']
            if opts.end is not None:
                cmd += ['-t', str(opts.end - opts.start)]
            if opts.kind == 'audio':
                cmd += ['-map', '0:a:0', '-vn']
                if opts.audio_format == 'mp3':
                    cmd += ['-c:a', 'libmp3lame'] + (['-q:a', str(opts.mp3_quality)] if opts.mp3_mode == 'vbr' else ['-b:a', str(opts.bitrate) + 'k'])
                elif opts.audio_format == 'm4a':
                    cmd += ['-c:a', 'aac', '-b:a', '256k']
                else:
                    if cut:
                        encoder = {'aac':'aac','mp3':'libmp3lame','opus':'libopus','vorbis':'libvorbis','flac':'flac','alac':'alac','pcm_s16le':'pcm_s16le'}.get(audio['codec_name'])
                        if not encoder:
                            raise ValueError('이 원본 코덱은 정확한 구간 인코딩을 지원하지 않습니다. MP3 또는 M4A를 선택하세요.')
                        cmd += ['-c:a', encoder]
                        if audio.get('bit_rate') and encoder not in ('flac','alac','pcm_s16le'):
                            cmd += ['-b:a', str(audio['bit_rate'])]
                    else:
                        cmd += ['-c:a', 'copy']
            else:
                if not video:
                    raise ValueError('이 파일에는 영상이 없습니다.')
                cmd += ['-map', '0:v:0', '-map', '0:a:0?']
                encode_video = cut or opts.subtitle_mode == 'burn' or (ext == 'mp4' and video['codec_name'] not in ('h264', 'hevc', 'av1', 'mpeg4'))
                if encode_video:
                    cmd += ['-c:v', 'libx264', '-preset', 'medium', '-crf', '18', '-pix_fmt', 'yuv420p']
                    filters = ['scale=trunc(iw/2)*2:trunc(ih/2)*2']
                    if opts.subtitle_mode == 'burn':
                        filters += ['subtitles=filename=captions.srt']
                    cmd += ['-vf', ','.join(filters)]
                else:
                    cmd += ['-c:v', 'copy']
                encode_audio = cut or (ext == 'mp4' and audio and audio['codec_name'] not in ('aac','mp3','alac'))
                cmd += ['-c:a', 'aac', '-b:a', '256k'] if encode_audio else ['-c:a', 'copy']
                if sub and opts.subtitle_mode == 'embed':
                    cmd += ['-map', '1:s:0', '-c:s', 'mov_text' if ext == 'mp4' else 'srt', '-metadata:s:s:0', 'title=' + opts.subtitle_lang]
            if ext in ('mp4', 'm4a'):
                cmd += ['-movflags', '+faststart']
            cmd += [str(dest)]
            def conversion_progress(line):
                if line.startswith('out_time_us=') and duration > 0:
                    try:
                        value = float(line.split('=', 1)[1]) / 1000000
                        self.update(jid, progress=max(0, min(100, value / duration * 100)))
                    except ValueError:
                        pass
            self.command(jid, ctx, cmd, work, conversion_progress)
            verified = json.loads(self.command(jid, ctx, [media.binary('ffprobe'), '-v', 'error', '-show_format', '-show_streams', '-of', 'json', str(dest)], work, timeout=30, capture_output=True))
            if not dest.stat().st_size or not verified.get('streams'):
                raise RuntimeError('출력 파일 검사에 실패했습니다.')
            settings = job['settings']; root = Path(settings['save_dir']).resolve()
            folder = root / settings['subfolder']
            if settings['by_channel']:
                folder /= safe_name(job['channel'], 65)
            folder = folder.resolve()
            if not folder.is_relative_to(root):
                raise ValueError('저장 경로가 지정된 폴더를 벗어났습니다.')
            folder.mkdir(parents=True, exist_ok=True)
            name = safe_name(settings['filename'].format(title=job['title'], id=job['video_id'], channel=job['channel'], date=datetime.now().strftime('%Y-%m-%d')), 140)
            name += '-' + jid[:8]
            outputs = [(dest, folder / (name + '.' + ext))]
            if sub and opts.subtitle_mode == 'srt':
                outputs.append((sub, folder / (name + '.' + opts.subtitle_lang + '.srt')))
            if opts.thumbnail:
                thumbs = [p for p in work.glob('source.*') if p.suffix.lower() in ('.jpg','.jpeg','.png','.webp','.avif')]
                if not thumbs:
                    raise RuntimeError('요청한 원본 썸네일이 저장되지 않았습니다.')
                outputs += [(p, folder / (name + '.thumbnail' + p.suffix)) for p in thumbs]
            # Copy to a sibling temporary file, then publish atomically even across drives.
            for source, target in outputs:
                if ctx['cancel'].is_set():
                    raise Cancelled()
                temp = target.with_name(target.name + '.pending')
                published.append(temp)
                shutil.copyfile(source, temp)
                if target.exists():
                    raise RuntimeError('기존 파일을 덮어쓸 수 없습니다.')
                temp.replace(target); published.remove(temp); published.append(target)
            with self.lock:
                if ctx['cancel'].is_set():
                    raise Cancelled()
                self.update(jid, status='completed', stage='완료', progress=100, speed=None, eta=None,
                            files=[{'name': p.name, 'path': str(p), 'size': p.stat().st_size} for p in published],
                            output_duration=float(verified['format'].get('duration') or 0),
                            output_streams=[{'type': s['codec_type'], 'codec': s.get('codec_name'), 'height': s.get('height'), 'fps': s.get('avg_frame_rate')} for s in verified['streams']])
        except Cancelled:
            for p in published:
                p.unlink(missing_ok=True)
            self.update(jid, status='cancelled', stage='취소됨', progress=None, speed=None, eta=None)
        except Exception as e:
            for p in published:
                p.unlink(missing_ok=True)
            self.update(jid, status='failed', stage='실패', error=str(e)[-3000:], speed=None, eta=None)
        finally:
            # work is an internally generated UUID folder, never a user-provided path.
            if work.resolve().parent == (self.data / 'work').resolve():
                shutil.rmtree(work, ignore_errors=True)
            with self.lock:
                self.active.pop(jid, None)
            self.wake.set()
