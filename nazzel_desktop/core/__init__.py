"""Nazzel desktop: the same download engine as the iPhone app, with ffmpeg."""
import os

from . import engine
from .settings import settings, cache_dir, data_dir, tool, resource

_ready = False


def boot():
    """Points the shared engine at the desktop folders, and puts ffmpeg / deno on PATH for yt-dlp."""
    global _ready
    if _ready:
        return
    tools = resource('tools')
    if os.path.isdir(tools):
        os.environ['PATH'] = tools + os.pathsep + os.environ.get('PATH', '')
    # the iPhone-only JavaScript plugin is not wanted on Windows
    engine.UPDATABLE_PACKAGES = [p for p in engine.UPDATABLE_PACKAGES if 'apple' not in p]
    engine.api_init({'documents': settings().downloads, 'caches': cache_dir(),
                     'engine_dir': os.path.join(data_dir(), 'engine')})
    _ready = True
