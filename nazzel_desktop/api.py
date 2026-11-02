"""What the interface (JavaScript) can call. pywebview runs each call on its own thread."""
import ctypes
import os
import re
import subprocess
import threading
import time
import uuid

from .core import engine, media, gemini, subs
from .core.downloads import manager
from .core.extras import SUBSCRIPTIONS, extract, ipa_info
from .core.library import LIBRARY
from .core.settings import settings, cache_dir, VERSION, JsonStore

URL_RE = re.compile(r'https?://[^\s<>"\']+', re.I)


def read_clipboard():
    """Text on the Windows clipboard (no extra modules)."""
    if os.name != 'nt':
        return ''
    user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
    user32.GetClipboardData.restype = ctypes.c_void_p
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    if not user32.OpenClipboard(None):
        return ''
    try:
        handle = user32.GetClipboardData(13)      # CF_UNICODETEXT
        if not handle:
            return ''
        pointer = kernel32.GlobalLock(handle)
        try:
            return ctypes.wstring_at(pointer) if pointer else ''
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


ACTIONS = [
    {'name': 'download_media', 'description': 'Download a video, its sound (MP3) or the photos of a post from a link.',
     'parameters': {'type': 'OBJECT', 'properties': {'url': {'type': 'STRING'},
                                                     'kind': {'type': 'STRING', 'enum': ['video', 'audio', 'photos']}},
                    'required': ['url']}},
    {'name': 'download_latest_from_channel', 'description': 'Download the newest N videos of a channel / account / playlist.',
     'parameters': {'type': 'OBJECT', 'properties': {'url': {'type': 'STRING'}, 'count': {'type': 'INTEGER'},
                                                     'kind': {'type': 'STRING', 'enum': ['video', 'audio']}},
                    'required': ['url', 'count']}},
    {'name': 'follow_channel', 'description': 'Follow a channel: every new video downloads automatically.',
     'parameters': {'type': 'OBJECT', 'properties': {'url': {'type': 'STRING'},
                                                     'kind': {'type': 'STRING', 'enum': ['video', 'audio']}},
                    'required': ['url']}},
    {'name': 'search_library', 'description': 'List downloaded files whose title matches (empty = newest).',
     'parameters': {'type': 'OBJECT', 'properties': {'query': {'type': 'STRING'}}}},
    {'name': 'play_from_library', 'description': 'Play a downloaded file in the player.',
     'parameters': {'type': 'OBJECT', 'properties': {'query': {'type': 'STRING'}}, 'required': ['query']}},
]
ACTION_WORDS = ('نزل', 'نزّل', 'حمل', 'حمّل', 'تحميل', 'تابع', 'شغل', 'شغّل', 'ملفاتي', 'download', 'follow', 'play')


class Api:
    def __init__(self):
        self.window = None
        self.base_url = ''
        self.tasks = {}
        self.chat = JsonStore('assistant.json', [])
        self._clip_seen = ''
        self.notices = []

    # ------------------------------------------------------------ general

    def info(self):
        from .core.settings import tool
        return {'version': VERSION, 'engine': engine._engine_info(), 'ffmpeg': bool(tool('ffmpeg')), 'deno': bool(tool('deno'))}

    def get_settings(self):
        data = dict(settings().data)
        data['gemini_key_set'] = bool(data.get('gemini_key'))
        return data

    def save_settings(self, values):
        return settings().update(values)

    def choose_folder(self):
        import webview
        result = self.window.create_file_dialog(webview.FOLDER_DIALOG, directory=settings().downloads)
        if result:
            settings().update({'download_dir': result[0]})
            return result[0]
        return None

    def choose_files(self, multiple=True):
        import webview
        result = self.window.create_file_dialog(webview.OPEN_DIALOG, allow_multiple=multiple)
        return list(result or [])

    def open_folder(self, key=''):
        path = LIBRARY.path(key) if key else settings().downloads
        if os.name == 'nt':
            if os.path.isfile(path):
                subprocess.Popen(['explorer', '/select,', os.path.normpath(path)])
            else:
                os.startfile(path)  # noqa
        return True

    def open_external(self, key):
        path = LIBRARY.path(key)
        if os.name == 'nt':
            os.startfile(path)  # noqa
        return True

    def open_url(self, url):
        import webbrowser
        webbrowser.open(url)

    def notices_pop(self):
        out, self.notices = self.notices, []
        return out

    def notify(self, text):
        self.notices.append(text)

    def clipboard_link(self):
        """A new link copied anywhere (offered as a download), once per link."""
        if not settings().get('watch_clipboard'):
            return None
        try:
            text = read_clipboard()
        except Exception:
            return None
        match = URL_RE.search(text or '')
        if not match or match.group(0) == self._clip_seen:
            return None
        self._clip_seen = match.group(0)
        return match.group(0)

    # ------------------------------------------------------------ downloads

    def add(self, text, mode=None, quality=None):
        links = URL_RE.findall(text or '') or ([text.strip()] if text and '.' in text and ' ' not in text.strip() else [])
        links += re.findall(r'itms-services://\S+', text or '')
        ids = manager().add_many([l if '://' in l else 'https://' + l for l in links], mode, quality)
        return len(ids)

    def jobs(self):
        return manager().snapshot()

    def job_action(self, job_id, action):
        m = manager()
        {'pause': m.pause, 'resume': m.resume, 'cancel': m.cancel, 'retry': m.retry, 'remove': m.remove}[action](job_id)
        return True

    def clear_finished(self):
        manager().clear_finished()

    def playlist_entries(self, url):
        data = engine.api_list({'url': url, 'limit': 300})
        return data

    # ------------------------------------------------------------ library

    def library(self, folder=''):
        data = LIBRARY.listing(folder)
        if not folder:
            data['continue'] = LIBRARY.continue_watching()
            data['playlists'] = LIBRARY.playlists()
        return data

    def all_items(self):
        return LIBRARY.all_items()

    def item_details(self, key):
        path = LIBRARY.path(key)
        item = LIBRARY.item(path)
        item['subtitle_tracks'] = LIBRARY.subtitles(path)
        item['position'] = LIBRARY.position(key)
        item['folder_key'] = os.path.dirname(key)
        return item

    def record(self, key, position, duration):
        LIBRARY.record(key, position, duration)

    def rename(self, key, name):
        return LIBRARY.rename(key, name)

    def delete(self, key):
        LIBRARY.delete(key)
        return True

    def move(self, key, folder):
        return LIBRARY.move(key, folder)

    def new_folder(self, name, parent=''):
        return LIBRARY.new_folder(name, parent)

    def folders(self):
        root = settings().downloads
        out = ['']
        for folder, dirs, _ in os.walk(root):
            for d in dirs:
                out.append(LIBRARY.key(os.path.join(folder, d)))
        return out

    def import_files(self, paths, folder=''):
        import shutil
        target = LIBRARY.path(folder) if folder else settings().downloads
        count = 0
        for p in paths:
            try:
                shutil.copy2(p, media.unique(os.path.join(target, os.path.basename(p))))
                count += 1
            except OSError:
                pass
        return count

    def playlists(self):
        return LIBRARY.playlists()

    def playlist(self, list_id):
        return LIBRARY.playlist(list_id)

    def playlist_create(self, name):
        return LIBRARY.playlist_create(name)

    def playlist_add(self, list_id, key):
        LIBRARY.playlist_add(list_id, key)

    def playlist_set(self, list_id, keys):
        LIBRARY.playlist_set(list_id, keys)

    def playlist_delete(self, list_id):
        LIBRARY.playlist_delete(list_id)

    # ------------------------------------------------------------ long tools (background tasks)

    def _task(self, label, work, tool=None, key=None):
        task_id = uuid.uuid4().hex[:8]
        self.tasks[task_id] = {'id': task_id, 'label': label, 'progress': 0, 'done': False, 'error': None, 'result': None,
                               'tool': tool, 'key': key}

        def progress(value):
            self.tasks[task_id]['progress'] = value

        def run():
            try:
                self.tasks[task_id]['result'] = work(progress)
            except Exception as e:
                message = e.friendly() if isinstance(e, gemini.GeminiError) else str(e)
                self.tasks[task_id]['error'] = message[:400]
            self.tasks[task_id]['done'] = True
            self.tasks[task_id]['finished'] = time.time()
        threading.Thread(target=run, daemon=True).start()
        return task_id

    def task_list(self):
        now = time.time()
        for key in [k for k, t in self.tasks.items() if t['done'] and now - t.get('finished', now) > 30]:
            self.tasks.pop(key, None)
        return list(self.tasks.values())

    def tool(self, name, key, options=None):
        options = options or {}
        path = LIBRARY.path(key)
        title = os.path.splitext(os.path.basename(path))[0][:40]
        s = settings()
        jobs = {
            'mp4': ('تحويل إلى MP4', lambda p: LIBRARY.key(media.to_mp4(path, p))),
            'mp3': ('تحويل إلى MP3', lambda p: LIBRARY.key(media.to_audio(path, 'mp3', int(s.get('mp3_kbps') or 256), p))),
            'm4a': ('تحويل إلى M4A', lambda p: LIBRARY.key(media.to_audio(path, 'm4a', 256, p))),
            'compress': ('ضغط', lambda p: LIBRARY.key(media.compress(path, float(options.get('mb') or 16), p))),
            'gif': ('صنع GIF', lambda p: LIBRARY.key(media.gif(path, float(options.get('start') or 0),
                                                              float(options.get('seconds') or 3), int(options.get('fps') or 12),
                                                              int(options.get('width') or 480), p))),
            'frame': ('لقطة', lambda p: LIBRARY.key(media.frame(path, float(options.get('at') or 0)))),
            'trim': ('قص', lambda p: LIBRARY.key(media.trim(path, float(options['start']), float(options['end']), p))),
            'arsubs': ('ترجمات عربية', lambda p: subs.fetch_for(path, LIBRARY.meta.data.get(key, {}).get('source'))),
            'aisubs': ('ترجمة بالذكاء الاصطناعي', lambda p: LIBRARY.key(gemini.ai_subtitles(path, p))),
            'transcript': ('تفريغ نصي', lambda p: LIBRARY.key(gemini.transcript(path, p))),
            'audio_studio': ('استوديو الصوت', lambda p: LIBRARY.key(media.audio_studio(path, options, p))),
            'sticker': ('ملصق', lambda p: LIBRARY.key(media.sticker(path, float(options.get('start') or 0),
                                                                  float(options.get('seconds') or 0), p))),
            'extract': ('فك الضغط', lambda p: self._extract(path, options.get('password'))),
        }
        label, work = jobs[name]
        return self._task(f'{label}: {title}', work, name, key)

    # ------------------------------------------------------------ player compatibility

    def needs_prepare(self, key):
        path = LIBRARY.path(key)
        try:
            return bool(media.kind(path) == 'video' and media.needs_prepare(path))
        except Exception:
            return False

    def prepare_playback(self, key):
        """Makes a copy the built-in player can play (cached). Returns a task id; the result is the file's /file/ path."""
        path = LIBRARY.path(key)

        def work(p):
            try:
                media.extract_text_subs(path)
            except Exception:
                pass
            return media.playable_copy(path, p).replace('\\', '/')
        return self._task(f'تجهيز للتشغيل: {os.path.basename(path)[:40]}', work, 'prepare', key)

    def task_status(self, task_id):
        return self.tasks.get(task_id)

    # ------------------------------------------------------------ browser + voice

    def browse(self, text):
        from . import browser
        return browser.open_window(self, text)

    def browse_history(self):
        from . import browser
        return browser.BrowserApi(self).history()

    def browse_history_clear(self):
        from . import browser
        browser.BrowserApi(self).clear_history()

    def voice(self, data, mime):
        """Speech (recorded in the page) → text, through Gemini."""
        try:
            return {'text': gemini.transcribe_audio(data, mime)}
        except gemini.GeminiError as e:
            return {'error': e.friendly() if e.code else e.message}
        except Exception as e:
            return {'error': str(e)[:200]}

    def _extract(self, path, password):
        result = extract(path, password)
        if result.get('failed'):
            raise RuntimeError(('🔒 ' if result.get('need_password') else '') + result.get('error', 'فشل'))
        return f"{result.get('count', 0)} ملف"

    def montage(self, keys, music=None, music_volume=0.6, clip_volume=1.0):
        out = media.unique(os.path.join(settings().downloads, f'مونتاج {time.strftime("%Y-%m-%d %H.%M")}.mp4'))
        clips = [LIBRARY.path(k) for k in keys]
        song = LIBRARY.path(music) if music else None
        return self._task('مونتاج', lambda p: LIBRARY.key(media.montage(clips, out, song, music_volume, clip_volume, p)))

    def ipa(self, key):
        icon_dir = os.path.join(cache_dir(), 'icons')
        os.makedirs(icon_dir, exist_ok=True)
        return ipa_info(LIBRARY.path(key), icon_dir)

    # ------------------------------------------------------------ Stremio

    def stremio_target(self, page):
        found = subs.target_from_page(page)
        return list(found) if found else None

    def stremio_episodes(self, series_id):
        return subs.episodes(series_id)

    def stremio_download(self, kind, vid, quality=None):
        if not subs.addon_base():
            return 'حط رابط إضافة ستريميو من الإعدادات أول'
        found = subs.streams(kind, vid)
        if not found:
            return 'الإضافة ما عطت روابط مباشرة (تحتاج Real-Debrid أو خدمة مشابهة مفعّلة فيها)'
        pick = subs.best(found, quality or settings().get('stremio_quality') or '1080p')
        manager().add(pick['url'], 'video', 'best', referer=subs.page_for(kind, vid), as_file=True)
        return None

    def stremio_test(self):
        return subs.addon_name()

    # ------------------------------------------------------------ subscriptions

    def subscriptions(self):
        return SUBSCRIPTIONS.items

    def subscribe(self, url, mode='video', latest=0):
        return SUBSCRIPTIONS.add(url.strip(), mode, int(latest or 0))

    def unsubscribe(self, url):
        SUBSCRIPTIONS.remove(url)

    def check_subscriptions(self):
        return SUBSCRIPTIONS.check_all()

    # ------------------------------------------------------------ engine

    def update_engine(self):
        return engine.api_update_engine({})

    # ------------------------------------------------------------ assistant

    def chat_history(self):
        return self.chat.data

    def chat_clear(self):
        self.chat.data = []
        self.chat.save()

    def test_key(self, key):
        models = gemini.available_models(key.strip())
        return {'ok': bool(models), 'count': len(models), 'models': gemini.FALLBACK if models else []}

    def ask(self, text, attachment=None):
        """One question (with an optional file); actions run in the app. Returns the reply message."""
        text = (text or '').strip()
        history = []
        for m in self.chat.data[-30:]:
            if m.get('error') or not m.get('text'):
                continue
            history.append({'role': m['role'], 'parts': [{'text': m['text']}]})
        self.chat.data.append({'role': 'user', 'text': text, 'file': os.path.basename(attachment) if attachment else None})
        parts = []
        wants_action = not attachment and any(w in text.lower() for w in ACTION_WORDS)
        low = False
        try:
            if attachment:
                parts.append(gemini.file_part(attachment, gemini._key()))
            yt = re.search(r'https?://(www\.|m\.)?(youtube\.com/(watch\?v=|shorts/)|youtu\.be/)\S+', text)
            if yt and not wants_action:
                parts.append({'file_data': {'file_uri': yt.group(0), 'mime_type': 'video/mp4'}})
                low = True
            parts.append({'text': text or 'وش هذا؟ اشرحه لي.'})
            contents = history + [{'role': 'user', 'parts': parts}]
            reply = {'role': 'model', 'text': '', 'actions': [], 'sources': [], 'play': None}
            for _ in range(4):
                answer = gemini.generate(contents, search=settings().get('gemini_search') and not wants_action,
                                         functions=ACTIONS if wants_action else None, low_media=low)
                reply['text'] += answer['text']
                reply['sources'] = answer['sources'] or reply['sources']
                if not answer['calls']:
                    break
                responses = []
                for part in answer['calls']:
                    call = part['functionCall']
                    summary, response, play = self._action(call.get('name'), call.get('args') or {})
                    reply['actions'].append(summary)
                    if play:
                        reply['play'] = play
                    responses.append({'functionResponse': {'name': call.get('name'), 'response': response}})
                contents = contents + [{'role': 'model', 'parts': answer['parts']}, {'role': 'user', 'parts': responses}]
        except gemini.GeminiError as e:
            reply = {'role': 'model', 'text': e.friendly() if e.code else e.message, 'error': True}
        except Exception as e:
            reply = {'role': 'model', 'text': f'صار خطأ: {e}', 'error': True}
        self.chat.data.append(reply)
        self.chat.data = self.chat.data[-200:]
        self.chat.save()
        return reply

    def _action(self, name, args):
        kind = args.get('kind') or 'video'
        url = (args.get('url') or '').strip()
        if name == 'download_media':
            manager().add(url, kind)
            return f'بدأ التحميل ({kind})', {'ok': True}, None
        if name == 'download_latest_from_channel':
            count = max(1, min(30, int(args.get('count') or 5)))
            data = engine.api_list({'url': url, 'limit': count})
            entries = [e['url'] for e in data.get('entries') or []][:count]
            if not entries:
                return 'ما قدرت أقرأ القناة', {'ok': False, 'error': data.get('error') or 'not a channel'}, None
            for link in reversed(entries):
                manager().add(link, kind)
            return f'بدأ تحميل {len(entries)} من {data.get("title") or "القناة"}', {'ok': True, 'count': len(entries)}, None
        if name == 'follow_channel':
            error = SUBSCRIPTIONS.add(url, kind)
            return (error or 'صرت تتابع القناة'), {'ok': not error, 'error': error}, None
        words = [w for w in (args.get('query') or '').lower().split() if len(w) > 1]
        items = LIBRARY.all_items()
        if words:
            scored = [(sum(w in (i['title'] + ' ' + i['name']).lower() for w in words), i) for i in items]
            items = [i for score, i in sorted(scored, key=lambda x: -x[0]) if score]
        if name == 'search_library':
            return f'لقيت {len(items)} ملف', {'files': [{'title': i['title'], 'kind': i['kind']} for i in items[:20]]}, None
        if name == 'play_from_library':
            item = next((i for i in items if i['kind'] in ('video', 'audio')), None)
            if not item:
                return 'ما لقيت الملف', {'ok': False}, None
            return f'يشتغل: {item["title"]}', {'ok': True}, item['key']
        return '', {'ok': False}, None
