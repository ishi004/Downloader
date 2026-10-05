import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mediadock.media import ROOT, binary, capture, diagnostics

out = ROOT / 'tools' / 'help'
out.mkdir(parents=True, exist_ok=True)
checks = {
    'yt-dlp-help.txt': [sys.executable, '-m', 'yt_dlp', '--help'],
    'ffmpeg-help.txt': [binary('ffmpeg'), '-hide_banner', '-h', 'full'],
    'ffmpeg-subtitles.txt': [binary('ffmpeg'), '-hide_banner', '-h', 'filter=subtitles'],
    'ffmpeg-encoders.txt': [binary('ffmpeg'), '-hide_banner', '-encoders'],
    'ffmpeg-license.txt': [binary('ffmpeg'), '-L'],
    'ffprobe-help.txt': [binary('ffprobe'), '-hide_banner', '-h'],
}
for name, command in checks.items():
    (out / name).write_text(capture(command, 30), 'utf-8')
report = diagnostics()
(ROOT / 'tools' / 'capabilities.json').write_text(json.dumps(report, indent=2, ensure_ascii=False), 'utf-8')
print(json.dumps(report, indent=2, ensure_ascii=False))
sys.exit(0 if report['ready'] else 1)
