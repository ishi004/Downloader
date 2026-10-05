"""Download official Windows FFmpeg build; preserve provenance and bundled licenses."""
import hashlib
import json
import platform
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / 'tools'
TOOLS.mkdir(exist_ok=True)

def fetch(url):
    return urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': 'MediaDock-local-setup'}), timeout=120)

def main():
    if sys.platform != 'win32' or platform.machine().lower() not in ('amd64', 'x86_64'):
        raise SystemExit('This installer targets Windows x64. Install ffmpeg/ffprobe in PATH on other platforms.')
    with fetch('https://api.github.com/repos/yt-dlp/FFmpeg-Builds/releases/latest') as r:
        release = json.load(r)
    asset = next(a for a in release['assets'] if a['name'] == 'ffmpeg-master-latest-win64-gpl.zip')
    archive = TOOLS / asset['name']
    expected = asset.get('digest')
    cache_valid = archive.exists() and (not expected or expected == 'sha256:' + hashlib.sha256(archive.read_bytes()).hexdigest())
    if not cache_valid:
        print('Downloading', asset['browser_download_url'], flush=True)
        pending = archive.with_suffix('.download')
        with fetch(asset['browser_download_url']) as r, pending.open('wb') as f:
            shutil.copyfileobj(r, f)
        candidate_digest = hashlib.sha256(pending.read_bytes()).hexdigest()
        if expected and expected != 'sha256:' + candidate_digest:
            raise SystemExit('SHA256 mismatch; downloaded file was not installed')
        pending.replace(archive)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    if expected and expected != 'sha256:' + digest:
        raise SystemExit('SHA256 mismatch; remove downloaded archive and retry')
    with zipfile.ZipFile(archive) as z:
        for entry in z.infolist():
            target = (TOOLS / entry.filename).resolve()
            if not target.is_relative_to(TOOLS.resolve()):
                raise SystemExit('Unsafe archive path')
        z.extractall(TOOLS)
    bindir = next(TOOLS.glob('ffmpeg-*/bin'))
    report = {'release': release['html_url'], 'asset': asset['browser_download_url'], 'asset_id': asset['id'],
              'asset_updated_at': asset['updated_at'], 'sha256': digest,
              'github_digest_verified': bool(expected), 'ffmpeg_dir': str(bindir.relative_to(ROOT)),
              'source': 'https://github.com/yt-dlp/FFmpeg-Builds',
              'redistribution': 'GPL build. Binaries excluded from app source; retain licenses and corresponding source if redistributing.'}
    for name, args in [('ffmpeg', ['-version']), ('ffprobe', ['-version'])]:
        report[name] = subprocess.check_output([str(bindir / (name + '.exe')), *args], text=True, encoding='utf-8')
    report['yt_dlp'] = subprocess.check_output([sys.executable, '-m', 'yt_dlp', '--version'], text=True).strip()
    report['yt_dlp_source'] = 'https://pypi.org/project/yt-dlp/' + report['yt_dlp'] + '/'
    (TOOLS / 'provenance.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))

if __name__ == '__main__':
    main()
