"""Updates itself: looks at the latest release on GitHub, downloads the installer and runs it quietly
(the installer closes this copy and opens the new one). Portable copies just open the release page."""
import json
import os
import subprocess
import sys
import tempfile
import urllib.request

from .core.settings import resource

REPO = 'polatmsmakm-ctrl/nazzel-desktop'
RELEASES = f'https://github.com/{REPO}/releases/latest'


def current_build():
    try:
        with open(resource('build.txt'), encoding='utf-8') as fh:
            return int(fh.read().strip() or 0)
    except (OSError, ValueError):
        return 0


def installed():
    """True for the installed copy (the installer's uninstaller sits next to the exe)."""
    if not getattr(sys, 'frozen', False):
        return False
    folder = os.path.dirname(sys.executable)
    return any(name.lower().startswith('unins') and name.lower().endswith('.exe') for name in os.listdir(folder))


def check():
    """{'available', 'build', 'current', 'url', 'installed'} (or {'error'})."""
    request = urllib.request.Request(f'https://api.github.com/repos/{REPO}/releases/latest',
                                     headers={'User-Agent': 'Nazzel-desktop', 'Accept': 'application/vnd.github+json'})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            data = json.loads(response.read().decode())
    except Exception as e:
        return {'error': str(e)[:160]}
    tag = data.get('tag_name') or ''
    try:
        build = int(tag.rsplit('.', 1)[-1])
    except ValueError:
        build = 0
    asset = next((a for a in data.get('assets') or [] if a.get('name') == 'Nazzel-Setup.exe'), None)
    current = current_build()
    return {'available': bool(current and build > current), 'build': build, 'current': current,
            'url': asset.get('browser_download_url') if asset else None, 'installed': installed(),
            'page': data.get('html_url') or RELEASES}


def download_and_install(url, progress=None):
    """Downloads the installer and starts it quietly. The caller then quits the app."""
    target = os.path.join(tempfile.gettempdir(), 'Nazzel-Setup-update.exe')
    request = urllib.request.Request(url, headers={'User-Agent': 'Nazzel-desktop'})
    with urllib.request.urlopen(request, timeout=60) as response, open(target, 'wb') as fh:
        total = int(response.headers.get('Content-Length') or 0)
        done = 0
        while True:
            chunk = response.read(1 << 20)
            if not chunk:
                break
            fh.write(chunk)
            done += len(chunk)
            if progress and total:
                progress(done / total)
    flags = 0x00000008 | 0x00000200 if os.name == 'nt' else 0      # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    subprocess.Popen([target, '/SILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/CLOSEAPPLICATIONS'],
                     creationflags=flags, close_fds=True)
    return target
