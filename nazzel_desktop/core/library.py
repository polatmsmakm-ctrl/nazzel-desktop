"""The downloads folder as a library: files, titles / channels / covers, watch progress, playlists."""
import os
import shutil
import time
import uuid

from . import media
from .settings import settings, data_dir, JsonStore

SUB_EXTS = {'srt', 'vtt', 'ass'}


class Library:
    def __init__(self):
        self.meta = JsonStore('library.json', {})
        self.history = JsonStore('history.json', {})
        self.lists = JsonStore('playlists.json', [])
        self.covers = os.path.join(data_dir(), 'covers')
        os.makedirs(self.covers, exist_ok=True)

    @property
    def root(self):
        return settings().downloads

    def key(self, path):
        return os.path.relpath(os.path.abspath(path), self.root).replace('\\', '/')

    def path(self, key):
        full = os.path.abspath(os.path.join(self.root, key))
        if not full.startswith(os.path.abspath(self.root)):
            raise ValueError('outside the library')
        return full

    def set_meta(self, path, title=None, uploader=None, source=None, cover=None):
        entry = self.meta.data.get(self.key(path), {})
        entry.update({k: v for k, v in {'title': title, 'uploader': uploader, 'source': source}.items() if v})
        entry.setdefault('added', time.time())
        if cover and os.path.exists(cover):
            target = os.path.join(self.covers, uuid.uuid4().hex + '.' + (media.ext(cover) or 'jpg'))
            shutil.copy(cover, target)
            entry['cover'] = target
        self.meta.data[self.key(path)] = entry
        self.meta.save()

    def subtitles(self, path):
        """Sidecar subtitles: "<name>.<lang>.srt" next to the video."""
        folder, name = os.path.split(path)
        stem = os.path.splitext(name)[0]
        out = []
        try:
            names = sorted(os.listdir(folder))
        except OSError:
            return out
        for n in names:
            if n.startswith(stem + '.') and media.ext(n) in SUB_EXTS:
                lang = n[len(stem) + 1:].rsplit('.', 1)[0] or 'sub'
                out.append({'lang': lang, 'file': self.key(os.path.join(folder, n))})
        rank = lambda t: (0 if t['lang'].startswith('ar') else 1 if t['lang'].startswith('en') else 2, t['lang'])
        return sorted(out, key=rank)

    def listing(self, folder=''):
        base = self.path(folder) if folder else self.root
        folders, items = [], []
        try:
            entries = list(os.scandir(base))
        except OSError:
            entries = []
        for e in entries:
            if e.name.startswith('.') or e.name.endswith(('.part', '.ytdl', '.nzstate')):
                continue
            if e.is_dir():
                try:
                    count = sum(1 for x in os.scandir(e.path) if not x.name.startswith('.') and media.ext(x.name) not in SUB_EXTS)
                except OSError:
                    count = 0
                folders.append({'name': e.name, 'key': self.key(e.path), 'count': count})
                continue
            if media.ext(e.name) in SUB_EXTS:
                continue
            items.append(self.item(e.path, e.stat()))
        items.sort(key=lambda i: i['added'], reverse=True)
        folders.sort(key=lambda f: f['name'].lower())
        return {'folders': folders, 'items': items, 'folder': folder}

    def item(self, path, stat=None):
        stat = stat or os.stat(path)
        key = self.key(path)
        m = self.meta.data.get(key, {})
        h = self.history.data.get(key)
        return {
            'key': key, 'name': os.path.basename(path), 'title': m.get('title') or os.path.splitext(os.path.basename(path))[0],
            'uploader': m.get('uploader'), 'source': m.get('source'), 'size': stat.st_size,
            'added': m.get('added') or stat.st_mtime, 'kind': media.kind(path), 'ext': media.ext(path),
            'playable': media.ext(path) in media.BROWSER_PLAYABLE, 'cover': bool(m.get('cover')),
            'subtitles': len(self.subtitles(path)) if media.kind(path) == 'video' else 0,
            'progress': (h['position'] / h['duration']) if h and h.get('duration') else 0,
        }

    def all_items(self):
        out = []
        for folder, _, names in os.walk(self.root):
            for n in names:
                if n.startswith('.') or media.ext(n) in SUB_EXTS or n.endswith(('.part', '.ytdl', '.nzstate')):
                    continue
                try:
                    out.append(self.item(os.path.join(folder, n)))
                except OSError:
                    pass
        return sorted(out, key=lambda i: i['added'], reverse=True)

    def cover(self, key):
        m = self.meta.data.get(key, {})
        if m.get('cover') and os.path.exists(m['cover']):
            return m['cover']
        path = self.path(key)
        return media.thumbnail(path) if os.path.exists(path) else None

    # --- files

    def rename(self, key, name):
        src = self.path(key)
        name = name.strip().replace('/', '_').replace('\\', '_')
        if not name:
            return key
        dest = media.unique(os.path.join(os.path.dirname(src), name + ('.' + media.ext(src) if media.ext(src) else '')))
        stem_old, stem_new = os.path.splitext(src)[0], os.path.splitext(dest)[0]
        for track in self.subtitles(src):
            old = self.path(track['file'])
            shutil.move(old, stem_new + old[len(stem_old):])
        shutil.move(src, dest)
        self._move_meta(key, self.key(dest))
        return self.key(dest)

    def delete(self, key):
        path = self.path(key)
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
        else:
            for track in self.subtitles(path):
                try:
                    os.remove(self.path(track['file']))
                except OSError:
                    pass
            try:
                os.remove(path)
            except OSError:
                pass
        self.meta.data.pop(key, None)
        self.meta.save()

    def move(self, key, folder_key):
        src = self.path(key)
        target_dir = self.path(folder_key) if folder_key else self.root
        dest = media.unique(os.path.join(target_dir, os.path.basename(src)))
        stem_old, stem_new = os.path.splitext(src)[0], os.path.splitext(dest)[0]
        for track in self.subtitles(src):
            old = self.path(track['file'])
            shutil.move(old, stem_new + old[len(stem_old):])
        shutil.move(src, dest)
        self._move_meta(key, self.key(dest))
        return self.key(dest)

    def new_folder(self, name, parent=''):
        path = media.unique(os.path.join(self.path(parent) if parent else self.root, name.strip() or 'مجلد جديد'))
        os.makedirs(path, exist_ok=True)
        return self.key(path)

    def _move_meta(self, old, new):
        if old in self.meta.data:
            self.meta.data[new] = self.meta.data.pop(old)
            self.meta.save()
        if old in self.history.data:
            self.history.data[new] = self.history.data.pop(old)
            self.history.save()

    # --- continue watching

    def record(self, key, position, duration):
        if duration < 120:
            return
        if position / duration > 0.95:
            self.history.data.pop(key, None)
        else:
            self.history.data[key] = {'position': position, 'duration': duration, 'date': time.time()}
        self.history.save()

    def position(self, key):
        h = self.history.data.get(key)
        return h['position'] if h else 0

    def continue_watching(self):
        out = []
        for key, h in sorted(self.history.data.items(), key=lambda kv: kv[1]['date'], reverse=True)[:20]:
            try:
                if os.path.exists(self.path(key)) and 0.02 < h['position'] / h['duration'] < 0.95:
                    item = self.item(self.path(key))
                    item['remaining'] = h['duration'] - h['position']
                    out.append(item)
            except (OSError, ValueError, ZeroDivisionError):
                pass
        return out

    # --- playlists

    def playlists(self):
        return [{'id': p['id'], 'name': p['name'], 'count': len(p['items'])} for p in self.lists.data]

    def playlist(self, list_id):
        p = next((x for x in self.lists.data if x['id'] == list_id), None)
        if not p:
            return None
        items = []
        for key in p['items']:
            try:
                items.append(self.item(self.path(key)))
            except (OSError, ValueError):
                pass
        return {'id': p['id'], 'name': p['name'], 'items': items}

    def playlist_create(self, name):
        p = {'id': uuid.uuid4().hex[:10], 'name': name.strip() or 'قائمة جديدة', 'items': []}
        self.lists.data.insert(0, p)
        self.lists.save()
        return p['id']

    def playlist_add(self, list_id, key):
        for p in self.lists.data:
            if p['id'] == list_id and key not in p['items']:
                p['items'].append(key)
        self.lists.save()

    def playlist_set(self, list_id, keys):
        for p in self.lists.data:
            if p['id'] == list_id:
                p['items'] = keys
        self.lists.save()

    def playlist_delete(self, list_id):
        self.lists.data = [p for p in self.lists.data if p['id'] != list_id]
        self.lists.save()


LIBRARY = Library()
