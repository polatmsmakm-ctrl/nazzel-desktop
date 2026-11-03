"""Settings, folders and small JSON stores of the desktop app."""
import json
import os
import sys
import threading

APP_NAME = 'Nazzel'
VERSION = '1.3.0'


def data_dir():
    """Private app data: %APPDATA%\\Nazzel on Windows."""
    base = os.environ.get('APPDATA') or os.path.join(os.path.expanduser('~'), '.config')
    path = os.path.join(base, APP_NAME)
    os.makedirs(path, exist_ok=True)
    return path


def cache_dir():
    base = os.environ.get('LOCALAPPDATA') or os.path.join(os.path.expanduser('~'), '.cache')
    path = os.path.join(base, APP_NAME, 'cache')
    os.makedirs(path, exist_ok=True)
    return path


def default_downloads():
    home = os.path.expanduser('~')
    for name in ('Downloads', 'التنزيلات'):
        if os.path.isdir(os.path.join(home, name)):
            return os.path.join(home, name, 'Nazzel')
    return os.path.join(home, 'Nazzel')


def resource(*parts):
    """A file shipped with the app (PyInstaller unpacks next to the exe / into _MEIPASS)."""
    base = getattr(sys, '_MEIPASS', os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(base, *parts)


def tool(name):
    """ffmpeg / ffprobe / deno shipped in tools/, else whatever is on PATH."""
    exe = name + ('.exe' if os.name == 'nt' else '')
    for folder in (resource('tools'), os.path.join(os.path.dirname(sys.executable), 'tools')):
        path = os.path.join(folder, exe)
        if os.path.exists(path):
            return path
    import shutil
    return shutil.which(name)


class JsonStore:
    """A dict saved as JSON in the data folder, safe to use from several threads."""

    def __init__(self, name, default=None):
        self.path = os.path.join(data_dir(), name)
        self.lock = threading.RLock()
        self.data = default if default is not None else {}
        try:
            with open(self.path, encoding='utf-8') as fh:
                self.data = json.load(fh)
        except (OSError, ValueError):
            pass

    def save(self):
        with self.lock:
            tmp = self.path + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as fh:
                json.dump(self.data, fh, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)


DEFAULTS = {
    'download_dir': None,
    'quality': 'best',          # best / 2160 / 1440 / 1080 / 720 / 480
    'mode': 'video',            # video / audio / photos
    'audio_format': 'mp3',      # mp3 / m4a
    'mp3_kbps': 256,
    'connections': 8,
    'parallel': 3,
    'subtitles': True,          # Arabic + English subtitles with videos
    'arabic_film_subs': True,   # every Arabic subtitle for films / episodes
    'watch_clipboard': True,    # offer to download links copied anywhere
    'gemini_key': '',
    'gemini_model': 'gemini-2.5-flash',
    'gemini_search': True,
    'stremio_addon': '',
    'stremio_quality': '1080p',
    'sub_font': 'system', 'sub_color': 'white', 'sub_background': 'box', 'sub_scale': 1.0, 'dual_subs': False, 'assistant_memory': True, 'close_to_tray': False, 'run_at_startup': False, 'sub_top': False,
}


class Settings(JsonStore):
    def __init__(self):
        super().__init__('settings.json', dict(DEFAULTS))
        for key, value in DEFAULTS.items():
            self.data.setdefault(key, value)
        if not self.data.get('download_dir'):
            self.data['download_dir'] = default_downloads()
        os.makedirs(self.data['download_dir'], exist_ok=True)

    def get(self, key):
        return self.data.get(key, DEFAULTS.get(key))

    def update(self, values):
        with self.lock:
            for key, value in values.items():
                if key in DEFAULTS:
                    self.data[key] = value
            if self.data.get('download_dir'):
                os.makedirs(self.data['download_dir'], exist_ok=True)
            self.save()
        return self.data

    @property
    def downloads(self):
        return self.data['download_dir']


SETTINGS = None


def settings():
    global SETTINGS
    if SETTINGS is None:
        SETTINGS = Settings()
    return SETTINGS
