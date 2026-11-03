"""Running in the background: an icon in the tray (open / check followed channels / quit), start with Windows,
and one running copy only (a second launch just shows the first window)."""
import os
import sys
import threading
import urllib.request

from .core.settings import data_dir, resource

RUN_KEY = r'Software\Microsoft\Windows\CurrentVersion\Run'


def startup_command():
    """The command Windows should run at sign-in (only for the installed / portable exe)."""
    if getattr(sys, 'frozen', False):
        return f'"{sys.executable}" --background'
    return None


def set_startup(on):
    if os.name != 'nt':
        return False
    import winreg
    command = startup_command()
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            if on and command:
                winreg.SetValueEx(key, 'Nazzel', 0, winreg.REG_SZ, command)
            else:
                try:
                    winreg.DeleteValue(key, 'Nazzel')
                except FileNotFoundError:
                    pass
        return bool(on and command) or not on
    except OSError:
        return False


def is_startup():
    if os.name != 'nt':
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, 'Nazzel')
            return True
    except OSError:
        return False


# ---- one copy only: the running copy writes its port; a new launch asks it to show its window
def _port_file():
    return os.path.join(data_dir(), 'port.txt')


def remember_port(url, token=''):
    try:
        with open(_port_file(), 'w') as fh:
            fh.write(url + '\n' + token)
    except OSError:
        pass


def _running():
    try:
        with open(_port_file()) as fh:
            lines = fh.read().split('\n')
        return lines[0].strip(), (lines[1].strip() if len(lines) > 1 else '')
    except OSError:
        return '', ''


def show_running_copy():
    """True when another copy answered (and shows its window): this one should quit."""
    url, _ = _running()
    if not url:
        return False
    try:
        with urllib.request.urlopen(url + '/__show', timeout=1.5) as response:
            return response.status == 200
    except Exception:
        return False


def send_to_running_copy(link, mode):
    """Hands a link (from the browser extension) to the copy that is already open. True when it took it."""
    import urllib.parse
    url, token = _running()
    if not url or not token:
        return False
    query = urllib.parse.urlencode({'u': link, 'm': mode or 'video', 'k': token})
    try:
        with urllib.request.urlopen(f'{url}/__add?{query}', timeout=3) as response:
            return response.status == 200
    except Exception:
        return False


def parse_protocol(argv):
    """nazzel://download?url=…&mode=… on the command line -> (link, mode) or None."""
    import urllib.parse
    for arg in argv[1:]:
        if arg.lower().startswith('nazzel:'):
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(arg).query)
            link = (query.get('url') or [''])[0]
            if link.startswith(('http://', 'https://')):
                return link, (query.get('mode') or ['video'])[0]
    return None


def register_protocol():
    """Makes nazzel:// links (the browser extension) open this app. Per user, no admin rights."""
    if os.name != 'nt' or not getattr(sys, 'frozen', False):
        return
    import winreg
    command = f'"{sys.executable}" "%1"'
    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r'Software\Classes\nazzel') as key:
            winreg.SetValueEx(key, '', 0, winreg.REG_SZ, 'URL:Nazzel')
            winreg.SetValueEx(key, 'URL Protocol', 0, winreg.REG_SZ, '')
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r'Software\Classes\nazzel\shell\open\command') as key:
            winreg.SetValueEx(key, '', 0, winreg.REG_SZ, command)
    except OSError:
        pass


# ---- the tray icon
def start_tray(window, on_check, on_quit):
    """Starts the icon in its own thread. Returns the icon (None when the tray is not available)."""
    try:
        import pystray
        from PIL import Image
    except ImportError:
        return None

    def show(*_):
        try:
            window.show()
            window.restore()
        except Exception:
            pass

    menu = pystray.Menu(
        pystray.MenuItem('افتح نزّل', show, default=True),
        pystray.MenuItem('شيّك على المتابعات الحين', lambda *_: threading.Thread(target=on_check, daemon=True).start()),
        pystray.MenuItem('خروج', lambda icon, *_: on_quit(icon)),
    )
    try:
        image = Image.open(resource('ui', 'icon.png'))
        icon = pystray.Icon('Nazzel', image, 'نزّل', menu)
        icon.run_detached()
        return icon
    except Exception:
        return None
