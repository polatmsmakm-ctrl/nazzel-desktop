"""Archives (zip / tar / 7z), followed channels, IPA details."""
import os
import threading
import time

from . import engine, media
from .settings import settings, JsonStore


def extract(path, password=None):
    """Unpacks next to the archive. 7z through py7zr, the rest through the shared engine."""
    if media.ext(path) == '7z':
        try:
            import py7zr
        except ImportError:
            return {'failed': True, 'error': 'فك 7z مو متوفر'}
        stem = os.path.splitext(os.path.basename(path))[0]
        dest = media.unique(os.path.join(os.path.dirname(path), stem))
        try:
            with py7zr.SevenZipFile(path, 'r', password=password or None) as archive:
                if archive.needs_password() and not password:
                    return {'failed': True, 'need_password': True, 'error': 'الملف محمي بكلمة سر'}
                archive.extractall(dest)
        except py7zr.exceptions.PasswordRequired:
            return {'failed': True, 'need_password': True, 'error': 'الملف محمي بكلمة سر'}
        except Exception as e:
            return {'failed': True, 'error': engine._clean_error(str(e))}
        count = sum(len(files) for _, _, files in os.walk(dest))
        return {'folder': dest, 'count': count}
    result = engine.api_extract({'path': path, 'password': password})
    return result


def ipa_info(path, icon_dir):
    return engine.api_ipa_info({'path': path, 'icon_dir': icon_dir})


class Subscriptions:
    INTERVAL = 30 * 60

    def __init__(self):
        self.store = JsonStore('subscriptions.json', [])
        self.checking = False

    @property
    def items(self):
        return self.store.data

    def _list(self, url, limit=25):
        data = engine.api_list({'url': url, 'limit': limit})
        if data.get('failed'):
            return None, None, data.get('error')
        if not data.get('playlist'):
            return None, None, 'هذا الرابط مقطع واحد، مو قناة أو حساب'
        return [e['url'] for e in data.get('entries') or []], data.get('title'), None

    def add(self, url, mode='video', latest=0):
        from .downloads import manager
        if any(s['url'] == url for s in self.items):
            return 'تتابعه من قبل'
        entries, title, error = self._list(url)
        if entries is None:
            return error or 'ما قدرت أقرأ القناة'
        self.items.insert(0, {'url': url, 'title': title or url, 'mode': mode, 'seen': entries,
                              'last_check': time.time(), 'new': 0, 'error': None})
        self.store.save()
        for link in list(reversed(entries[:latest])):
            manager().add(link, mode)
        return None

    def remove(self, url):
        self.store.data = [s for s in self.items if s['url'] != url]
        self.store.save()

    def check_all(self):
        from .downloads import manager
        if self.checking:
            return 0
        self.checking = True
        added = 0
        try:
            for sub in self.items:
                entries, _, error = self._list(sub['url'])
                sub['last_check'] = time.time()
                sub['error'] = error
                if entries is None:
                    continue
                fresh = [e for e in entries if e not in sub['seen']]
                for link in reversed(fresh[:10]):
                    manager().add(link, sub.get('mode') or 'video')
                sub['new'] = sub.get('new', 0) + len(fresh)
                sub['seen'] = (fresh + sub['seen'])[:600]
                added += len(fresh)
            self.store.save()
        finally:
            self.checking = False
        return added

    def start_timer(self, notify):
        def loop():
            time.sleep(20)
            while True:
                if self.items:
                    count = self.check_all()
                    if count:
                        notify(f'بدأ تحميل {count} مقطع جديد من القنوات اللي تتابعها')
                time.sleep(self.INTERVAL)
        threading.Thread(target=loop, daemon=True).start()


SUBSCRIPTIONS = Subscriptions()
