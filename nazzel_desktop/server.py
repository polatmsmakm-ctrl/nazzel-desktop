"""A small local web server: the interface, and the library's files for the player (with Range,
so long films seek instantly), covers and subtitles (as WebVTT)."""
import http.server
import mimetypes
import os
import re
import socketserver
import threading
import urllib.parse

from .core import media
from .core.library import LIBRARY
from .core.settings import resource

UI = resource('ui')
mimetypes.add_type('video/mp4', '.mp4')
mimetypes.add_type('video/webm', '.webm')
mimetypes.add_type('video/x-matroska', '.mkv')
mimetypes.add_type('audio/mp4', '.m4a')
mimetypes.add_type('text/vtt', '.vtt')
mimetypes.add_type('application/javascript', '.js')


class Handler(http.server.BaseHTTPRequestHandler):
    on_show = None
    protocol_version = 'HTTP/1.1'
    # set only for tests / --browser: lets a normal browser call the API over HTTP
    dev_api = None

    def do_POST(self):
        if not self.dev_api or not self.path.startswith('/__api/'):
            return self.send_error(404)
        import json
        name = self.path[len('/__api/'):]
        length = int(self.headers.get('Content-Length') or 0)
        args = json.loads(self.rfile.read(length) or b'[]')
        try:
            result = getattr(self.dev_api, name)(*args)
            body = json.dumps(result, ensure_ascii=False, default=str).encode()
            self.send_response(200)
        except Exception as e:
            body = json.dumps({'error': str(e)}).encode()
            self.send_response(500)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass

    def do_HEAD(self):
        self.do_GET(head=True)

    def do_GET(self, head=False):
        parsed = urllib.parse.urlsplit(self.path)
        path = urllib.parse.unquote(parsed.path)
        query = urllib.parse.parse_qs(parsed.query)
        try:
            if path == '/__show':          # a second launch asks the running copy to show its window
                if Handler.on_show:
                    Handler.on_show()
                self.send_response(200)
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            if path.startswith('/media/'):
                return self.send_file(LIBRARY.path(path[len('/media/'):]), head)
            if path.startswith('/cover/'):
                cover = LIBRARY.cover(path[len('/cover/'):])
                if not cover:
                    return self.send_error(404)
                return self.send_file(cover, head, cache=True)
            if path.startswith('/sub/'):
                return self.send_subtitle(LIBRARY.path(path[len('/sub/'):]), float((query.get('delay') or ['0'])[0]))
            if path.startswith('/file/'):        # icons extracted from IPAs etc. (the app's cache folder only)
                from .core.settings import cache_dir
                target = os.path.realpath(path[len('/file/'):])
                if not target.startswith(os.path.realpath(cache_dir())):
                    return self.send_error(403)
                return self.send_file(target, head, cache=True)
            target = os.path.normpath(os.path.join(UI, path.lstrip('/') or 'index.html'))
            if not target.startswith(UI) or not os.path.isfile(target):
                target = os.path.join(UI, 'index.html')
            return self.send_file(target, head)
        except (ValueError, OSError):
            return self.send_error(404)

    def send_file(self, path, head=False, cache=False):
        if not os.path.isfile(path):
            return self.send_error(404)
        size = os.path.getsize(path)
        start, end = 0, size - 1
        status = 200
        match = re.match(r'bytes=(\d*)-(\d*)', self.headers.get('Range') or '')
        if match and size:
            if match.group(1):
                start = int(match.group(1))
                end = int(match.group(2)) if match.group(2) else size - 1
            elif match.group(2):
                start = max(0, size - int(match.group(2)))
            end = min(end, size - 1)
            status = 206
        length = max(0, end - start + 1)
        self.send_response(status)
        mime = mimetypes.guess_type(path)[0] or 'application/octet-stream'
        if media.ext(path) == 'mkv':
            mime = 'video/webm'      # Chromium plays most MKVs when told they are WebM
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(length))
        self.send_header('Accept-Ranges', 'bytes')
        if status == 206:
            self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
        self.send_header('Cache-Control', 'max-age=86400' if cache else 'no-cache')
        self.end_headers()
        if head:
            return
        with open(path, 'rb') as fh:
            fh.seek(start)
            remaining = length
            while remaining > 0:
                chunk = fh.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    return
                remaining -= len(chunk)

    def send_subtitle(self, path, delay):
        with open(path, 'rb') as fh:
            raw = fh.read()
        from .core.subs import decode
        text = decode(raw)
        if media.ext(path) == 'ass':
            text = ass_to_srt(text)
        if delay:
            def shift(m):
                total = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3)) + int(m.group(4)) / 1000 + delay
                total = max(0.0, total)
                ms = int(round(total * 1000))
                return f'{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d}.{ms % 1000:03d}'
            text = re.sub(r'(\d{1,2}):(\d{2}):(\d{2})[,.](\d{3})', shift, text)
        body = (text if text.lstrip('﻿').startswith('WEBVTT') else media.srt_to_vtt(text)).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'text/vtt; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def ass_to_srt(text):
    out, n = [], 0
    for line in text.splitlines():
        if not line.startswith('Dialogue:'):
            continue
        fields = line.split(',', 9)
        if len(fields) < 10:
            continue
        def t(v):
            h, m, s = v.strip().split(':')
            return f'{int(h):02d}:{int(m):02d}:{float(s):06.3f}'.replace('.', ',')
        body = re.sub(r'\{[^}]*\}', '', fields[9]).replace('\\N', '\n').replace('\\n', '\n')
        n += 1
        out.append(f'{n}\n{t(fields[1])} --> {t(fields[2])}\n{body}\n')
    return '\n'.join(out)


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def start(dev_api=None, port=0):
    Handler.dev_api = dev_api
    server = Server(('127.0.0.1', port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f'http://127.0.0.1:{server.server_address[1]}'
