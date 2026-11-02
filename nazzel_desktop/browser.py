"""The Nazzel browser: a window with a docked toolbar (back/forward/address/download menu), an ad guard and a home page of
popular sites, like the phone app. Logins stay saved (the web view keeps its own storage)."""
import urllib.parse

from .core.settings import JsonStore, resource


def normalize(text):
    """What was typed in the address bar -> a URL (or a Google search)."""
    text = (text or '').strip()
    if not text:
        return ''
    if '://' in text:
        return text
    if ' ' not in text and '.' in text:
        return 'https://' + text
    return 'https://www.google.com/search?q=' + urllib.parse.quote(text)


def guard_script():
    with open(resource('ui', 'guard.js'), encoding='utf-8') as fh:
        return fh.read()


class BrowserApi:
    """What the toolbar inside the browser window can call."""

    def __init__(self, main):
        self.main = main
        self.store = JsonStore('browser.json', {'history': []})

    # ---- address bar
    def go(self, text):
        url = normalize(text)
        if url:
            entries = [e for e in self.store.data.get('history', []) if e != text]
            self.store.data['history'] = ([text.strip()] + entries)[:40]
            self.store.save()
        return url

    def home(self):
        return self.main.base_url.rstrip('/') + '/browser-home.html'

    def history(self):
        return self.store.data.get('history', [])

    def clear_history(self):
        self.store.data['history'] = []
        self.store.save()

    # ---- downloads
    def grab(self, url, mode='video', as_file=False):
        from .core.downloads import manager
        ids = manager().add_many([url], mode, None) if not as_file else [manager().add(url, mode, 'best', as_file=True)]
        return len(ids)

    def active_count(self):
        from .core.downloads import manager
        return sum(1 for j in manager().snapshot() if j.get('phase') in ('queued', 'running'))

    def stremio(self, url):
        return self.main.stremio_target(url)

    def stremio_download(self, kind, vid):
        return self.main.stremio_download(kind, vid)

    def season(self, series_id):
        self._main_js(f'seasonDialog({series_id!r})')

    def summarize(self, url):
        self._main_js(f'askAssistant({("لخّص لي هالفيديو: " + url)!r})')

    # ---- play without downloading (no ads): the main window's player takes a direct address
    def play(self, url, audio_only=False):
        try:
            from .core import engine
            yt_dlp = engine._yt_dlp()
            fmt = 'bestaudio[protocol^=http]/bestaudio/best' if audio_only else \
                'best[protocol^=http][vcodec!=none][acodec!=none]/best[protocol^=http]/best'
            opts = {'quiet': True, 'no_warnings': True, 'noplaylist': True, 'format': fmt,
                    'remote_components': ['ejs:github']}
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
            direct = (info or {}).get('url') or next((f.get('url') for f in reversed((info or {}).get('formats') or []) if f.get('url')), None)
            if not direct:
                return 'ما قدرت أجيب رابط التشغيل. جرّب التحميل'
            if str(direct).split('?')[0].endswith('.m3u8'):
                return 'هالموقع يبث بصيغة ما يشغّلها المشغّل المدمج مباشرة. حمّله وشغّله'
            self._main_js(f'playExternal({direct!r}, {(info.get("title") or "")!r}, {bool(audio_only)!r})')
            return None
        except Exception as e:
            return ('تعذّر التشغيل: ' + str(e))[:140]

    def _main_js(self, code):
        window = getattr(self.main, 'window', None)
        if not window:
            return
        try:
            window.restore()
            window.show()
        except Exception:
            pass
        try:
            window.evaluate_js(code)
        except Exception:
            pass


def open_window(main, text=''):
    import webview
    api = BrowserApi(main)
    url = normalize(text) or api.home()
    window = webview.create_window('نزّل — المتصفح', url, js_api=api, width=1200, height=820, text_select=True,
                                   background_color='#0B0F10')
    window.events.loaded += lambda: _inject(window)
    return True


def _inject(window):
    try:
        window.evaluate_js(guard_script())
    except Exception:
        pass
