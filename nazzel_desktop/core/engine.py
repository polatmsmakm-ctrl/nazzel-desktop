"""
Nazzel download engine.

A thin layer over yt-dlp that the iOS app calls through the C bridge.
Every call goes through `call(name, json_text)` and returns a JSON string:
    {"ok": true, ...}                     on success
    {"ok": false, "error": "...", ...}    on failure

iOS has no ffmpeg, so the engine never merges anything itself. When the best
quality needs a separate video and audio stream, it downloads both files and
returns a "merge" item; the app merges them natively with AVFoundation.
"""

import contextlib
import hashlib
import http.client
import importlib
import json
import os
import re
import shutil
import ssl
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import zipfile

ENGINE_VERSION = 3

# Packages the in-app updater keeps fresh from PyPI (all pure Python).
UPDATABLE_PACKAGES = ['yt-dlp', 'yt-dlp-ejs', 'yt-dlp-apple-webkit-jsi', 'gallery-dl', 'certifi']
_PURGE_PREFIXES = ('yt_dlp', 'yt_dlp_ejs', 'yt_dlp_plugins', 'gallery_dl', 'certifi')

_STATE = {
    'documents': None,
    'caches': None,
    'engine_dir': None,
    'engine_site': None,
    'ready': False,
}
_JOBS = {}
_JOBS_LOCK = threading.Lock()
_IMPORT_LOCK = threading.Lock()
_GLOBAL_LOG = []
_LOG_LIMIT = 600


class Cancelled(Exception):
    pass


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _log(line):
    line = str(line)
    _GLOBAL_LOG.append(time.strftime('%H:%M:%S ') + line)
    if len(_GLOBAL_LOG) > _LOG_LIMIT:
        del _GLOBAL_LOG[: len(_GLOBAL_LOG) - _LOG_LIMIT]


def _ssl_context():
    try:
        import certifi
        return ssl.create_default_context(cafile=os.environ.get('NAZZEL_CAFILE') or certifi.where())
    except Exception:
        return ssl.create_default_context()


def _http_get(url, timeout=30):
    req = urllib.request.Request(url, headers={'User-Agent': 'Nazzel/1.0 (+iOS)'})
    with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context()) as resp:
        return resp.read()


def _purge_modules():
    for name in list(sys.modules):
        if name.split('.')[0] in _PURGE_PREFIXES:
            del sys.modules[name]
    importlib.invalidate_caches()
    sys.path_importer_cache.clear()


def _activate_engine_site():
    """Put the updated engine (if any) in front of the bundled packages."""
    site = _STATE['engine_site']
    while site in sys.path:
        sys.path.remove(site)
    if site and os.path.isdir(site) and os.path.exists(os.path.join(site, 'nazzel-engine.json')):
        sys.path.insert(0, site)
        return True
    return False


def _yt_dlp():
    with _IMPORT_LOCK:
        import yt_dlp
        return yt_dlp


def _engine_info():
    info = {'python': sys.version.split()[0], 'platform': sys.platform, 'engine_api': ENGINE_VERSION}
    try:
        yt_dlp = _yt_dlp()
        from yt_dlp.version import __version__ as v
        info['yt_dlp'] = v
        info['yt_dlp_path'] = os.path.dirname(yt_dlp.__file__)
    except Exception as e:
        info['yt_dlp'] = None
        info['yt_dlp_error'] = repr(e)
    try:
        import yt_dlp_ejs
        info['ejs'] = getattr(yt_dlp_ejs, 'version', None)
    except Exception:
        info['ejs'] = None
    try:
        from gallery_dl.version import __version__ as gv
        info['gallery_dl'] = gv
    except Exception:
        info['gallery_dl'] = None
    try:
        from yt_dlp_plugins.extractor.webkit_jsi import __version__ as pv
        info['webkit_jsi'] = pv
    except Exception:
        info['webkit_jsi'] = None
    site = _STATE.get('engine_site')
    info['source'] = 'updated' if site and site in sys.path else 'bundled'
    return info


def _clean_error(msg):
    msg = str(msg or '')
    for junk in ('; please report this issue on', 'Confirm you are on the latest version'):
        if junk in msg:
            msg = msg.split(junk)[0]
    if msg.startswith('ERROR: '):
        msg = msg[7:]
    return msg.strip()


def _is_mpegts(path):
    try:
        with open(path, 'rb') as fh:
            head = fh.read(189)
        return len(head) == 189 and head[0] == 0x47 and head[188] == 0x47
    except OSError:
        return False


def _friendly_error(msg):
    """Short Arabic explanation for the most common failures (full text stays in the log)."""
    m = _clean_error(msg).lower()
    table = [
        (('confirm your age', 'age-restricted', 'age restricted', 'inappropriate for some users', 'age verification',
          'over_18', 'nsfw', 'adult content', 'sensitive content'),
         'المقطع مقيّد بالعمر. سجّل دخولك للموقع من الإعدادات بحساب عمره فوق ١٨ (يوتيوب: حساب مؤكد العمر) وحاول مرة ثانية.'),
        (('not a file',), 'الرابط يفتح صفحة ويب مو ملف قابل للتحميل.'),
        (('unsupported url', 'is not a valid url'), 'الرابط غير مدعوم: ما لقيت فيه فيديو ولا ملف قابل للتحميل.'),
        (('private', 'login required', 'log in', 'sign in', 'cookies', 'authentication', 'rate-limit', 'rate limit'),
         'هذا المحتوى يحتاج تسجيل دخول. سجل دخولك للموقع من الإعدادات وحاول مرة ثانية.'),
        (('not a bot',), 'الموقع طلب تأكيد إنك مو روبوت. سجل دخولك من الإعدادات أو حاول بعد شوي.'),
        (('http error 404', 'not found', 'does not exist', 'unavailable', 'removed'), 'الفيديو غير موجود أو انحذف.'),
        (('http error 403', 'forbidden'), 'الموقع رفض الطلب (403). جرب تحديث المحرك من الإعدادات.'),
        (('no video formats', 'requested format is not available', 'no formats'), 'ما لقيت فيديو قابل للتحميل في هذا الرابط.'),
        (('incomplete download', 'connection closed early', 'range request'), 'انقطع التحميل قبل ما يكتمل. حاول مرة ثانية.'),
        (('no space left',), 'ما في مساحة كافية على الجهاز.'),
        (('timed out', 'timeout', 'temporary failure', 'name resolution', 'network is unreachable', 'connection'),
         'مشكلة في الاتصال بالإنترنت. تأكد من الشبكة وحاول مرة ثانية.'),
        (('geo', 'not available in your country'), 'الفيديو غير متاح في منطقتك.'),
    ]
    for keys, text in table:
        if any(k in m for k in keys):
            return text
    return 'صار خطأ أثناء التحميل. جرب تحديث المحرك من الإعدادات، وإذا استمر انسخ السجل.'


@contextlib.contextmanager
def _cookie_copy(shared, tag):
    """A private copy of the app's cookies.txt for one call.

    yt-dlp writes cookies back when it finishes; with a shared file, parallel downloads
    (or the app exporting fresh cookies) could leave it half written."""
    private = None
    if shared and os.path.exists(shared) and os.path.getsize(shared) > 0:
        folder = _STATE.get('caches') or os.path.dirname(shared)
        private = os.path.join(folder, f'cookies-{re.sub(r"[^A-Za-z0-9_-]", "", str(tag))[:40] or "x"}.txt')
        try:
            shutil.copyfile(shared, private)
        except OSError:
            private = None
    try:
        yield private
    finally:
        if private:
            try:
                os.remove(private)
            except OSError:
                pass


class _QuietLogger:
    """For quick lookups (stream, list): keep only the last error."""

    def __init__(self):
        self.last_error = None

    def debug(self, msg):
        pass

    def info(self, msg):
        pass

    def warning(self, msg):
        _log(f'warning: {msg}')

    def error(self, msg):
        self.last_error = str(msg)
        _log(f'error: {msg}')


def _lookup_opts(logger, cookies, **extra):
    opts = {
        'quiet': True,
        'no_color': True,
        'noprogress': True,
        'logger': logger,
        'skip_download': True,
        'socket_timeout': 20,
        'extractor_retries': 2,
        'check_formats': False,
        'cachedir': os.path.join(_STATE['caches'], 'yt-dlp') if _STATE.get('caches') else False,
        'remote_components': ['ejs:github'],
    }
    if cookies:
        opts['cookiefile'] = cookies
    opts.update(extra)
    return opts


# --------------------------------------------------------------------------
# yt-dlp glue
# --------------------------------------------------------------------------

class _JobLogger:
    def __init__(self, job):
        self.job = job

    def _add(self, level, msg):
        if self.job.get('cancel'):
            from yt_dlp.utils import DownloadCancelled
            raise DownloadCancelled('cancelled by user')
        msg = str(msg)
        if msg.startswith('[debug] '):
            return
        line = f'{level}: {msg}' if level != 'info' else msg
        self.job['log'].append(line)
        if len(self.job['log']) > 200:
            del self.job['log'][:50]
        _log(line)
        if level == 'error':
            self.job['last_error'] = msg

    def debug(self, msg):
        self._add('info', msg)

    def info(self, msg):
        self._add('info', msg)

    def warning(self, msg):
        self._add('warning', msg)

    def error(self, msg):
        self._add('error', msg)


def _stream_kind(fmt):
    v = (fmt or {}).get('vcodec')
    a = (fmt or {}).get('acodec')
    if v == 'none' and a not in (None, 'none'):
        return 'audio'
    if a == 'none' and v not in (None, 'none'):
        return 'video'
    return 'file'


def _format_spec(mode, quality, av1=False):
    """Pick formats iOS can play and save to Photos without ffmpeg.

    - never VP9 / VP8 / WebM (iPhones can't play them from a file)
    - AV1 only on iPhones with an AV1 decoder (iPhone 15 Pro and newer): that is
      how YouTube serves 4K / 2K
    - highest resolution first (up to the chosen limit), H.264 preferred at equal size
    """
    if mode == 'audio':
        return 'ba[acodec^=mp4a]/ba[ext=m4a]/ba[acodec=aac]/ba/b', ['acodec:aac', 'proto', 'ext:m4a']
    height = None if quality in (None, '', 'best') else int(quality)
    playable = '[vcodec!^=?vp9][vcodec!^=?vp09][vcodec!^=?vp8]' + ('' if av1 else '[vcodec!^=?av01]')
    fmt = '/'.join([
        f'bv*{playable}[ext!=webm]+ba[acodec^=mp4a]',
        f'bv*{playable}[ext!=webm]+ba[ext=m4a]',
        f'b{playable}[ext!=webm]',
        'bv*+ba',
        'b',
    ])
    # 'res' ranks by the short side, so a vertical 1080x1920 TikTok counts as 1080p.
    # With a limit, the best at-or-below it wins; if none exists, the closest above.
    sort = [f'res:{height}' if height else 'res', 'vcodec:h264', 'acodec:aac', 'proto', 'ext:mp4:m4a']
    return fmt, sort


def _collect_items(info, workdir):
    """Walk the result of extract_info and list the files the app should keep."""
    items = []

    def visit(entry):
        if not entry:
            return
        if entry.get('_type') == 'playlist' or entry.get('entries') is not None:
            for e in entry.get('entries') or []:
                visit(e)
            return
        for rd in entry.get('requested_downloads') or []:
            fmts = rd.get('requested_formats')
            base = {
                'title': entry.get('title') or entry.get('id') or 'video',
                'id': entry.get('id'),
                'extractor': entry.get('extractor_key') or entry.get('extractor'),
                'uploader': entry.get('uploader') or entry.get('channel') or entry.get('uploader_id'),
                'duration': entry.get('duration'),
                'webpage_url': entry.get('webpage_url'),
                'media': 'media',
            }
            if fmts:
                video = next((f for f in fmts if _stream_kind(f) == 'video'), None)
                audio = next((f for f in fmts if _stream_kind(f) == 'audio'), None)
                paths = [f.get('filepath') for f in fmts]
                if video and audio and all(p and os.path.exists(p) for p in (video.get('filepath'), audio.get('filepath'))):
                    stem = os.path.splitext(rd.get('filepath') or rd.get('_filename') or video['filepath'])[0]
                    items.append(dict(base, kind='merge', video=video['filepath'], audio=audio['filepath'],
                                      output=stem + '.mp4', vcodec=video.get('vcodec'), acodec=audio.get('acodec')))
                    continue
                for p in paths:
                    if p and os.path.exists(p):
                        items.append(dict(base, kind='file', path=p))
                continue
            path = rd.get('filepath') or rd.get('_filename')
            if path and os.path.exists(path):
                container = None
                # Native HLS downloads of MPEG-TS segments are not real MP4 files.
                if _is_mpegts(path):
                    container = 'mpegts'
                    ts_path = os.path.splitext(path)[0] + '.ts'
                    os.replace(path, ts_path)
                    path = ts_path
                items.append(dict(base, kind='file', path=path, vcodec=rd.get('vcodec'), acodec=rd.get('acodec'),
                                  protocol=rd.get('protocol'), container=container))

    visit(info)
    return items


# --------------------------------------------------------------------------
# public API (called from Swift)
# --------------------------------------------------------------------------

def api_init(arg):
    _STATE['documents'] = arg.get('documents')
    _STATE['caches'] = arg.get('caches')
    _STATE['engine_dir'] = arg.get('engine_dir')
    _STATE['engine_site'] = os.path.join(arg['engine_dir'], 'site') if arg.get('engine_dir') else None
    for d in (_STATE['caches'], _STATE['engine_dir']):
        if d:
            os.makedirs(d, exist_ok=True)
    if _STATE['caches']:
        os.environ.setdefault('XDG_CACHE_HOME', _STATE['caches'])
    _activate_engine_site()
    try:
        import certifi
        os.environ['SSL_CERT_FILE'] = certifi.where()
    except Exception as e:
        _log(f'certifi missing: {e!r}')
    if arg.get('watchdog'):
        # CI diagnostics: dump every Python thread's stack periodically (works even if the GIL is stuck)
        import faulthandler
        _STATE['watchdog_file'] = open(arg['watchdog'], 'w')
        faulthandler.dump_traceback_later(int(arg.get('watchdog_seconds') or 45), repeat=True,
                                          file=_STATE['watchdog_file'])
    _STATE['ready'] = True
    return {'engine_api': ENGINE_VERSION}


def api_warmup(arg):
    """Import yt-dlp once in the background so the first download starts fast."""
    try:
        _yt_dlp()
    except Exception:
        # A broken update must never brick the app: fall back to the bundled engine.
        if _STATE.get('engine_site') in sys.path:
            _log('updated engine failed to import, falling back to bundled:\n' + traceback.format_exc())
            sys.path.remove(_STATE['engine_site'])
            _purge_modules()
            _yt_dlp()
        else:
            raise
    return _engine_info()


def api_info(arg):
    return _engine_info()


# --------------------------------------------------------------------------
# Turbo downloads: many connections per file (like download managers do)
# --------------------------------------------------------------------------

class _TurboFallback(Exception):
    """The server can't do parallel ranges; use yt-dlp's normal single connection."""


def _turbo_download(fd, filename, info_dict, connections):
    import queue
    from yt_dlp.networking import Request
    from yt_dlp.networking.exceptions import HTTPError
    from yt_dlp.utils import DownloadCancelled
    from yt_dlp.utils.networking import HTTPHeaderDict

    url = info_dict['url']
    base_headers = HTTPHeaderDict({'Accept-Encoding': 'identity'}, info_dict.get('http_headers'))
    tmp = fd.temp_name(filename)
    started = time.time()
    block = 256 * 1024

    def open_range(start, end):
        headers = HTTPHeaderDict(base_headers)
        headers['Range'] = f'bytes={start}-{end}'
        return fd.ydl.urlopen(Request(url, headers=headers))

    # The first piece tells us the size and whether the server honours ranges.
    probe_end = 1024 * 1024 - 1
    first = open_range(0, probe_end)
    content_range = first.headers.get('Content-Range') or ''
    if first.status != 206 or '/' not in content_range or content_range.endswith('/*'):
        first.close()
        raise _TurboFallback('no range support')
    total = int(content_range.rsplit('/', 1)[1])
    if total <= probe_end + 1:
        # the first answer already holds the whole (small) file
        with open(tmp, 'wb') as fh:
            while True:
                data = first.read(block)
                if not data:
                    break
                fh.write(data)
        first.close()
        if os.path.getsize(tmp) != total:
            os.remove(tmp)
            raise _TurboFallback('short read')
        fd.try_rename(tmp, filename)
        fd._hook_progress({'downloaded_bytes': total, 'total_bytes': total, 'filename': filename,
                           'status': 'finished', 'elapsed': time.time() - started,
                           'ctx_id': info_dict.get('ctx_id')}, info_dict)
        return True
    if total < 3 * 1024 * 1024:
        first.close()
        raise _TurboFallback('small file')

    piece = max(1024 * 1024, min(8 * 1024 * 1024, total // (connections * 3)))
    pieces = queue.Queue()
    pieces.put((0, min(probe_end, total - 1)))
    offset = probe_end + 1
    while offset < total:
        end = min(offset + piece - 1, total - 1)
        pieces.put((offset, end))
        offset = end + 1

    preopened = {0: first}
    lock = threading.Lock()
    stop = threading.Event()
    state = {'done': 0, 'active': 0, 'errors': [], 'fallback': None, 'backoff': False}

    out = os.open(tmp, os.O_RDWR | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        os.ftruncate(out, total)
    except OSError:
        pass

    def worker():
        with lock:
            state['active'] += 1
        try:
            while not stop.is_set():
                try:
                    start, end = pieces.get_nowait()
                except queue.Empty:
                    return
                pos, attempts = start, 0
                while pos <= end and not stop.is_set():
                    resp = None
                    try:
                        resp = preopened.pop(start, None) if pos == start else None
                        if resp is None:
                            resp = open_range(pos, end)
                        if resp.status != 206:
                            raise _TurboFallback('range ignored mid-download')
                        while pos <= end and not stop.is_set():
                            data = resp.read(min(block, end - pos + 1))
                            if not data:
                                break
                            os.pwrite(out, data, pos)
                            pos += len(data)
                            with lock:
                                state['done'] += len(data)
                        if pos <= end and not stop.is_set():
                            raise OSError('connection closed early')
                    except _TurboFallback as e:
                        state['fallback'] = str(e)
                        stop.set()
                        return
                    except HTTPError as e:
                        status = getattr(e, 'status', 0)
                        with lock:
                            others = state['active'] > 1
                        if status in (403, 429, 503) and others:
                            # the site limits connections: hand the rest to the others
                            pieces.put((pos, end))
                            state['backoff'] = True
                            return
                        attempts += 1
                        if attempts > 6:
                            state['errors'].append(e)
                            stop.set()
                            return
                        time.sleep(min(0.4 * 2 ** attempts, 6))
                    except Exception as e:
                        attempts += 1
                        if attempts > 6:
                            state['errors'].append(e)
                            stop.set()
                            return
                        time.sleep(min(0.4 * 2 ** attempts, 6))
                    finally:
                        if resp is not None:
                            try:
                                resp.close()
                            except Exception:
                                pass
        finally:
            with lock:
                state['active'] -= 1

    threads = [threading.Thread(target=worker, daemon=True, name=f'turbo-{i}') for i in range(connections)]
    for t in threads:
        t.start()

    last_report = 0.0
    try:
        while any(t.is_alive() for t in threads):
            time.sleep(0.2)
            now = time.time()
            if now - last_report < 0.4:
                continue
            last_report = now
            done = state['done']
            speed = fd.calc_speed(started, now, done)
            fd._hook_progress({
                'status': 'downloading',
                'downloaded_bytes': done,
                'total_bytes': total,
                'tmpfilename': tmp,
                'filename': filename,
                'eta': fd.calc_eta(started, now, total, done),
                'speed': speed,
                'elapsed': now - started,
                'ctx_id': info_dict.get('ctx_id'),
                'connections': state['active'],
            }, info_dict)
    except BaseException:
        stop.set()
        for t in threads:
            t.join(5)
        os.close(out)
        for leftover in preopened.values():
            leftover.close()
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    os.close(out)
    for leftover in preopened.values():
        leftover.close()

    if state['fallback'] is not None or (state['done'] < total and not state['errors'] and pieces.empty()):
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise _TurboFallback(state['fallback'] or 'incomplete')
    if state['errors']:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise state['errors'][-1]
    if state['done'] != total:
        raise DownloadCancelled('incomplete download') if stop.is_set() else OSError(
            f'turbo: got {state["done"]} of {total} bytes')

    fd.try_rename(tmp, filename)
    elapsed = time.time() - started
    fd._hook_progress({
        'downloaded_bytes': total,
        'total_bytes': total,
        'filename': filename,
        'status': 'finished',
        'elapsed': elapsed,
        'ctx_id': info_dict.get('ctx_id'),
    }, info_dict)
    if state['backoff']:
        fd.to_screen('[turbo] the site limited parallel connections; finished with fewer')
    fd.to_screen(f'[turbo] {total / 1048576:.1f} MiB in {elapsed:.1f}s '
                 f'({total / max(elapsed, 0.001) / 1048576:.1f} MiB/s, {connections} connections)')
    return True


def _install_turbo():
    """Route yt-dlp's plain HTTP downloads through the multi-connection downloader."""
    from yt_dlp import downloader as dl
    from yt_dlp.downloader.http import HttpFD
    if getattr(dl, '_nazzel_turbo', False):
        return

    class TurboHttpFD(HttpFD):
        def real_download(self, filename, info_dict):
            connections = int(self.params.get('nazzel_connections') or 1)
            simple = (connections > 1 and filename != '-' and not info_dict.get('request_data')
                      and not info_dict.get('is_live') and not self.params.get('test')
                      and self._get_impersonate_target(info_dict) is None)
            if simple:
                from yt_dlp.utils import DownloadCancelled
                try:
                    return _turbo_download(self, filename, info_dict, connections)
                except DownloadCancelled:
                    raise
                except _TurboFallback as e:
                    self.to_screen(f'[turbo] {e}: using a single connection')
                except Exception as e:
                    # never lose a download to turbo mode: retry the classic way
                    self.to_screen(f'[turbo] failed ({e}); retrying with a single connection')
            return super().real_download(filename, info_dict)

    for proto in ('http', 'https'):
        dl.PROTOCOL_MAP[proto] = TurboHttpFD
    dl._nazzel_turbo = True


IMAGE_EXTS = {'jpg', 'jpeg', 'png', 'webp', 'gif', 'heic', 'avif', 'bmp'}
MEDIA_EXTS = IMAGE_EXTS | {'mp4', 'mov', 'm4v', 'webm', 'mkv', 'm4a', 'mp3', 'aac', 'opus', 'ogg', 'wav', 'flac', 'ts',
                           'avi', 'flv', 'wmv', 'mpg', 'mpeg', 'm2ts', 'mts', '3gp', 'ogv', 'mka', 'wma'}

# yt-dlp answers like these mean "this post has no video": try the gallery engine instead.
_NO_VIDEO_HINTS = ('no video', 'unsupported url', 'no formats', 'no media', 'there is no video',
                   'only images', 'photo', 'image', 'requested format is not available', 'no video formats',
                   'unable to extract', 'not a video')


def _clear_dir(folder):
    for name in os.listdir(folder):
        path = os.path.join(folder, name)
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
        else:
            try:
                os.remove(path)
            except OSError:
                pass


def _new_job(job_id):
    job = {
        'status': 'extracting', 'downloaded': 0, 'total': None, 'speed': None, 'eta': None,
        'stage': None, 'title': None, 'thumbnail': None, 'item_index': 1, 'item_count': 1,
        'fragment': None, 'cancel': False, 'log': [], 'last_error': None, 'started': time.time(),
        'engine': 'yt-dlp', 'uploader': None,
    }
    with _JOBS_LOCK:
        _JOBS[job_id] = job
    return job


def _find_artwork(media_path):
    """yt-dlp writes '<stem>.webp/.jpg' next to the media when writethumbnail is on."""
    folder = os.path.dirname(media_path)
    stem = os.path.splitext(os.path.basename(media_path))[0]
    stem = re.sub(r'\.f[0-9A-Za-z_-]+$', '', stem)  # 'title [id].f137' -> 'title [id]'
    for ext in ('jpg', 'jpeg', 'webp', 'png'):
        candidate = os.path.join(folder, f'{stem}.{ext}')
        if os.path.exists(candidate) and os.path.abspath(candidate) != os.path.abspath(media_path):
            return candidate
    return None


def _run_ytdlp(job, url, mode, quality, workdir, cookies, extra_opts=None, max_items=30, connections=10,
               av1=False, subtitles=None):
    yt_dlp = _yt_dlp()
    _install_turbo()
    from yt_dlp.postprocessor.common import PostProcessor
    from yt_dlp.utils import DownloadCancelled

    class _InfoPP(PostProcessor):
        def run(self, info):
            job['title'] = info.get('title') or job['title']
            job['thumbnail'] = info.get('thumbnail') or job['thumbnail']
            job['uploader'] = info.get('uploader') or info.get('channel') or job['uploader']
            if info.get('n_entries'):
                job['item_count'] = info.get('n_entries')
                job['item_index'] = info.get('playlist_index') or job['item_index']
            job['status'] = 'downloading'
            return [], info

    def hook(d):
        if job['cancel']:
            raise DownloadCancelled('cancelled by user')
        st = d.get('status')
        info = d.get('info_dict') or {}
        job['stage'] = _stream_kind(info)
        if st == 'downloading':
            job['status'] = 'downloading'
            job['downloaded'] = d.get('downloaded_bytes') or 0
            job['total'] = d.get('total_bytes') or d.get('total_bytes_estimate')
            job['speed'] = d.get('speed')
            job['eta'] = d.get('eta')
            fi, fc = d.get('fragment_index'), d.get('fragment_count')
            job['fragment'] = [fi, fc] if fi and fc else None
            job['connections'] = d.get('connections')
        elif st == 'finished':
            job['downloaded'] = d.get('total_bytes') or d.get('downloaded_bytes') or job['downloaded']
            job['total'] = job['downloaded']
            job['status'] = 'finishing'

    fmt, sort = _format_spec(mode, quality, av1)
    opts = {
        'outtmpl': {'default': '%(title).70B [%(id)s].%(ext)s', 'thumbnail': '%(title).70B [%(id)s].%(ext)s'},
        'paths': {'home': workdir, 'temp': workdir},
        'format': fmt,
        'format_sort': sort,
        'noplaylist': True,
        'playlistend': int(max_items or 30),
        'ignoreerrors': 'only_download',   # lets yt-dlp keep separate video/audio files without ffmpeg
        'logger': _JobLogger(job),
        'progress_hooks': [hook],
        'quiet': True,
        'noprogress': True,
        'no_color': True,
        'socket_timeout': 30,
        'retries': 5,
        'fragment_retries': 10,
        'extractor_retries': 2,
        'concurrent_fragment_downloads': max(4, int(connections)),
        'nazzel_connections': int(connections),
        'http_chunk_size': 10 * 1024 * 1024,
        'overwrites': True,
        'continuedl': True,
        'writethumbnail': True,            # cover art for the player / lock screen
        'check_formats': False,
        'fixup': 'never',
        'trim_file_name': 120,
        'cachedir': os.path.join(_STATE['caches'], 'yt-dlp') if _STATE.get('caches') else False,
        'remote_components': ['ejs:github'],
    }
    if cookies:
        opts['cookiefile'] = cookies
    if subtitles and mode == 'video':
        # e.g. ['ar', 'en']: real subtitles first, YouTube's automatic (and auto-translated) ones otherwise
        opts.update({'writesubtitles': True, 'writeautomaticsub': True,
                     'subtitleslangs': list(subtitles), 'subtitlesformat': 'vtt/srt/best'})
    if extra_opts:
        opts.update(extra_opts)

    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.add_post_processor(_InfoPP(ydl), when='pre_process')
        info = ydl.extract_info(url, download=True)
        info = ydl.sanitize_info(info) if info else info
    items = _collect_items(info, workdir)
    for item in items:
        media_path = item.get('output') or item.get('path') or item.get('video') or ''
        if item.get('kind') == 'file' and media_path.rsplit('.', 1)[-1].lower() in IMAGE_EXTS:
            item['media'] = 'image'
            continue
        art = _find_artwork(media_path)
        if art:
            item['artwork'] = art
        subs = _find_subtitles(media_path)
        if subs:
            item['subtitles'] = subs
    return items, (info or {}).get('title')


def _find_subtitles(media_path):
    """yt-dlp writes '<stem>.<lang>.vtt' next to the media."""
    folder = os.path.dirname(media_path)
    stem = os.path.splitext(os.path.basename(media_path))[0]
    stem = re.sub(r'\.f[0-9A-Za-z_-]+$', '', stem)
    found = []
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return found
    for name in names:
        if name.startswith(stem + '.') and name.lower().endswith(('.vtt', '.srt')):
            lang = name[len(stem) + 1:].rsplit('.', 1)[0]
            if lang and os.path.getsize(os.path.join(folder, name)) > 0:
                found.append({'lang': lang, 'path': os.path.join(folder, name)})
    return found[:4]


def _gallery_supports(url):
    try:
        from gallery_dl import extractor
        return extractor.find(url) is not None
    except Exception as e:
        _log(f'gallery-dl unavailable: {e!r}')
        return False


_GALLERY_LOCK = threading.Lock()


def _run_gallery(job, url, workdir, cookies, max_items=60):
    """Images, carousels, slideshows and sites yt-dlp does not know (gallery-dl)."""
    with _GALLERY_LOCK:  # gallery-dl keeps its settings in one global config
        return _run_gallery_locked(job, url, workdir, cookies, max_items)


def _run_gallery_locked(job, url, workdir, cookies, max_items=60):
    import logging
    from gallery_dl import config, job as gjob

    job['engine'] = 'gallery-dl'
    job['status'] = 'downloading'
    job['stage'] = 'file'
    config.clear()
    config.set(('extractor',), 'base-directory', workdir)
    config.set(('extractor',), 'directory', [])
    config.set(('extractor',), 'timeout', 30)
    config.set(('extractor',), 'retries', 3)
    config.set(('extractor',), 'skip', False)
    config.set(('extractor',), 'image-range', f'1-{int(max_items)}')
    config.set(('extractor',), 'user-agent',
               'Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 '
               '(KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1')
    config.set(('extractor', 'instagram'), 'videos', True)
    config.set(('extractor', 'twitter'), 'videos', True)
    config.set(('extractor', 'tiktok'), 'audio', True)
    config.set(('output',), 'mode', 'null')
    config.set(('output',), 'progress', False)
    if cookies:
        config.set(('extractor',), 'cookies', cookies)

    class _Handler(logging.Handler):
        def emit(self, record):
            try:
                msg = record.getMessage()
            except Exception:
                return
            level = 'error' if record.levelno >= logging.ERROR else 'warning' if record.levelno >= logging.WARNING else 'info'
            line = f'[gallery-dl] {msg}' if level == 'info' else f'{level}: [gallery-dl] {msg}'
            job['log'].append(line)
            _log(line)
            if level == 'error':
                job['last_error'] = msg

    handler = _Handler(logging.INFO)
    root = logging.getLogger()
    root.addHandler(handler)
    if root.level > logging.INFO or root.level == logging.NOTSET:
        root.setLevel(logging.INFO)

    class _Job(gjob.DownloadJob):
        def handle_url(self, url, kwdict):
            if job['cancel']:
                raise Cancelled()
            job['item_count'] = max(job['item_count'], int(kwdict.get('count') or 0) or job['item_count'])
            job['item_index'] = int(kwdict.get('num') or job['item_index'])
            if not job['title']:
                for key in ('description', 'content', 'title', 'caption', 'text', 'desc'):
                    value = kwdict.get(key)
                    if isinstance(value, str) and value.strip():
                        job['title'] = value.strip().splitlines()[0][:120]
                        break
            if not job['uploader']:
                for key in ('username', 'author', 'user', 'owner'):
                    value = kwdict.get(key)
                    if isinstance(value, dict):
                        value = value.get('name') or value.get('username') or value.get('nick')
                    if isinstance(value, str) and value:
                        job['uploader'] = value
                        break
            super().handle_url(url, kwdict)

    _log(f'[gallery-dl] {url}')
    try:
        _Job(url).run()
    finally:
        root.removeHandler(handler)

    if job['cancel']:
        raise Cancelled()
    items = []
    for name in sorted(os.listdir(workdir)):
        path = os.path.join(workdir, name)
        ext = name.rsplit('.', 1)[-1].lower() if '.' in name else ''
        if os.path.isfile(path) and ext in MEDIA_EXTS and not name.endswith('.part'):
            items.append({'kind': 'file', 'path': path, 'title': job['title'] or os.path.splitext(name)[0],
                          'uploader': job['uploader'], 'webpage_url': None,
                          'media': 'image' if ext in IMAGE_EXTS else 'media'})
    return items, job['title']


# --------------------------------------------------------------------------
# Any file: archives, documents, apps, disk images ... (not only video)
# --------------------------------------------------------------------------

# Links ending like this are files, not web pages or videos: they go straight to the file downloader.
FILE_EXTS = {
    'zip', 'rar', '7z', 'tar', 'gz', 'tgz', 'bz2', 'xz', 'zst', 'cab',
    'pdf', 'doc', 'docx', 'xls', 'xlsx', 'ppt', 'pptx', 'odt', 'ods', 'odp', 'rtf', 'txt', 'csv', 'tsv',
    'json', 'epub', 'mobi', 'azw3', 'djvu', 'md', 'sql', 'log', 'ics', 'vcf',
    'apk', 'xapk', 'ipa', 'dmg', 'pkg', 'exe', 'msi', 'deb', 'rpm', 'appimage', 'jar', 'iso', 'img', 'bin',
    'torrent', 'ttf', 'otf', 'woff', 'woff2', 'psd', 'ai', 'svg', 'dwg', 'stl', 'obj', 'fbx',
    'mobileconfig', 'plist', 'dylib', 'deb', 'p12', 'cer', 'crt', 'pem',
    # iPhone / Mac apps and firmware, disk images
    'tipa', 'ipsw', 'xip', 'esd', 'wim', 'vhd', 'vhdx', 'vmdk', 'vdi', 'qcow2', 'ova',
    'gif',
}

_PAGE_TYPES = ('text/html', 'application/xhtml+xml')
_DIRECT_UA = ('Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 '
              '(KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1')


def _looks_like_file(url):
    try:
        path = urllib.parse.urlsplit(url).path
    except ValueError:
        return False
    name = urllib.parse.unquote(path.rsplit('/', 1)[-1])
    return '.' in name and name.rsplit('.', 1)[-1].lower() in FILE_EXTS


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _load_jar(cookies):
    if not cookies:
        return None
    try:
        import http.cookiejar
        jar = http.cookiejar.MozillaCookieJar(cookies)
        jar.load(ignore_discard=True, ignore_expires=True)
        return jar
    except Exception as e:
        _log(f'cookies not loaded for direct download: {e!r}')
        return None


def _direct_open(url, referer, jar, start=None, end=None, timeout=30, method='GET'):
    """GET with our own redirect loop, so the sign-in cookies only go to the host they belong to."""
    opener = urllib.request.build_opener(_NoRedirect, urllib.request.HTTPSHandler(context=_ssl_context()))
    for _ in range(10):
        headers = {'User-Agent': _DIRECT_UA, 'Accept': '*/*', 'Accept-Encoding': 'identity'}
        if referer:
            headers['Referer'] = referer
            m = re.match(r'(https?://[^/]+)', referer, re.I)
            if m:
                headers['Origin'] = m.group(1)
        if start is not None:
            headers['Range'] = f'bytes={start}-' + ('' if end is None else str(end))
        req = urllib.request.Request(url, headers=headers, method=method)
        if jar is not None:
            try:
                jar.add_cookie_header(req)
            except Exception:
                pass
        try:
            return opener.open(req, timeout=timeout), url
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308) and e.headers.get('Location'):
                url = urllib.parse.urljoin(url, e.headers['Location'])
                continue
            raise
    raise OSError('too many redirects')


def _direct_filename(url, headers, ctype):
    name = ''
    disp = headers.get('Content-Disposition')
    if disp:
        try:
            import email.message
            msg = email.message.Message()
            msg['Content-Disposition'] = disp
            name = msg.get_filename() or ''
        except Exception:
            name = ''
        if not name:
            m = re.search(r"filename\*\s*=\s*[\w-]+'[^']*'([^;]+)", disp, re.I)
            if m:
                name = urllib.parse.unquote(m.group(1).strip().strip('"'))
    if not name:
        path = urllib.parse.urlsplit(url).path
        name = urllib.parse.unquote(path.rsplit('/', 1)[-1])
    name = re.sub(r'[\\/\x00-\x1f:*?"<>|]', '_', name).strip().strip('.')
    if not name:
        name = urllib.parse.urlsplit(url).hostname or 'file'
    if '.' not in name:
        import mimetypes
        ext = mimetypes.guess_extension(ctype or '') if ctype and ctype != 'application/octet-stream' else None
        if ext:
            name += ext
    if len(name) > 120:
        stem, dot, ext = name.rpartition('.')
        name = (stem[:110] + dot + ext[:8]) if dot and len(ext) <= 8 else name[:120]
    return name


def _sniff_ext(path):
    """What a file without a usable name really is, from its first bytes (an ipa is a zip with a
    Payload/ folder, an iso has "CD001" at 32 KB ...). '' when unsure."""
    try:
        size = os.path.getsize(path)
        with open(path, 'rb') as fh:
            head = fh.read(64 * 1024)
            iso = b''
            if size > 0x8006:
                fh.seek(0x8001)
                iso = fh.read(5)
            tail = b''
            if size > 512:
                fh.seek(size - 512)
                tail = fh.read(4)
    except OSError:
        return ''
    if iso == b'CD001':
        return 'iso'
    if head.startswith(b'PK\x03\x04'):
        try:
            with zipfile.ZipFile(path) as z:
                names = z.namelist()[:400]
        except Exception:
            names = []
        if any(n.startswith('Payload/') and '.app/' in n for n in names):
            return 'ipa'
        if 'AndroidManifest.xml' in names and any(n.endswith('.dex') for n in names):
            return 'apk'
        if any(n == 'word/document.xml' for n in names):
            return 'docx'
        if any(n == 'xl/workbook.xml' for n in names):
            return 'xlsx'
        if any(n == 'ppt/presentation.xml' for n in names):
            return 'pptx'
        if 'mimetype' in names and 'META-INF/container.xml' in names:
            return 'epub'
        return 'zip'
    if tail == b'koly':
        return 'dmg'
    signatures = (
        (b'%PDF', 'pdf'), (b'Rar!\x1a\x07', 'rar'), (b"7z\xbc\xaf\x27\x1c", '7z'), (b'\x1f\x8b', 'gz'),
        (b'BZh', 'bz2'), (b'\xfd7zXZ\x00', 'xz'), (b'\x1a\x45\xdf\xa3', 'mkv'), (b'xar!', 'pkg'),
        (b'\x89PNG', 'png'), (b'\xff\xd8\xff', 'jpg'), (b'GIF8', 'gif'), (b'MZ', 'exe'),
        (b'!<arch>\ndebian', 'deb'), (b'ID3', 'mp3'), (b'fLaC', 'flac'), (b'OggS', 'ogg'),
    )
    for magic, ext in signatures:
        if head.startswith(magic):
            return ext
    if head[4:8] == b'ftyp':
        return 'm4a' if head[8:11] in (b'M4A', b'M4B') else 'mp4'
    if head[:4] == b'RIFF' and head[8:12] == b'AVI ':
        return 'avi'
    if head[:4] == b'RIFF' and head[8:12] == b'WAVE':
        return 'wav'
    return ''


def _ipa_from_manifest(url, referer, cookies):
    """An app install link (itms-services://?action=download-manifest&url=…plist, used by most IPA
    sites) or the manifest itself: the address of the .ipa inside. None when it is not one."""
    manifest = None
    if url.lower().startswith('itms-services:'):
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        manifest = (query.get('url') or [None])[0]
        if not manifest:
            raise OSError('install link without a manifest address')
    elif _path_ext(url) == 'plist' or 'download-manifest' in url.lower():
        manifest = url
    if not manifest or not re.match(r'https?://', manifest, re.I):
        return None
    import plistlib
    try:
        resp, _ = _direct_open(manifest, referer, _load_jar(cookies), timeout=20)
        try:
            data = resp.read(2 * 1024 * 1024)
        finally:
            resp.close()
        plist = plistlib.loads(data)
    except Exception as e:
        if url.lower().startswith('itms-services:'):
            raise OSError(f'could not read the app manifest: {e}')
        return None     # a normal .plist file: download it as it is
    for item in (plist.get('items') or []) if isinstance(plist, dict) else []:
        for asset in item.get('assets') or []:
            if asset.get('kind') == 'software-package' and asset.get('url'):
                _log(f'install link -> {asset["url"]}')
                return asset['url']
    if url.lower().startswith('itms-services:'):
        raise OSError('the app manifest has no .ipa address')
    return None


def _only_generic(url):
    """True when no site-specific yt-dlp extractor knows this link (only the catch-all one does)."""
    try:
        import yt_dlp
        for ie in yt_dlp.extractor.gen_extractor_classes():
            if ie.ie_key() != 'Generic' and ie.suitable(url):
                return False
        return True
    except Exception:
        return False


_STREAM_TYPES = ('application/vnd.apple.mpegurl', 'application/x-mpegurl', 'application/dash+xml',
                 'audio/mpegurl', 'audio/x-mpegurl')
# Manifests, feeds and subtitles: the video engine's business, never "a file to save".
MANIFEST_EXTS = {'m3u8', 'm3u', 'mpd', 'ism', 'f4m', 'rss', 'xml', 'atom', 'json', 'vtt', 'srt', 'm4s'}
_GENERIC_TYPES = ('application/octet-stream', 'binary/octet-stream', 'application/x-unknown',
                  'application/unknown', 'application/force-download', 'application/download')


def _path_ext(url):
    try:
        name = urllib.parse.unquote(urllib.parse.urlsplit(url).path.rsplit('/', 1)[-1])
    except ValueError:
        return ''
    return name.rsplit('.', 1)[-1].lower() if '.' in name else ''


def _probe_is_file(url, referer, cookies):
    """Looks at the headers (HEAD, no body) of a link nobody knows, and says True only when it is
    clearly a file to save as it is. False/None: leave it to the video engine (it falls back to the
    file downloader by itself if it cannot use the link)."""
    if not re.match(r'https?://', url, re.I):
        return None
    try:
        resp, final = _direct_open(url, referer, _load_jar(cookies), timeout=8, method='HEAD')
    except Exception:
        return None
    try:
        ctype = resp.headers.get_content_type()
        if ctype in _PAGE_TYPES or ctype.startswith(('video/', 'audio/')) or ctype in _STREAM_TYPES:
            return False
        if ctype in ('application/xml', 'text/xml', 'application/rss+xml', 'application/atom+xml', 'application/json'):
            return False
        name = _direct_filename(final, resp.headers, ctype)
        ext = name.rsplit('.', 1)[-1].lower() if '.' in name else ''
        if ext in MANIFEST_EXTS or (ext in MEDIA_EXTS and ext not in IMAGE_EXTS):
            return False
        if ctype in _GENERIC_TYPES:
            # a plain "binary" answer says nothing: trust a file-like name or an explicit attachment
            disp = (resp.headers.get('Content-Disposition') or '').lower()
            return ext in FILE_EXTS or ext in IMAGE_EXTS or disp.startswith('attachment')
        return True     # a specific type that is not media or a page: pdf, zip, office, image ...
    except Exception:
        return None
    finally:
        resp.close()


def _run_direct(job, url, workdir, cookies, referer, connections=10, force=False, resume=False):
    """Download any file as it is. Several connections when the server allows it.
    resume: carry on from what an earlier (paused / interrupted) run left in workdir."""
    job['engine'] = 'direct'
    job['status'] = 'extracting'
    jar = _load_jar(cookies)
    referer = (referer or '').strip() or None
    first, final_url = _direct_open(url, referer, jar)
    try:
        ctype = first.headers.get_content_type()
        if ctype in _PAGE_TYPES and not force and not _looks_like_file(final_url):
            raise OSError('not a file: the link opens a web page')
        name = _direct_filename(final_url, first.headers, ctype)
        try:
            total = int(first.headers.get('Content-Length') or 0) or None
        except ValueError:
            total = None
        ranges = 'bytes' in (first.headers.get('Accept-Ranges') or '').lower()
        if first.headers.get('Content-Encoding', 'identity').lower() not in ('', 'identity'):
            ranges = False      # a compressed body: sizes and ranges do not match the file
    except BaseException:
        first.close()
        raise

    path = os.path.join(workdir, name)
    job['title'] = name
    job['total'] = total
    job['downloaded'] = 0
    job['status'] = 'downloading'
    started = time.time()
    state = {'done': 0}
    lock = threading.Lock()

    def progress(n):
        with lock:
            state['done'] += n
            job['downloaded'] = state['done']
            job['speed'] = state['done'] / max(time.time() - started, 0.001)
            job['eta'] = int((total - state['done']) / job['speed']) if total and job['speed'] else None

    def check_cancel():
        if job['cancel']:
            raise Cancelled()

    parts = min(max(int(connections or 1), 1), 8)
    state_path = path + '.nzstate'
    saved = None
    if resume and os.path.exists(state_path) and os.path.exists(path):
        try:
            with open(state_path) as fh:
                saved = json.load(fh)
            if saved.get('total') != total or not ranges:
                saved = None
        except Exception:
            saved = None
    if saved and saved.get('single'):
        parts = 1
    elif saved and saved.get('parts'):
        parts = len(saved['parts'])
    if not (total and ranges and total >= 4 * 1024 * 1024 and parts > 1):
        # one connection; if it drops, carry on from where it stopped
        job['connections'] = 1
        resp, offset, tries = first, 0, 0
        mode = 'wb'
        if resume and ranges and total and os.path.exists(path) and 0 < os.path.getsize(path) < total:
            offset = os.path.getsize(path)
            first.close()
            resp, _ = _direct_open(final_url, referer, jar, start=offset)
            if resp.status == 206:
                mode = 'ab'
                _log(f'direct: resuming at {offset} of {total}')
                progress(offset)
            else:
                resp.close()
                resp, _ = _direct_open(final_url, referer, jar)
                offset = 0
        if ranges and total:
            try:
                with open(state_path, 'w') as fh:
                    json.dump({'total': total, 'single': True}, fh)
            except OSError:
                pass
        with open(path, mode) as fh:
            while True:
                try:
                    while True:
                        check_cancel()
                        chunk = resp.read(256 * 1024)
                        if not chunk:
                            # read(n) ends quietly when the server hangs up early: that is not "done"
                            if total and offset < total:
                                raise OSError('connection closed early')
                            break
                        fh.write(chunk)
                        offset += len(chunk)
                        progress(len(chunk))
                    break
                except (OSError, http.client.HTTPException) as e:
                    resp.close()
                    tries += 1
                    if tries > 5 or not ranges:
                        raise
                    _log(f'direct: connection dropped at {offset} ({e!r}), resuming')
                    time.sleep(min(tries, 3))
                    resp, _ = _direct_open(final_url, referer, jar, start=offset)
                    if resp.status != 206:
                        resp.close()
                        raise
        resp.close()
    else:
        first.close()
        job['connections'] = parts
        size = -(-total // parts)
        # [next byte to fetch, last byte] per part; saved so a later run can carry on
        if saved and saved.get('parts'):
            spans = [list(span) for span in saved['parts']]
            already = sum(span[0] - (i * size) for i, span in enumerate(spans))
            _log(f'direct: resuming {parts} parts, {already} of {total} bytes already here')
            progress(max(0, already))
        else:
            with open(path, 'wb') as fh:
                fh.truncate(total)
            spans = [[i * size, min(total - 1, i * size + size - 1)] for i in range(parts)]
        errors = []
        stop_saving = threading.Event()

        def save_state():
            while not stop_saving.wait(2):
                try:
                    with open(state_path + '.tmp', 'w') as fh:
                        json.dump({'total': total, 'parts': spans}, fh)
                    os.replace(state_path + '.tmp', state_path)
                except OSError:
                    pass

        saver = threading.Thread(target=save_state, daemon=True)
        saver.start()

        def worker(index):
            lo, hi = spans[index]
            tries = 0
            fd = os.open(path, os.O_WRONLY)
            try:
                while lo <= hi and not errors:
                    try:
                        resp, _ = _direct_open(final_url, referer, jar, start=lo, end=hi)
                        try:
                            if resp.status != 206:
                                raise OSError(f'server ignored the range request (HTTP {resp.status})')
                            while lo <= hi:
                                check_cancel()
                                chunk = resp.read(min(256 * 1024, hi - lo + 1))
                                if not chunk:
                                    break
                                os.pwrite(fd, chunk, lo)
                                lo += len(chunk)
                                spans[index][0] = lo
                                progress(len(chunk))
                        finally:
                            resp.close()
                        if lo <= hi:
                            raise OSError('connection closed early')
                    except Cancelled:
                        raise
                    except (OSError, http.client.HTTPException) as e:
                        tries += 1
                        if tries > 5:
                            raise
                        _log(f'direct: part {index} retry {tries}: {e!r}')
                        time.sleep(min(tries, 3))
            except BaseException as e:
                errors.append(e)
            finally:
                os.close(fd)

        threads = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(parts)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        stop_saving.set()
        try:
            with open(state_path, 'w') as fh:
                json.dump({'total': total, 'parts': spans}, fh)
        except OSError:
            pass
        if errors:
            for e in errors:
                if isinstance(e, Cancelled):
                    raise e
            raise errors[0]

    if total and os.path.getsize(path) != total:
        raise OSError(f'incomplete download ({os.path.getsize(path)} of {total} bytes)')
    try:
        os.remove(state_path)
    except OSError:
        pass
    ext = name.rsplit('.', 1)[-1].lower() if '.' in name else ''
    if ext not in FILE_EXTS and ext not in MEDIA_EXTS:
        # "download.php", "file", "get?id=5" ...: name it after what it really is (ipa, iso, zip ...)
        real = _sniff_ext(path)
        if real:
            # a script name says nothing ("download.php"): drop it; anything else stays in the name
            stem = name.rsplit('.', 1)[0] if ext in ('php', 'aspx', 'asp', 'jsp', 'cgi', 'html', 'htm') else name
            new_name = f'{stem}.{real}'
            new_path = os.path.join(workdir, new_name)
            try:
                os.replace(path, new_path)
                _log(f'direct: {name} is really a .{real} file')
                path, name, ext = new_path, new_name, real
            except OSError:
                pass
    host = urllib.parse.urlsplit(final_url).hostname
    items = [{'kind': 'file', 'path': path, 'title': name, 'uploader': host, 'webpage_url': url,
              'media': 'image' if ext in IMAGE_EXTS else ('media' if ext in MEDIA_EXTS else 'file')}]
    return items, name


def api_download(arg):
    # Every job reads (and yt-dlp writes back) its own copy of the cookie file.
    with _cookie_copy(arg.get('cookies'), arg.get('job')) as private:
        result = _download(dict(arg, cookies=private))
        # A link found on some website (a media file or stream) may be refused or empty: the page
        # itself is the second try. The first failure is kept in the log so nothing is hidden.
        fallback = (arg.get('fallback_url') or '').strip()
        if result.get('failed') and fallback and fallback != (arg.get('url') or '').strip():
            _log(f'first try failed ({result.get("error")}), trying {fallback}')
            retry = _download(dict(arg, cookies=private, url=fallback, fallback_url=None))
            if not retry.get('failed'):
                return retry
            retry['detail'] = (result.get('detail') or '') + '\n--- second try ---\n' + (retry.get('detail') or '')
            return retry
        return result


_AGE_GATE = re.compile(r'confirm your age|age[- ]restrict|inappropriate for some users|age verification|'
                       r'sign in to confirm|over_18|nsfw', re.I)


def _is_age_gate(message):
    return bool(_AGE_GATE.search(message or ''))


def _age_opts(extra_opts):
    opts = dict(extra_opts or {})
    args = dict(opts.get('extractor_args') or {})
    args['youtube'] = dict(args.get('youtube') or {}, player_client=['web_embedded', 'tv', 'mweb', 'web_creator',
                                                                     'android_vr', 'default'])
    opts['extractor_args'] = args
    return opts


def _gif_from_page(url, referer, cookies):
    """Giphy / Tenor pages (and their short links): the .gif itself. None for anything else."""
    lower = url.lower()
    m = re.search(r'giphy\.com/(?:gifs|stickers|embed)/(?:[^/?#]*-)?([A-Za-z0-9]{6,})', url, re.I)
    if m:
        return f'https://i.giphy.com/{m.group(1)}.gif'
    if not re.search(r'(tenor\.com/(?:[a-z]{2}/)?view/|tenor\.com/[a-z0-9]+\.gif|gfycat\.com/|imgur\.com/)', lower):
        return None
    try:
        resp, _ = _direct_open(url, referer, _load_jar(cookies), timeout=15)
        try:
            html = resp.read(1024 * 1024).decode('utf-8', 'replace')
        finally:
            resp.close()
    except Exception:
        return None
    for pattern in (r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+\.gif[^"\']*)',
                    r'<meta[^>]+content=["\']([^"\']+\.gif[^"\']*)["\'][^>]+property=["\']og:image',
                    r'"contentUrl"\s*:\s*"([^"]+\.gif)"',
                    r'(https://media\d*\.tenor\.com/[^"\'\s]+\.gif)'):
        found = re.search(pattern, html, re.I)
        if found:
            return found.group(1).replace('\\u002F', '/').replace('&amp;', '&')
    return None


def _site_headers(referer):
    """Headers for media found on an ordinary website: many servers only serve the file to
    requests that come from their own page (hot-link protection)."""
    referer = (referer or '').strip()
    if not referer.lower().startswith(('http://', 'https://')):
        return None
    headers = {'Referer': referer}
    m = re.match(r'(https?://[^/]+)', referer, re.I)
    if m:
        headers['Origin'] = m.group(1)
    return headers


def _download(arg):
    from yt_dlp.utils import DownloadCancelled

    job_id = arg['job']
    url = arg['url'].strip()
    workdir = arg['workdir']
    os.makedirs(workdir, exist_ok=True)
    mode = arg.get('mode', 'video')
    job = _new_job(job_id)
    cookies = arg.get('cookies')
    if not (cookies and os.path.exists(cookies) and os.path.getsize(cookies) > 0):
        cookies = None

    _log(f'--- download {url} mode={mode} quality={arg.get("quality")}')
    items, title, errors = [], None, []

    def cancelled():
        job['status'] = 'cancelled'
        return {'cancelled': True, 'error': 'تم الإلغاء'}

    as_file = bool(arg.get('as_file'))
    try:
        ipa = _ipa_from_manifest(url, arg.get('referer'), cookies)
    except Exception as e:
        job['status'] = 'error'
        return {'failed': True, 'error': _friendly_error(_clean_error(str(e))), 'detail': str(e)}
    if ipa:
        url, as_file = ipa, True
    gif = None if as_file else _gif_from_page(url, arg.get('referer'), cookies)
    if gif:
        _log(f'gif page -> {gif}')
        url, as_file = gif, True

    lower = url.lower()
    # Instagram stories and highlights: gallery-dl handles them (images and videos, with your sign-in)
    stories = 'instagram.com/stories/' in lower or re.search(r'instagram\.com/[^/?#]+/highlights', lower)
    order = ['gallery', 'yt-dlp'] if (mode == 'photos' or stories) else ['yt-dlp', 'gallery']
    # Any other file (zip, pdf, apk ...): a link that ends like a file, or one the app already knows
    # is a download (the browser saw a download response), goes straight to the file downloader.
    # For every other link it is the last resort, after the video engines gave up.
    if as_file or _looks_like_file(url):
        order = ['direct']
    else:
        # a link no site extractor knows (no extension, e.g. /download?id=5): look at what it serves
        if mode != 'photos' and not stories and _path_ext(url) not in (MEDIA_EXTS | MANIFEST_EXTS) \
                and _only_generic(url) and _probe_is_file(url, arg.get('referer'), cookies):
            order = ['direct']
        elif _path_ext(url) not in MANIFEST_EXTS:
            # never save a stream manifest (m3u8, mpd ...) as if it were a file
            order.append('direct')
    subtitles = arg.get('subtitles') if mode == 'video' else None
    extra_opts = dict(arg.get('extra_opts') or {})
    site_headers = _site_headers(arg.get('referer'))
    if site_headers:
        extra_opts['http_headers'] = site_headers
    resume = bool(arg.get('resume'))
    for position, engine in enumerate(order):
        if items:
            break
        if engine == 'gallery' and not _gallery_supports(url):
            continue
        if engine == 'gallery' and errors and not any(h in errors[-1].lower() for h in _NO_VIDEO_HINTS) \
                and mode != 'photos':
            # yt-dlp failed for a reason gallery-dl will not fix (login, network, removed post)
            continue
        if not (resume and position == 0):
            _clear_dir(workdir)  # leftovers from a failed attempt must not leak into the result
        try:
            if engine == 'yt-dlp':
                def run(subs, opts):
                    return _run_ytdlp(job, url, 'video' if mode == 'photos' else mode, arg.get('quality'),
                                      workdir, cookies, opts or None, arg.get('max_items'),
                                      arg.get('connections') or 10, bool(arg.get('av1')), subs)

                def attempt(subs):
                    # age-restricted (YouTube "confirm your age"): other YouTube players often still
                    # serve it; with a signed-in, age-verified account the normal way already works
                    try:
                        found = run(subs, extra_opts)
                        if found[0] or not _is_age_gate(job.get('last_error') or ''):
                            return found
                    except (DownloadCancelled, Cancelled):
                        raise
                    except Exception as e:
                        if not _is_age_gate(getattr(e, 'msg', None) or str(e)):
                            raise
                    _log('age-restricted: trying other players')
                    _clear_dir(workdir)
                    job['last_error'] = None
                    return run(subs, _age_opts(extra_opts))
                try:
                    items, title = attempt(subtitles)
                except (DownloadCancelled, Cancelled):
                    raise
                except Exception as e:
                    if not (subtitles and 'subtitle' in str(e).lower()):
                        raise
                    items = []
                    job['last_error'] = str(e)
                if not items and subtitles and 'subtitle' in (job.get('last_error') or '').lower():
                    # the subtitles failed (YouTube often rate-limits them): the video matters more
                    _log('subtitles failed, downloading without them')
                    job['last_error'] = None
                    _clear_dir(workdir)
                    items, title = attempt(None)
            elif engine == 'direct':
                items, title = _run_direct(job, url, workdir, cookies, arg.get('referer'),
                                           arg.get('connections') or 10, force=as_file,
                                           resume=resume and position == 0)
            else:
                items, title = _run_gallery(job, url, workdir, cookies, arg.get('max_items') or 60)
            if not items:
                errors.append(_clean_error(job.get('last_error') or 'no file was downloaded'))
        except (DownloadCancelled, Cancelled):
            return cancelled()
        except Exception as e:
            msg = _clean_error(getattr(e, 'msg', None) or str(e))
            job['last_error'] = msg
            errors.append(msg)
            _log(f'ERROR [{engine}] {msg}')
        if job['cancel']:
            return cancelled()

    if not items:
        job['status'] = 'error'
        msg = errors[0] if errors else 'no file was downloaded'
        return {'failed': True, 'error': _friendly_error(msg), 'detail': '\n'.join(errors) or msg,
                'log': job['log'][-40:]}

    job['status'] = 'done'
    return {
        'items': items,
        'title': title or job['title'],
        'uploader': job.get('uploader'),
        'engine': job['engine'],
        'warnings': [l for l in job['log'] if l.startswith('warning:')][-10:],
    }


def api_progress(arg):
    job = _JOBS.get(arg.get('job'))
    if not job:
        return {'status': 'unknown'}
    out = {k: job.get(k) for k in ('status', 'downloaded', 'total', 'speed', 'eta', 'stage', 'title',
                                     'thumbnail', 'item_index', 'item_count', 'fragment', 'uploader',
                                     'connections', 'engine')}
    out['last_log'] = job['log'][-1] if job['log'] else None
    return out


def api_cancel(arg):
    job = _JOBS.get(arg.get('job'))
    if job:
        job['cancel'] = True
    return {'cancelled': bool(job)}


def api_forget(arg):
    with _JOBS_LOCK:
        _JOBS.pop(arg.get('job'), None)
    return {}


def api_log(arg):
    return {'log': _GLOBAL_LOG[-int(arg.get('lines') or 300):]}


def _pypi_latest(pkg):
    data = json.loads(_http_get(f'https://pypi.org/pypi/{pkg}/json'))
    version = data['info']['version']
    wheel = None
    for f in data.get('urls') or []:
        if f.get('packagetype') == 'bdist_wheel' and f['filename'].endswith('-py3-none-any.whl'):
            wheel = f
            break
    return version, wheel


def _version_tuple(v):
    # PyPI says 2026.8.19 while yt-dlp reports 2026.08.19
    return tuple(int(x) for x in re.findall(r'\d+', v or ''))


def api_check_update(arg):
    info = _engine_info()
    latest, _ = _pypi_latest('yt-dlp')
    newer = _version_tuple(latest) > _version_tuple(info.get('yt_dlp'))
    return {'current': info.get('yt_dlp'), 'latest': latest, 'update_available': newer}


def api_update_engine(arg):
    """Download the newest pure-Python wheels from PyPI and hot-swap the engine."""
    site = _STATE['engine_site']
    new_site = site + '.new'
    old_site = site + '.old'
    shutil.rmtree(new_site, ignore_errors=True)
    os.makedirs(new_site)
    versions = {}
    try:
        for pkg in UPDATABLE_PACKAGES:
            version, wheel = _pypi_latest(pkg)
            if not wheel:
                raise RuntimeError(f'no pure-python wheel for {pkg} {version}')
            blob = _http_get(wheel['url'], timeout=120)
            want = (wheel.get('digests') or {}).get('sha256')
            if want and hashlib.sha256(blob).hexdigest() != want:
                raise RuntimeError(f'checksum mismatch for {pkg}')
            path = os.path.join(new_site, wheel['filename'])
            with open(path, 'wb') as fh:
                fh.write(blob)
            with zipfile.ZipFile(path) as zf:
                zf.extractall(new_site)
            os.remove(path)
            versions[pkg] = version
            _log(f'update: fetched {pkg} {version}')
        with open(os.path.join(new_site, 'nazzel-engine.json'), 'w') as fh:
            json.dump({'versions': versions, 'updated': time.time()}, fh)
    except Exception:
        shutil.rmtree(new_site, ignore_errors=True)
        raise

    shutil.rmtree(old_site, ignore_errors=True)
    if os.path.isdir(site):
        os.rename(site, old_site)
    os.rename(new_site, site)

    with _IMPORT_LOCK:
        _purge_modules()
        _activate_engine_site()
    try:
        info = api_warmup({})
        if info.get('source') != 'updated':
            raise RuntimeError('updated engine did not load')
    except Exception:
        # roll back to the previous engine
        _log('update failed to load, rolling back:\n' + traceback.format_exc())
        shutil.rmtree(site, ignore_errors=True)
        if os.path.isdir(old_site):
            os.rename(old_site, site)
        with _IMPORT_LOCK:
            _purge_modules()
            _activate_engine_site()
        api_warmup({})
        raise RuntimeError('التحديث ما اشتغل، رجعت للنسخة السابقة')
    shutil.rmtree(old_site, ignore_errors=True)
    return {'versions': versions, 'info': info}


def api_reset_engine(arg):
    site = _STATE['engine_site']
    with _IMPORT_LOCK:
        shutil.rmtree(site, ignore_errors=True)
        _purge_modules()
        _activate_engine_site()
    return api_warmup({})


def api_selftest(arg):
    """Used by CI on the iOS simulator. Checks the embedded runtime end to end."""
    report = {'engine': _engine_info()}
    checks = {}
    try:
        import ssl as _ssl
        checks['openssl'] = _ssl.OPENSSL_VERSION
    except Exception as e:
        checks['openssl'] = f'FAIL {e!r}'
    try:
        v, _ = _pypi_latest('yt-dlp')
        checks['https_pypi'] = f'ok latest={v}'
    except Exception as e:
        checks['https_pypi'] = f'FAIL {e!r}'
    try:
        import yt_dlp
        checks['extractors'] = len(yt_dlp.extractor.gen_extractor_classes())
    except Exception as e:
        checks['extractors'] = f'FAIL {e!r}'
    if arg.get('js'):
        try:
            from yt_dlp_plugins.webkit_jsi.lib.easy import WKJSE_Factory, WKJSE_Webview, jsres_to_log
            from yt_dlp_plugins.webkit_jsi.lib.logging import DefaultLoggerImpl
            out = []
            factory = WKJSE_Factory(DefaultLoggerImpl())
            send = factory.__enter__()
            try:
                wv = WKJSE_Webview(send).__enter__()
                try:
                    wv.on_script_log(lambda m: out.append(jsres_to_log(*m['argsArr'])))
                    wv.execute_js('console.log(String(6 * 7))')
                finally:
                    wv.__exit__(None, None, None)
            finally:
                factory.__exit__(None, None, None)
            checks['webkit_js'] = 'ok ' + ''.join(out).strip()
        except Exception as e:
            checks['webkit_js'] = f'FAIL {e!r}'
    report['checks'] = checks
    return report


# --------------------------------------------------------------------------
# play without downloading, and list playlists / channels
# --------------------------------------------------------------------------

def _codec_ok(f, av1):
    v = (f.get('vcodec') or '').lower()
    if v.startswith(('vp9', 'vp09', 'vp8')) or f.get('ext') == 'webm':
        return False
    if v.startswith('av01') and not av1:
        return False
    return True


def _pick_subtitles(info, langs, limit=2):
    """[{'lang', 'url'}] for the wanted languages: real subtitles first, then automatic ones."""
    out = []
    for lang in langs or []:
        for pool in (info.get('subtitles') or {}, info.get('automatic_captions') or {}):
            tracks = pool.get(lang) or []
            vtt = next((t for t in tracks if t.get('ext') == 'vtt' and t.get('url')), None)
            if vtt:
                out.append({'lang': lang, 'url': vtt['url']})
                break
        if len(out) >= limit:
            break
    return out


def api_stream(arg):
    """Something the iPhone's own player can play right away: an HLS manifest (adaptive
    quality, like the YouTube app), else one file with picture and sound, else a separate
    video + audio pair the app joins on the fly."""
    yt_dlp = _yt_dlp()
    url = arg['url'].strip()
    audio_only = bool(arg.get('audio'))
    av1 = bool(arg.get('av1'))
    log = _QuietLogger()
    with _cookie_copy(arg.get('cookies'), 'stream') as cookies:
        opts = _lookup_opts(log, cookies, noplaylist=True, playlistend=1, format='bv*+ba/b',
                            ignore_no_formats_error=True)
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            info = ydl.sanitize_info(info) if info else None
    if info and (info.get('_type') == 'playlist' or info.get('entries') is not None):
        entries = [e for e in (info.get('entries') or []) if e]
        info = entries[0] if entries else None
    if not info:
        return {'failed': True, 'error': _friendly_error(log.last_error or 'no video'),
                'detail': log.last_error or ''}

    fmts = [f for f in (info.get('formats') or []) if f.get('url')]
    if not fmts and info.get('url'):
        fmts = [info]

    def proto(f):
        return f.get('protocol') or ''

    def direct(f):
        # one plain file (DASH 'http_dash_segments' are many pieces: the player can't open those)
        return proto(f) in ('http', 'https')

    def height(f):
        return f.get('height') or 0

    def headers(f):
        return {k: v for k, v in (f.get('http_headers') or {}).items() if isinstance(v, str)}

    result = None
    if audio_only:
        audios = [f for f in fmts if f.get('vcodec') == 'none' and f.get('acodec') not in (None, 'none')
                  and direct(f)
                  and ((f.get('acodec') or '').startswith('mp4a') or f.get('ext') in ('m4a', 'mp4', 'mp3'))]
        if audios:
            best = max(audios, key=lambda f: f.get('abr') or f.get('tbr') or 0)
            result = {'kind': 'audio', 'url': best['url'], 'headers': headers(best)}
    if not result:
        hls = [f for f in fmts if proto(f).startswith('m3u8') and _codec_ok(f, av1)]
        if hls:
            best = max(hls, key=lambda f: (height(f), f.get('tbr') or 0))
            result = {'kind': 'hls', 'url': best.get('manifest_url') or best['url'], 'headers': headers(best),
                      'height': height(best)}
    if not result:
        muxed = [f for f in fmts if direct(f) and f.get('vcodec') != 'none'
                 and f.get('acodec') != 'none' and _codec_ok(f, av1)]
        if muxed:
            best = max(muxed, key=lambda f: (height(f), f.get('tbr') or 0))
            result = {'kind': 'file', 'url': best['url'], 'headers': headers(best), 'height': height(best)}
    if not result:
        videos = [f for f in fmts if direct(f) and f.get('vcodec') not in (None, 'none')
                  and f.get('acodec') == 'none' and _codec_ok(f, av1) and height(f) <= 1080]
        audios = [f for f in fmts if direct(f) and f.get('vcodec') == 'none'
                  and (f.get('acodec') or '').startswith('mp4a')]
        if videos and audios:
            v = max(videos, key=lambda f: (height(f), f.get('tbr') or 0))
            a = max(audios, key=lambda f: f.get('abr') or f.get('tbr') or 0)
            result = {'kind': 'pair', 'video': v['url'], 'audio': a['url'], 'headers': headers(v),
                      'height': height(v)}
    if not result:
        return {'failed': True, 'error': 'ما لقيت نسخة يقدر الآيفون يشغلها مباشرة. جرب التحميل بدالها.',
                'detail': f'{len(fmts)} formats'}

    thumb = info.get('thumbnail') or next((t.get('url') for t in reversed(info.get('thumbnails') or [])
                                           if t.get('url')), None)
    result.update({
        'title': info.get('title') or info.get('id') or 'فيديو',
        'uploader': info.get('uploader') or info.get('channel'),
        'id': info.get('id'),
        'duration': info.get('duration'),
        'thumbnail': thumb,
        'webpage_url': info.get('webpage_url') or url,
        'is_live': bool(info.get('is_live')),
        'subtitles': _pick_subtitles(info, arg.get('subtitles')),
    })
    _log(f'stream {result["kind"]} {result.get("height") or ""}p {result["title"][:60]}')
    return result


_CHANNEL_ROOT = re.compile(
    r'^(https?://(?:www\.|m\.)?youtube\.com/(?:@[^/?#]+|channel/[^/?#]+|c/[^/?#]+|user/[^/?#]+))/?(?:[?#].*)?$')


def api_list(arg):
    """The videos inside a playlist or channel, without downloading anything."""
    yt_dlp = _yt_dlp()
    url = arg['url'].strip()
    root = _CHANNEL_ROOT.match(url)
    if root:
        url = root.group(1) + '/videos'
    limit = max(1, min(int(arg.get('limit') or 300), 1000))
    log = _QuietLogger()
    with _cookie_copy(arg.get('cookies'), 'list') as cookies:
        opts = _lookup_opts(log, cookies, extract_flat='in_playlist', playlistend=limit)
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            info = ydl.sanitize_info(info) if info else None
    if not info:
        return {'failed': True, 'error': _friendly_error(log.last_error or 'no playlist'),
                'detail': log.last_error or ''}
    if info.get('_type') not in ('playlist', 'multi_video') and info.get('entries') is None:
        return {'playlist': False, 'title': info.get('title')}
    entries = []
    for e in info.get('entries') or []:
        if not e:
            continue
        link = e.get('url') or e.get('webpage_url')
        if link and not link.startswith('http') and (e.get('ie_key') or '').startswith('Youtube'):
            link = 'https://www.youtube.com/watch?v=' + link
        if not link:
            continue
        thumb = e.get('thumbnail') or next((t.get('url') for t in reversed(e.get('thumbnails') or [])
                                            if t.get('url')), None)
        entries.append({'url': link, 'id': e.get('id'), 'title': e.get('title') or e.get('id') or link,
                        'duration': e.get('duration'), 'thumbnail': thumb,
                        'uploader': e.get('uploader') or e.get('channel')})
    return {'playlist': True, 'title': info.get('title') or '', 'uploader': info.get('uploader') or info.get('channel'),
            'count': len(entries), 'entries': entries}


# --------------------------------------------------------------------------
# Archives and app files (open them inside the app)
# --------------------------------------------------------------------------

def _unique_dir(parent, name):
    path = os.path.join(parent, name)
    n = 2
    while os.path.exists(path):
        path = os.path.join(parent, f'{name} ({n})')
        n += 1
    return path


def _safe_target(root, name):
    """Where an archive entry goes; None for names that would escape the folder (zip slip)."""
    name = name.replace('\\', '/').lstrip('/')
    target = os.path.realpath(os.path.join(root, name))
    return target if target == os.path.realpath(root) or target.startswith(os.path.realpath(root) + os.sep) else None


def api_extract(arg):
    """Opens zip / tar / tgz / tbz2 / txz / gz / bz2 / xz into a new folder next to the archive."""
    path = arg['path']
    password = (arg.get('password') or '').encode() or None
    base = os.path.basename(path)
    lower = base.lower()
    stem = base
    for ext in ('.tar.gz', '.tar.bz2', '.tar.xz', '.tgz', '.tbz2', '.txz', '.zip', '.tar', '.gz', '.bz2', '.xz',
                '.ipa', '.tipa', '.apk', '.epub', '.cbz', '.docx', '.xlsx', '.pptx'):
        if lower.endswith(ext):
            stem = base[:-len(ext)]
            break
    if lower.endswith(('.rar', '.7z', '.cab', '.zst')):
        return {'failed': True, 'error': 'ملفات RAR و7z ما ينفتحون داخل التطبيق. افتحها بتطبيق الملفات أو شاركها لتطبيق ثاني.'}
    dest = _unique_dir(arg.get('into') or os.path.dirname(path), stem)
    count = 0
    try:
        if zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as z:
                if password:
                    z.setpassword(password)
                for info in z.infolist():
                    name = info.filename
                    if not info.flag_bits & 0x800:
                        # old zips (Windows) store names in cp437; Arabic ones are often cp1256
                        raw = name.encode('cp437', 'replace')
                        for enc in ('utf-8', 'cp1256'):
                            try:
                                name = raw.decode(enc)
                                break
                            except UnicodeDecodeError:
                                continue
                    target = _safe_target(dest, name)
                    if not target or name.startswith('__MACOSX/') or os.path.basename(name) == '.DS_Store':
                        continue
                    if info.is_dir():
                        os.makedirs(target, exist_ok=True)
                        continue
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    with z.open(info) as src, open(target, 'wb') as out:
                        shutil.copyfileobj(src, out, 1024 * 1024)
                    count += 1
        elif lower.endswith(('.tar', '.tar.gz', '.tgz', '.tar.bz2', '.tbz2', '.tar.xz', '.txz')):
            import tarfile
            with tarfile.open(path) as t:
                for member in t.getmembers():
                    if not (member.isfile() or member.isdir()):
                        continue
                    target = _safe_target(dest, member.name)
                    if not target:
                        continue
                    if member.isdir():
                        os.makedirs(target, exist_ok=True)
                        continue
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    src = t.extractfile(member)
                    if src:
                        with src, open(target, 'wb') as out:
                            shutil.copyfileobj(src, out, 1024 * 1024)
                        count += 1
        elif lower.endswith(('.gz', '.bz2', '.xz')):
            import gzip, bz2, lzma
            opener = gzip.open if lower.endswith('.gz') else bz2.open if lower.endswith('.bz2') else lzma.open
            os.makedirs(dest, exist_ok=True)
            with opener(path, 'rb') as src, open(os.path.join(dest, stem), 'wb') as out:
                shutil.copyfileobj(src, out, 1024 * 1024)
            count = 1
        else:
            return {'failed': True, 'error': 'هذا الملف مو أرشيف مضغوط أعرف أفتحه.'}
    except RuntimeError as e:
        shutil.rmtree(dest, ignore_errors=True)
        if 'password' in str(e).lower() or 'encrypted' in str(e).lower():
            return {'failed': True, 'need_password': True, 'error': 'الملف محمي بكلمة سر' + (' (غلط)' if password else '')}
        return {'failed': True, 'error': str(e)}
    except NotImplementedError:
        shutil.rmtree(dest, ignore_errors=True)
        return {'failed': True, 'error': 'التشفير في هالملف (AES) ما أقدر أفتحه.'}
    except Exception as e:
        shutil.rmtree(dest, ignore_errors=True)
        return {'failed': True, 'error': _clean_error(str(e))}
    return {'folder': dest, 'count': count}


def api_ipa_info(arg):
    """Name, bundle id, version and icon of an .ipa / .tipa (read from the app inside)."""
    import plistlib
    path = arg['path']
    try:
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
            plist_name = next((n for n in names if re.match(r'^Payload/[^/]+\.app/Info\.plist$', n)), None)
            if not plist_name:
                return {'failed': True, 'error': 'ما لقيت تطبيق داخل الملف'}
            info = plistlib.loads(z.read(plist_name))
            app_dir = plist_name[:-len('Info.plist')]
            icon_names = []
            icons = (info.get('CFBundleIcons') or {}).get('CFBundlePrimaryIcon') or {}
            icon_names += icons.get('CFBundleIconFiles') or []
            if icons.get('CFBundleIconName'):
                icon_names.append(icons['CFBundleIconName'])
            icon_names += info.get('CFBundleIconFiles') or []
            candidates = [n for n in names if n.startswith(app_dir) and n.count('/') == app_dir.count('/')
                          and n.lower().endswith('.png') and any(os.path.basename(n).startswith(i) for i in icon_names)]
            if not candidates:
                candidates = [n for n in names if n.startswith(app_dir) and 'appicon' in n.lower() and n.lower().endswith('.png')]
            icon_path = None
            if candidates and arg.get('icon_dir'):
                best = max(candidates, key=lambda n: z.getinfo(n).file_size)
                icon_path = os.path.join(arg['icon_dir'], 'icon-' + hashlib.md5(path.encode()).hexdigest()[:10] + '.png')
                with open(icon_path, 'wb') as fh:
                    fh.write(z.read(best))
            size = sum(i.file_size for i in z.infolist())
        return {'name': info.get('CFBundleDisplayName') or info.get('CFBundleName') or os.path.basename(path),
                'bundle': info.get('CFBundleIdentifier') or '', 'version': info.get('CFBundleShortVersionString') or '',
                'build': info.get('CFBundleVersion') or '', 'min_ios': info.get('MinimumOSVersion') or '',
                'icon': icon_path, 'installed_size': size}
    except zipfile.BadZipFile:
        return {'failed': True, 'error': 'الملف تالف أو مو IPA'}
    except Exception as e:
        return {'failed': True, 'error': _clean_error(str(e))}


_API = {
    'init': api_init,
    'warmup': api_warmup,
    'info': api_info,
    'download': api_download,
    'progress': api_progress,
    'cancel': api_cancel,
    'forget': api_forget,
    'log': api_log,
    'check_update': api_check_update,
    'update_engine': api_update_engine,
    'reset_engine': api_reset_engine,
    'selftest': api_selftest,
    'stream': api_stream,
    'list': api_list,
    'extract': api_extract,
    'ipa_info': api_ipa_info,
}


def call(name, arg_text):
    """Single entry point used by the C bridge."""
    try:
        fn = _API[name]
        arg = json.loads(arg_text) if arg_text else {}
        result = fn(arg) or {}
        if result.get('failed') or result.get('cancelled'):
            return json.dumps(dict(result, ok=False), ensure_ascii=False, default=str)
        return json.dumps(dict(result, ok=True), ensure_ascii=False, default=str)
    except Exception as e:
        tb = traceback.format_exc()
        _log(f'call {name} failed:\n{tb}')
        return json.dumps({'ok': False, 'error': _friendly_error(str(e)) if name == 'download' else str(e),
                           'detail': tb[-3000:]}, ensure_ascii=False)
