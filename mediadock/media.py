import json
import os
import re
import shutil
import signal
import subprocess
import sys
from pathlib import Path

from .models import ROOT, Options

CREATE_FLAGS = (subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP) if os.name == 'nt' else 0


def binary(name):
    candidates = sorted((ROOT / 'tools').glob(f'ffmpeg-*/bin/{name}.exe'))
    found = str(candidates[-1]) if candidates else shutil.which(name)
    if not found:
        raise RuntimeError(f'{name} 실행 파일이 없습니다. setup.ps1을 실행하세요.')
    return found


def ytdlp_base():
    return [sys.executable, '-m', 'yt_dlp', '--ignore-config', '--no-plugin-dirs', '--no-colors',
            '--encoding', 'utf-8', '--socket-timeout', '20', '--retries', '3', '--fragment-retries', '3',
            '--js-runtimes', 'node', '--ffmpeg-location', str(Path(binary('ffmpeg')).parent)]


def spawn(args, cwd=None):
    return subprocess.Popen(args, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding='utf-8', errors='replace', creationflags=CREATE_FLAGS,
                            start_new_session=os.name != 'nt', env={**os.environ, 'PYTHONIOENCODING': 'utf-8'})


def kill_tree(p):
    if p and p.poll() is None:
        if os.name == 'nt':
            subprocess.run(['taskkill', '/PID', str(p.pid), '/T', '/F'], capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
        else:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        p.wait(timeout=15)


def capture(args, timeout=120):
    p = spawn(args)
    try:
        output, _ = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_tree(p)
        raise RuntimeError('조회 시간이 초과되었습니다. 링크와 네트워크 상태를 확인하세요.')
    if p.returncode:
        raise RuntimeError(output[-2500:].strip())
    return output


def inspect_url(url, playlist=True):
    args = ytdlp_base() + ['--dump-single-json', '--skip-download', '--no-warnings']
    args += ['--flat-playlist', '--playlist-end', '200'] if playlist else ['--no-playlist']
    out = capture(args + ['--', url])
    return json.loads(out)


def media_summary(info, source_url, key):
    formats = [f for f in info.get('formats', []) if not f.get('has_drm')]
    videos = [f for f in formats if f.get('vcodec') != 'none']
    heights = sorted({f['height'] for f in videos if f.get('height')}, reverse=True)
    langs = [{'code': lang, 'name': next((v.get('name') for v in tracks if v.get('name')), lang), 'auto': auto}
             for field, auto in [('subtitles', False), ('automatic_captions', True)]
             for lang, tracks in info.get(field, {}).items() if tracks and re.fullmatch(r'[A-Za-z0-9_.-]{1,40}', lang)]
    return {'media_id': key, 'id': info.get('id'), 'url': source_url, 'title': info.get('title') or '제목 없음',
            'channel': info.get('channel') or info.get('uploader') or '채널 정보 없음', 'thumbnail': info.get('thumbnail'),
            'duration': info.get('duration'), 'heights': heights,
            'fps_by_height': {str(h): sorted({f['fps'] for f in videos if f.get('height') == h and f.get('fps')}, reverse=True) for h in heights},
            'subtitles': langs, 'has_video': bool(videos),
            'has_audio': any(f.get('acodec') != 'none' for f in formats),
            'is_live': info.get('is_live', False)}


def select_formats(info, opts: Options):
    formats = [f for f in info.get('formats', []) if not f.get('has_drm')]
    # yt-dlp sorts formats worst -> best; use its extractor's order rather than inventing codec quality.
    if opts.kind == 'audio':
        audio = [f for f in formats if f.get('acodec') != 'none' and f.get('vcodec') == 'none']
        if not audio:
            audio = [f for f in formats if f.get('acodec') != 'none']
        if not audio:
            raise ValueError('다운로드할 오디오 스트림이 없습니다.')
        return [audio[-1]]
    videos = [f for f in formats if f.get('vcodec') != 'none']
    if opts.height is not None:
        videos = [f for f in videos if f.get('height') == opts.height]
    if opts.fps is not None:
        videos = [f for f in videos if f.get('fps') and abs(f['fps'] - opts.fps) < .01]
    if not videos:
        raise ValueError('선택한 해상도 또는 프레임률을 실제로 지원하지 않습니다.')
    video = videos[-1]
    if video.get('acodec') != 'none':
        return [video]
    audios = [f for f in formats if f.get('vcodec') == 'none' and f.get('acodec') != 'none']
    return [video, audios[-1]] if audios else [video]


def estimate(info, opts):
    fs = select_formats(info, opts)
    total, bases = 0, []
    for f in fs:
        if f.get('filesize'):
            total += f['filesize']; bases.append('제공된 파일 크기')
        elif f.get('filesize_approx'):
            total += f['filesize_approx']; bases.append('사이트의 추정 크기')
        elif f.get('tbr') and info.get('duration'):
            total += f['tbr'] * 1000 / 8 * info['duration']; bases.append('비트레이트 × 재생 시간')
        else:
            return {'bytes': None, 'basis': '정보 없음'}
    return {'bytes': round(total), 'basis': ' + '.join(dict.fromkeys(bases)),
            'note': '다운로드 원본 기준. 구간·변환·자막 처리 후 최종 크기는 달라집니다.'}


def format_selector(info, opts):
    ids = [str(f['format_id']) for f in select_formats(info, opts)]
    if any(not re.fullmatch(r'[A-Za-z0-9_.-]+', i) for i in ids):
        raise ValueError('이 사이트의 형식 ID는 안전한 선택 문법으로 처리할 수 없습니다.')
    return '+'.join(ids)


def probe(path):
    return json.loads(capture([binary('ffprobe'), '-v', 'error', '-show_format', '-show_streams', '-of', 'json', str(path)], 30))


def shift_srt(text, start, end):
    def seconds(s):
        h, m, s = s.replace(',', '.').split(':')
        return int(h) * 3600 + int(m) * 60 + float(s)
    def timestamp(t):
        ms = max(0, round(t * 1000)); h, ms = divmod(ms, 3600000); m, ms = divmod(ms, 60000); s, ms = divmod(ms, 1000)
        return f'{h:02}:{m:02}:{s:02},{ms:03}'
    result = []
    for block in re.split(r'\n\s*\n', text.replace('\r\n', '\n').strip()):
        lines = block.splitlines()
        idx = next((i for i, line in enumerate(lines) if '-->' in line), None)
        if idx is None:
            continue
        match = re.match(r'(\d+:\d+:\d+[,.]\d+)\s+-->\s+(\d+:\d+:\d+[,.]\d+)', lines[idx])
        if not match:
            continue
        a, b = map(seconds, match.groups())
        a, b = max(a, start), min(b, end if end is not None else float('inf'))
        if b <= a:
            continue
        result.append(f'{len(result)+1}\n{timestamp(a-start)} --> {timestamp(b-start)}\n' + '\n'.join(lines[idx+1:]))
    return '\n\n'.join(result) + ('\n' if result else '')


def diagnostics():
    result = {'ready': False}
    try:
        result['yt_dlp'] = capture([sys.executable, '-m', 'yt_dlp', '--version'], 20).strip()
        result['ffmpeg'] = capture([binary('ffmpeg'), '-version'], 20).splitlines()[0]
        result['ffprobe'] = capture([binary('ffprobe'), '-version'], 20).splitlines()[0]
        encoders = capture([binary('ffmpeg'), '-hide_banner', '-encoders'], 20)
        filters = capture([binary('ffmpeg'), '-hide_banner', '-filters'], 20)
        result['encoders'] = {name: bool(re.search(r'\s' + name + r'\s', encoders)) for name in ['libx264', 'aac', 'libmp3lame', 'libopus', 'libvorbis', 'flac', 'alac', 'pcm_s16le', 'srt', 'mov_text']}
        result['filters'] = {name: bool(re.search(r'\s' + name + r'\s', filters)) for name in ['subtitles', 'trim', 'atrim']}
        result['node'] = capture([shutil.which('node') or 'node', '--version'], 20).strip()
        result['ready'] = all(result['encoders'].values()) and all(result['filters'].values())
    except Exception as e:
        result['error'] = str(e)
    return result
