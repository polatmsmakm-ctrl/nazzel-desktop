"""The download queue: video (best quality, merged with ffmpeg), sound (MP3 / M4A), photos,
any file (several connections, resumable), with pause / resume that survives closing the app."""
import os
import re
import shutil
import threading
import time
import uuid

from . import engine, media
from .library import LIBRARY
from .settings import settings, data_dir, cache_dir, tool, JsonStore


QUALITIES = {'best': None, '2160': 2160, '1440': 1440, '1080': 1080, '720': 720, '480': 480}


class Job:
    def __init__(self, url, mode, quality, referer=None, as_file=False, job_id=None, title=None):
        self.id = job_id or uuid.uuid4().hex[:12]
        self.url = url
        self.mode = mode
        self.quality = quality
        self.referer = referer
        self.as_file = as_file
        self.title = title
        self.phase = 'queued'          # queued / running / paused / done / failed / cancelled
        self.status = 'في الانتظار'
        self.error = None
        self.files = []
        self.pause_requested = False
        self.cancel_requested = False
        self.resume_next = False
        self.created = time.time()

    @property
    def workdir(self):
        return os.path.join(cache_dir(), 'work', self.id)

    def public(self):
        progress = engine._JOBS.get(self.id, {}) if self.phase == 'running' else {}
        total = progress.get('total') or 0
        done = progress.get('downloaded') or 0
        return {
            'id': self.id, 'url': self.url, 'mode': self.mode, 'title': self.title or progress.get('title') or self.url,
            'phase': self.phase, 'status': self.status, 'error': self.error, 'files': self.files,
            'fraction': (done / total) if total else None, 'speed': progress.get('speed'), 'eta': progress.get('eta'),
            'downloaded': done, 'total': total, 'thumbnail': progress.get('thumbnail'),
        }


class DownloadManager:
    def __init__(self):
        self.jobs = []
        self.lock = threading.RLock()
        self.queue_store = JsonStore('queue.json', [])
        self._started = False
        for saved in self.queue_store.data if isinstance(self.queue_store.data, list) else []:
            job = Job(saved['url'], saved['mode'], saved['quality'], saved.get('referer'), saved.get('as_file', False),
                      saved['id'], saved.get('title'))
            job.phase = 'paused'
            job.resume_next = True
            job.status = 'متوقف · اضغط ▶ عشان يكمل من وين وقف'
            self.jobs.append(job)
        self.on_finished = []       # callbacks(job)

    # ---------------------------------------------------------------- queue

    def add(self, url, mode=None, quality=None, referer=None, as_file=False):
        url = (url or '').strip()
        if not url:
            return None
        s = settings()
        job = Job(url, mode or s.get('mode'), quality or s.get('quality'), referer, as_file)
        with self.lock:
            self.jobs.insert(0, job)
        self._save()
        self._pump()
        return job.id

    def add_many(self, urls, mode=None, quality=None):
        return [self.add(u, mode, quality) for u in urls if u]

    def find(self, job_id):
        return next((j for j in self.jobs if j.id == job_id), None)

    def pause(self, job_id):
        job = self.find(job_id)
        if not job:
            return
        if job.phase == 'queued':
            job.phase, job.status = 'paused', 'متوقف مؤقتاً'
        elif job.phase == 'running':
            job.pause_requested = True
            job.status = 'جاري الإيقاف…'
            engine.api_cancel({'job': job.id})
        self._save()

    def resume(self, job_id):
        job = self.find(job_id)
        if job and job.phase == 'paused':
            job.pause_requested = job.cancel_requested = False
            job.resume_next = True
            job.phase, job.status = 'queued', 'في الانتظار'
            self._save()
            self._pump()

    def cancel(self, job_id):
        job = self.find(job_id)
        if not job:
            return
        job.cancel_requested = True
        if job.phase in ('queued', 'paused'):
            job.phase, job.status = 'cancelled', 'تم الإلغاء'
            shutil.rmtree(job.workdir, ignore_errors=True)
        elif job.phase == 'running':
            engine.api_cancel({'job': job.id})
        self._save()

    def retry(self, job_id):
        job = self.find(job_id)
        if job:
            self.remove(job_id)
            self.add(job.url, job.mode, job.quality, job.referer, job.as_file)

    def remove(self, job_id):
        job = self.find(job_id)
        if not job:
            return
        if job.phase == 'running':
            self.cancel(job_id)
        with self.lock:
            self.jobs = [j for j in self.jobs if j.id != job_id]
        shutil.rmtree(job.workdir, ignore_errors=True)
        self._save()

    def clear_finished(self):
        with self.lock:
            self.jobs = [j for j in self.jobs if j.phase in ('queued', 'running', 'paused')]

    def snapshot(self):
        return [j.public() for j in self.jobs]

    def _save(self):
        with self.lock:
            self.queue_store.data = [
                {'id': j.id, 'url': j.url, 'mode': j.mode, 'quality': j.quality, 'referer': j.referer,
                 'as_file': j.as_file, 'title': j.title}
                for j in self.jobs if j.phase in ('queued', 'running', 'paused')]
        self.queue_store.save()

    def _pump(self):
        with self.lock:
            running = sum(1 for j in self.jobs if j.phase == 'running')
            limit = max(1, min(6, int(settings().get('parallel') or 3)))
            for job in reversed(self.jobs):
                if running >= limit:
                    break
                if job.phase == 'queued':
                    job.phase = 'running'
                    job.status = 'جاري قراءة الرابط…'
                    running += 1
                    threading.Thread(target=self._run, args=(job,), daemon=True).start()

    # ---------------------------------------------------------------- one job

    def _run(self, job):
        resuming = job.resume_next
        job.resume_next = False
        if not resuming:
            shutil.rmtree(job.workdir, ignore_errors=True)
        os.makedirs(job.workdir, exist_ok=True)
        try:
            result = download(job, resume=resuming)
        except Exception as e:     # never lose the queue to one bad link
            result = {'failed': True, 'error': engine._friendly_error(str(e)), 'detail': str(e)}
        if job.pause_requested and not job.cancel_requested:
            job.pause_requested = False
            job.phase, job.status, job.resume_next = 'paused', 'متوقف مؤقتاً', True
        elif result.get('cancelled') or job.cancel_requested:
            job.phase, job.status = 'cancelled', 'تم الإلغاء'
            shutil.rmtree(job.workdir, ignore_errors=True)
        elif result.get('failed'):
            job.phase, job.status = 'failed', 'فشل التحميل'
            job.error = result.get('error') or 'صار خطأ'
            shutil.rmtree(job.workdir, ignore_errors=True)
        else:
            job.title = result.get('title') or job.title
            job.status = 'جاري تجهيز الملف…'
            job.files = finish(job, result)
            job.phase = 'done'
            job.status = 'تم ✓ موجود في الملفات' if job.files else 'تم'
            shutil.rmtree(job.workdir, ignore_errors=True)
            for callback in self.on_finished:
                try:
                    callback(job)
                except Exception:
                    pass
        engine.api_forget({'job': job.id})
        self._save()
        self._pump()


MANAGER = None


def manager():
    global MANAGER
    if MANAGER is None:
        MANAGER = DownloadManager()
    return MANAGER


# -------------------------------------------------------------------- engine glue

def _ffmpeg_dir():
    path = tool('ffmpeg')
    return os.path.dirname(path) if path else None


def _ytdlp(job, url, mode, quality, workdir, cookies, extra=None):
    """yt-dlp with ffmpeg: best picture + best sound merged (MP4), or the sound as MP3 / M4A."""
    yt_dlp = engine._yt_dlp()
    state = engine._JOBS[job.id]
    s = settings()

    def hook(d):
        if state.get('cancel'):
            from yt_dlp.utils import DownloadCancelled
            raise DownloadCancelled('cancelled')
        if d.get('status') == 'downloading':
            state['status'] = 'downloading'
            state['downloaded'] = d.get('downloaded_bytes') or 0
            state['total'] = d.get('total_bytes') or d.get('total_bytes_estimate')
            state['speed'] = d.get('speed')
            state['eta'] = d.get('eta')
        info = d.get('info_dict') or {}
        state['title'] = info.get('title') or state.get('title')
        state['thumbnail'] = info.get('thumbnail') or state.get('thumbnail')
        state['uploader'] = info.get('uploader') or info.get('channel') or state.get('uploader')

    opts = {
        'outtmpl': os.path.join(workdir, '%(title).140B [%(id)s].%(ext)s'),
        'quiet': True, 'no_color': True, 'noprogress': True,
        'logger': engine._JobLogger(state),
        'progress_hooks': [hook],
        'noplaylist': True,
        'retries': 10, 'fragment_retries': 10, 'socket_timeout': 30,
        'concurrent_fragment_downloads': max(1, int(s.get('connections') or 8)),
        'continuedl': True,
        'writethumbnail': True,
        'windowsfilenames': True,
        'cachedir': os.path.join(cache_dir(), 'yt-dlp'),
        'remote_components': ['ejs:github'],
    }
    ffmpeg = _ffmpeg_dir()
    if ffmpeg:
        opts['ffmpeg_location'] = ffmpeg
    if cookies:
        opts['cookiefile'] = cookies
    height = QUALITIES.get(str(quality))
    post = []
    if mode == 'audio':
        fmt = s.get('audio_format') or 'mp3'
        opts['format'] = 'ba/b'
        post += [{'key': 'FFmpegExtractAudio', 'preferredcodec': fmt, 'preferredquality': str(s.get('mp3_kbps') or 256)},
                 {'key': 'FFmpegMetadata'}, {'key': 'EmbedThumbnail'}]
    else:
        limit = f'[height<={height}]' if height else ''
        opts['format'] = f'bv*{limit}+ba/b{limit}/bv*+ba/b'
        opts['merge_output_format'] = 'mp4/mkv'
        opts['format_sort'] = ['vcodec:h264', 'res', 'acodec:aac'] if height and height <= 1080 else ['res', 'fps']
        if s.get('subtitles'):
            opts.update({'writesubtitles': True, 'subtitleslangs': ['ar', 'ar-.*', 'en'], 'subtitlesformat': 'srt/vtt/best'})
            post.append({'key': 'FFmpegSubtitlesConvertor', 'format': 'srt'})
        post.append({'key': 'FFmpegMetadata'})
    opts['postprocessors'] = post
    if extra:
        opts.update(extra)
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
    if state.get('cancel'):
        raise engine.Cancelled()
    items = []
    for name in sorted(os.listdir(workdir)):
        path = os.path.join(workdir, name)
        e = media.ext(name)
        if not os.path.isfile(path) or name.endswith(('.part', '.ytdl', '.temp')) or e in ('jpg', 'webp', 'png') \
                and mode != 'photos':
            continue
        if e in ('srt', 'vtt'):
            continue
        items.append({'path': path, 'title': (info or {}).get('title'), 'uploader': (info or {}).get('uploader'),
                      'source': url})
    return items, (info or {}).get('title')


def download(job, resume=False):
    """Everything the engine knows (files, IPA links, GIF pages, galleries) plus yt-dlp with ffmpeg."""
    url, workdir = job.url, job.workdir
    state = engine._new_job(job.id)
    cookies = os.path.join(data_dir(), 'cookies.txt')
    cookies = cookies if os.path.exists(cookies) and os.path.getsize(cookies) > 0 else None
    connections = int(settings().get('connections') or 8)
    as_file = job.as_file
    try:
        ipa = engine._ipa_from_manifest(url, job.referer, cookies)
    except Exception as e:
        return {'failed': True, 'error': engine._friendly_error(str(e))}
    if ipa:
        url, as_file = ipa, True
    gif = None if as_file else engine._gif_from_page(url, job.referer, cookies)
    if gif:
        url, as_file = gif, True

    order = ['gallery', 'yt-dlp'] if job.mode == 'photos' else ['yt-dlp', 'gallery']
    if as_file or engine._looks_like_file(url):
        order = ['direct']
    elif engine._path_ext(url) not in (engine.MEDIA_EXTS | engine.MANIFEST_EXTS) and engine._only_generic(url) \
            and engine._probe_is_file(url, job.referer, cookies):
        order = ['direct']
    elif engine._path_ext(url) not in engine.MANIFEST_EXTS:
        order.append('direct')

    errors = []
    for position, name in enumerate(order):
        if not (resume and position == 0):
            engine._clear_dir(workdir)
        try:
            if name == 'direct':
                items, title = engine._run_direct(state, url, workdir, cookies, job.referer, connections,
                                                  force=as_file, resume=resume and position == 0)
                items = [{'path': i['path'], 'title': i.get('title'), 'uploader': i.get('uploader'), 'source': job.url}
                         for i in items]
            elif name == 'gallery':
                if not engine._gallery_supports(url):
                    continue
                items, title = engine._run_gallery(state, url, workdir, cookies, 60)
                items = [{'path': i['path'], 'title': i.get('title'), 'uploader': i.get('uploader'), 'source': job.url}
                         for i in items]
            else:
                try:
                    items, title = _ytdlp(job, url, job.mode, job.quality, workdir, cookies)
                except Exception as e:
                    if not engine._is_age_gate(str(e)) or state.get('cancel'):
                        raise
                    engine._clear_dir(workdir)
                    items, title = _ytdlp(job, url, job.mode, job.quality, workdir, cookies, engine._age_opts({}))
            if items:
                return {'items': items, 'title': title}
            errors.append('no file was downloaded')
        except Exception as e:
            if state.get('cancel') or isinstance(e, engine.Cancelled) or 'cancel' in type(e).__name__.lower():
                return {'cancelled': True}
            errors.append(engine._clean_error(getattr(e, 'msg', None) or str(e)))
    message = errors[0] if errors else 'no file was downloaded'
    return {'failed': True, 'error': engine._friendly_error(message), 'detail': '\n'.join(errors)}


def finish(job, result):
    """Moves the results into the library (with subtitles and cover) and fetches Arabic subtitles for films."""
    from . import subs
    folder = settings().downloads
    out = []
    for item in result.get('items', []):
        src = item['path']
        if not os.path.exists(src):
            continue
        dest = media.unique(os.path.join(folder, os.path.basename(src)))
        shutil.move(src, dest)
        stem_src = os.path.splitext(src)[0]
        stem_dest = os.path.splitext(dest)[0]
        workdir = os.path.dirname(src)
        for name in os.listdir(workdir):
            path = os.path.join(workdir, name)
            if not name.startswith(os.path.basename(stem_src)):
                continue
            rest = name[len(os.path.basename(stem_src)):]
            if media.ext(name) in ('srt', 'vtt'):
                shutil.move(path, stem_dest + rest)
        cover = next((os.path.join(workdir, n) for n in os.listdir(workdir)
                      if n.startswith(os.path.basename(stem_src)) and media.ext(n) in ('jpg', 'webp', 'png')), None)
        LIBRARY.set_meta(dest, title=item.get('title') or job.title, uploader=item.get('uploader'),
                         source=item.get('source') or job.url, cover=cover)
        if media.kind(dest) == 'video' and job.mode == 'video' and settings().get('arabic_film_subs'):
            page = job.referer if subs.target_from_page(job.referer) else None
            big = os.path.getsize(dest) > 150 * 1024 * 1024
            if page or (big and subs.guess(os.path.basename(stem_dest))):
                job.status = 'أجيب الترجمات العربية…'
                try:
                    subs.fetch_for(dest, page)
                except Exception:
                    pass
        out.append(dest)
    return out
