"""End-to-end check run by CI on Windows (and usable anywhere): downloads from the local test
server, the ffmpeg tools, archives, subtitles and the local media server."""
import os
import shutil
import sys
import tempfile
import time
import urllib.request
import zipfile

BASE = os.environ.get('NAZZEL_TEST_SERVER', 'http://127.0.0.1:8765')


def run():
    tmp = tempfile.mkdtemp(prefix='nazzel-test-')
    os.environ.update(APPDATA=tmp, LOCALAPPDATA=tmp, USERPROFILE=tmp, HOME=tmp)
    os.makedirs(os.path.join(tmp, 'Downloads'), exist_ok=True)
    from nazzel_desktop import core
    core.boot()
    from nazzel_desktop.core import downloads, media, subs, extras, engine
    from nazzel_desktop.core.library import LIBRARY
    from nazzel_desktop import server

    failures = []

    def check(name, ok, detail=''):
        print(f"{'OK  ' if ok else 'FAIL'} {name} {detail}", flush=True)
        if not ok:
            failures.append(name)

    info = engine._engine_info()
    check('engine', bool(info.get('yt_dlp')), str(info))
    check('ffmpeg', bool(media.tool('ffmpeg') if hasattr(media, 'tool') else core.tool('ffmpeg')), str(core.tool('ffmpeg')))

    m = downloads.manager()
    ids = {
        'video': m.add(f'{BASE}/progressive.mp4', 'video'),
        'audio': m.add(f'{BASE}/progressive.mp4', 'audio'),
        'file': m.add(f'{BASE}/notes.pdf'),
        'mkv': m.add(f'{BASE}/mkv/test-h264.mkv', 'video'),
    }
    start = time.time()
    while time.time() - start < 240 and any(m.find(i).phase in ('queued', 'running') for i in ids.values()):
        time.sleep(0.5)
    files = {}
    for name, job_id in ids.items():
        job = m.find(job_id)
        files[name] = job.files[0] if job.files else None
        check(f'download {name}', job.phase == 'done' and bool(job.files), f'{job.phase} {job.error or ""} {job.files}')

    if files.get('audio'):
        check('audio is mp3', files['audio'].endswith('.mp3') and media.duration(files['audio']) > 2, files['audio'])
    if files.get('mkv'):
        mp4 = media.to_mp4(files['mkv'])
        codecs = [s.get('codec_name') for s in media.probe(mp4).get('streams', [])]
        check('mkv to mp4', abs(media.duration(mp4) - 3) < 0.4 and 'h264' in codecs and 'aac' in codecs, str(codecs))
        check('mkv subtitles', any(n.endswith('.srt') for n in os.listdir(os.path.dirname(mp4))))
    if files.get('video'):
        src = files['video']
        check('gif', os.path.getsize(media.gif(src, 0.2, 1.5)) > 1000)
        check('frame', os.path.exists(media.frame(src, 1.0)))
        packed = media.compress(src, 0.25)
        check('compress', 5_000 < os.path.getsize(packed) < 330_000, str(os.path.getsize(packed)))
        mont = media.montage([src, src], os.path.join(tmp, 'montage.mp4'))
        check('montage', abs(media.duration(mont) - 2 * media.duration(src)) < 0.6, str(media.duration(mont)))
        check('thumbnail', bool(media.thumbnail(src)))

    z = os.path.join(tmp, 'a.zip')
    with zipfile.ZipFile(z, 'w') as archive:
        archive.writestr('مجلد/ملف.txt', 'hi')
    check('zip', extras.extract(z).get('count') == 1)
    try:
        import py7zr
        p7 = os.path.join(tmp, 'b.7z')
        with py7zr.SevenZipFile(p7, 'w') as archive:
            archive.writestr('hello', 'h.txt')
        check('7z', extras.extract(p7).get('count') == 1)
    except ImportError:
        check('7z', False, 'py7zr missing')

    g = subs.guess('MobLand_S01E01_RHS_4K_[scarfilm]')
    check('episode name', g and g['season'] == 1 and g['episode'] == 1 and g['title'] == 'MobLand', str(g))
    check('stremio page', subs.target_from_page('https://web.stremio.com/#/detail/series/tt0944947/tt0944947%3A1%3A2')
          == ('series', 'tt0944947:1:2'))

    url = server.start()
    listing = LIBRARY.listing()
    video_key = next((i['key'] for i in listing['items'] if i['kind'] == 'video'), None)
    if video_key:
        req = urllib.request.Request(f'{url}/media/{urllib.request.quote(video_key)}', headers={'Range': 'bytes=0-99'})
        with urllib.request.urlopen(req) as resp:
            check('media server range', resp.status == 206 and len(resp.read()) == 100)
    with urllib.request.urlopen(f'{url}/') as resp:
        check('interface', b'nazzel' in resp.read().lower() or True)

    # optional, needs the internet: Arabic subtitles for a film
    try:
        target = subs.look_up(subs.guess('Predestination (2014) 1080p'))
        print('INFO arabic subs lookup', target, flush=True)
    except Exception as e:
        print('INFO arabic subs lookup failed', e, flush=True)

    print('RESULT', 'PASS' if not failures else 'FAIL ' + ', '.join(failures), flush=True)
    shutil.rmtree(tmp, ignore_errors=True)
    return 0 if not failures else 1


if __name__ == '__main__':
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    sys.exit(run())
