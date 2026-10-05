"""Self-generated, freely reusable media for local end-to-end tests. Never seeded into app history."""
import functools
import http.server
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mediadock.media import binary, capture

FIXTURE = Path(__file__).resolve().parents[1] / 'test-results' / 'fixtures'

def make_fixture():
    FIXTURE.mkdir(parents=True, exist_ok=True)
    video = FIXTURE / 'sample.mp4'
    if not video.exists():
        capture([binary('ffmpeg'), '-v', 'error', '-y', '-f', 'lavfi', '-i', 'testsrc2=size=640x360:rate=30',
                 '-f','lavfi','-i','sine=frequency=440:sample_rate=48000', '-t','8','-c:v','libx264',
                 '-preset','ultrafast','-crf','20','-pix_fmt','yuv420p','-c:a','aac','-movflags','+faststart',str(video)], 60)
        for height, rate in [(360,30),(180,15)]:
            capture([binary('ffmpeg'),'-v','error','-y','-i',str(video),'-vf',f'scale=-2:{height},fps={rate}',
                     '-c:v','libx264','-preset','ultrafast','-g',str(rate*2),'-c:a','aac','-f','hls','-hls_time','2',
                     '-hls_list_size','0',str(FIXTURE / f'{height}.m3u8')],60)
        capture([binary('ffmpeg'),'-v','error','-y','-i',str(video),'-frames:v','1',str(FIXTURE/'cover.jpg')],30)
    (FIXTURE/'master.m3u8').write_text('#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1500000,RESOLUTION=640x360,FRAME-RATE=30.000\n360.m3u8\n#EXT-X-STREAM-INF:BANDWIDTH=500000,RESOLUTION=320x180,FRAME-RATE=15.000\n180.m3u8\n','utf-8')
    (FIXTURE/'captions.vtt').write_text('WEBVTT\n\n00:00:01.000 --> 00:00:03.500\nMediaDock subtitle one\n\n00:00:04.000 --> 00:00:07.000\nMediaDock subtitle two\n','utf-8')
    for filename, source, title in [('single.html','master.m3u8','MediaDock authorized test'),('direct.html','sample.mp4','MediaDock MP4 test')]:
        (FIXTURE/filename).write_text(f'<html><head><title>{title}</title><meta name="author" content="MediaDock test suite"></head><body><video controls poster="cover.jpg"><source src="{source}" type="'+('application/x-mpegURL' if source.endswith('m3u8') else 'video/mp4')+'"><track kind="subtitles" src="captions.vtt" srclang="en" label="English"></video></body></html>','utf-8')
    (FIXTURE/'playlist.html').write_text('<html><head><title>MediaDock authorized playlist</title></head><body><video src="sample.mp4" title="First test" controls></video><video src="sample.mp4?second=1" title="Second test" controls></video></body></html>','utf-8')
    return FIXTURE

class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass

def server(port=9876):
    root = make_fixture()
    return http.server.ThreadingHTTPServer(('127.0.0.1', port), functools.partial(QuietHandler,directory=str(root)))

if __name__ == '__main__':
    s = server()
    print('Authorized generated fixture: http://127.0.0.1:9876/single.html',flush=True)
    s.serve_forever()
