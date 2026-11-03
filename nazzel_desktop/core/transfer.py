"""Phone <-> PC over Wi-Fi. The PC shows a QR code; the phone opens it (the camera app, or Nazzel's scanner):
 - receive: a page where the phone picks files and uploads them into the downloads folder
 - send:    a page (and a list for the Nazzel app) with the chosen files to download
Only while the window is open, only on the local network, behind a random code."""
import json
import os
import re
import secrets
import socket
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .settings import settings

MAX_UPLOAD = 20 * 1024 ** 3          # 20 GB
LIFETIME = 30 * 60                   # the code stops working after 30 minutes


def lan_ip():
    """This computer's address on the local network."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(('10.255.255.255', 1))
        return probe.getsockname()[0]
    except OSError:
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return '127.0.0.1'
    finally:
        probe.close()


def qr_svg(text):
    try:
        import segno
        return segno.make(text, error='m').svg_inline(scale=7, border=2, dark='#0B0F10', light='#ffffff')
    except Exception:
        return ''


def _safe_name(name):
    name = urllib.parse.unquote(name or '').replace('\\', '/').split('/')[-1]
    name = re.sub(r'[\x00-\x1f<>:"|?*]', '_', name).strip(' .')
    return name[:150] or 'file'


def _unique(folder, name):
    base, ext = os.path.splitext(name)
    path, n = os.path.join(folder, name), 2
    while os.path.exists(path):
        path = os.path.join(folder, f'{base} ({n}){ext}')
        n += 1
    return path


PAGE = """<!doctype html><html lang="ar" dir="rtl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>نزّل</title>
<style>body{margin:0;background:#0B0F10;color:#eef4f5;font:16px system-ui,Tahoma,sans-serif;padding:24px;text-align:center}
h1{margin:6px 0 4px}p{color:#98a8ad}.btn{display:block;margin:14px auto;padding:16px;border-radius:16px;border:0;background:#2ed3b7;color:#04201b;font:700 17px inherit;width:100%;max-width:420px}
.file{background:#141b1d;border-radius:14px;margin:8px auto;padding:12px;max-width:420px;text-align:right;overflow:hidden;text-overflow:ellipsis}
.bar{height:6px;background:#ffffff1f;border-radius:3px;margin-top:8px}.bar i{display:block;height:6px;background:#2ed3b7;border-radius:3px;width:0}
a.dl{color:#2ed3b7;text-decoration:none;font-weight:700}</style></head><body>
<h1>⬇ نزّل</h1><p id="sub"></p><div id="box"></div>
<script>
const T=location.pathname.split('/')[2];const box=document.getElementById('box'),sub=document.getElementById('sub');
fetch('/t/'+T+'/info').then(r=>r.json()).then(info=>{
 if(info.mode==='receive'){
  sub.textContent='اختار الملفات اللي تبي ترسلها للكمبيوتر';
  const inp=document.createElement('input');inp.type='file';inp.multiple=true;inp.style.display='none';
  const b=document.createElement('button');b.className='btn';b.textContent='اختر ملفات';b.onclick=()=>inp.click();
  box.append(b,inp);
  inp.onchange=()=>{for(const f of inp.files){
    const row=document.createElement('div');row.className='file';row.textContent=f.name;
    const bar=document.createElement('div');bar.className='bar';const fill=document.createElement('i');bar.append(fill);row.append(bar);box.append(row);
    const x=new XMLHttpRequest();x.open('PUT','/t/'+T+'/up?name='+encodeURIComponent(f.name));
    x.upload.onprogress=e=>{if(e.lengthComputable)fill.style.width=(100*e.loaded/e.total)+'%'};
    x.onload=()=>{row.append(x.status==200?' ✓ وصل':' ✗ فشل')};x.onerror=()=>row.append(' ✗ فشل');x.send(f);}};
 }else{
  sub.textContent='اضغط على الملف عشان ينزل على جوالك';
  info.files.forEach(f=>{const row=document.createElement('div');row.className='file';
    const a=document.createElement('a');a.className='dl';a.href=f.url;a.textContent='⬇ '+f.name;row.append(a);box.append(row);});
 }});
</script></body></html>"""


class Session:
    def __init__(self, mode, files, on_event):
        self.mode = mode
        self.token = secrets.token_urlsafe(9)
        self.files = files                      # [(path, name)]
        self.on_event = on_event
        self.received = []                      # names saved
        self.started = time.time()
        self.server = None
        self.port = 0

    @property
    def url(self):
        return f'http://{lan_ip()}:{self.port}/t/{self.token}/'

    def info(self):
        base = f'http://{lan_ip()}:{self.port}/t/{self.token}'
        return {'mode': self.mode, 'app': 'nazzel',
                'files': [{'name': n, 'size': os.path.getsize(p), 'url': f'{base}/get/{i}/{urllib.parse.quote(n)}'}
                          for i, (p, n) in enumerate(self.files)]}

    def alive(self):
        return time.time() - self.started < LIFETIME

    def stop(self):
        if self.server:
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            try:
                self.server.server_close()
            except OSError:
                pass
            self.server = None


def start(mode, files=None, on_event=None):
    """Starts a session. files: [(path, name)] for 'send'. Returns the Session."""
    session = Session(mode, list(files or []), on_event or (lambda *a: None))

    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, *args):
            pass

        def _json(self, data, status=200):
            body = json.dumps(data, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Connection', 'close')
            self.end_headers()
            self.wfile.write(body)

        def _route(self):
            parsed = urllib.parse.urlsplit(self.path)
            parts = [p for p in parsed.path.split('/') if p]
            if len(parts) < 2 or parts[0] != 't' or not secrets.compare_digest(parts[1], session.token) or not session.alive():
                self.send_error(404)
                return None
            return parts[2:], urllib.parse.parse_qs(parsed.query)

        def do_GET(self):
            route = self._route()
            if route is None:
                return
            rest, _ = route
            if not rest:
                body = PAGE.encode()
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Connection', 'close')
                self.end_headers()
                self.wfile.write(body)
            elif rest[0] == 'info':
                self._json(session.info())
            elif rest[0] == 'get' and len(rest) >= 2 and rest[1].isdigit() and int(rest[1]) < len(session.files):
                path, name = session.files[int(rest[1])]
                self._send_file(path, name)
            else:
                self.send_error(404)

        def _send_file(self, path, name):
            size = os.path.getsize(path)
            start, end, status = 0, size - 1, 200
            match = re.match(r'bytes=(\d*)-(\d*)', self.headers.get('Range') or '')
            if match and size:
                if match.group(1):
                    start = int(match.group(1))
                    end = int(match.group(2)) if match.group(2) else size - 1
                elif match.group(2):
                    start = max(0, size - int(match.group(2)))
                end, status = min(end, size - 1), 206
            length = max(0, end - start + 1)
            self.send_response(status)
            self.send_header('Content-Type', 'application/octet-stream')
            self.send_header('Content-Disposition', "attachment; filename*=UTF-8''" + urllib.parse.quote(name))
            self.send_header('Content-Length', str(length))
            self.send_header('Accept-Ranges', 'bytes')
            if status == 206:
                self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
            self.send_header('Connection', 'close')
            self.end_headers()
            with open(path, 'rb') as fh:
                fh.seek(start)
                left = length
                while left > 0:
                    chunk = fh.read(min(1 << 20, left))
                    if not chunk:
                        break
                    try:
                        self.wfile.write(chunk)
                    except OSError:
                        return
                    left -= len(chunk)

        def do_PUT(self):
            route = self._route()
            if route is None:
                return
            rest, query = route
            if session.mode != 'receive' or rest != ['up']:
                return self.send_error(403)
            length = int(self.headers.get('Content-Length') or 0)
            if length <= 0 or length > MAX_UPLOAD:
                return self.send_error(413)
            name = _safe_name((query.get('name') or [''])[0] or self.headers.get('X-Filename'))
            folder = settings().downloads
            os.makedirs(folder, exist_ok=True)
            target = _unique(folder, name)
            part = target + '.part'
            try:
                with open(part, 'wb') as fh:
                    left = length
                    while left > 0:
                        chunk = self.rfile.read(min(1 << 20, left))
                        if not chunk:
                            raise ConnectionError('closed')
                        fh.write(chunk)
                        left -= len(chunk)
                os.replace(part, target)
            except Exception:
                try:
                    os.remove(part)
                except OSError:
                    pass
                return self._json({'ok': False}, 500)
            session.received.append(os.path.basename(target))
            session.on_event('received', os.path.basename(target))
            self._json({'ok': True, 'name': os.path.basename(target)})

        do_POST = do_PUT

    session.server = ThreadingHTTPServer(('0.0.0.0', 0), Handler)
    session.server.daemon_threads = True
    session.port = session.server.server_address[1]
    threading.Thread(target=session.server.serve_forever, daemon=True).start()
    return session
