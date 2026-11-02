"""Arabic subtitles for films / episodes (the OpenSubtitles add-on Stremio uses, Cinemeta for the
IMDb id) and downloads through the user's Stremio stream add-on (Torrentio + Real-Debrid …)."""
import json
import os
import re
import urllib.parse
import urllib.request

from . import engine, media
from .settings import settings

SUBTITLES = 'https://opensubtitles-v3.strem.io'
CATALOG = 'https://v3-cinemeta.strem.io'


def _json(url, timeout=25):
    req = urllib.request.Request(url, headers={'User-Agent': 'Nazzel-Desktop/1.0'})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=engine._ssl_context()) as resp:
            return json.loads(resp.read().decode('utf-8', 'replace'))
    except Exception:
        return None


def target_from_page(page):
    """(kind, id) of the film / episode open on a Stremio page, else None."""
    if not page:
        return None
    text = page
    for _ in range(2):
        text = urllib.parse.unquote(text)
    if 'stremio' not in text.lower():
        return None
    found = re.findall(r'(movie|series)/(tt\d{5,10})(?:/(tt\d{5,10}(?::\d+:\d+)?))?', text)
    if not found:
        return None
    kind, root, video = found[-1]
    if kind == 'series' and ':' in video:
        return kind, video
    return kind, root


def guess(name):
    """{'title', 'year', 'season', 'episode'} from a file name, None for ordinary clips."""
    text = re.sub(r'[._]', ' ', name)
    text = re.sub(r'\[[^\]]*\]', ' ', text)
    cut = len(text)
    season = episode = year = None
    m = re.search(r'\b[Ss](\d{1,2})\s?[Ee](\d{1,3})\b', text)
    if m:
        season, episode = int(m.group(1)), int(m.group(2))
        cut = min(cut, m.start())
    m = re.search(r'[(\s]((?:19|20)\d{2})(?:[)\s]|$)', text)
    if m:
        year = int(m.group(1))
        cut = min(cut, m.start())
    if year is None and season is None:
        return None
    m = re.search(r'\b(2160p|1080p|720p|480p|4k|uhd|web-?dl|webrip|bluray|brrip|hdtv|x264|x265|hevc|hdr)\b', text, re.I)
    if m:
        cut = min(cut, m.start())
    title = re.sub(r'\s+', ' ', re.sub(r'[()\-–]+', ' ', text[:cut])).strip()
    if len(title) < 2:
        return None
    return {'title': title, 'year': year, 'season': season, 'episode': episode}


def look_up(g):
    kind = 'series' if g.get('season') else 'movie'
    data = _json(f"{CATALOG}/catalog/{kind}/top/search={urllib.parse.quote(g['title'])}.json")
    metas = (data or {}).get('metas') or []
    if not metas:
        return None
    wanted = g['title'].lower()

    def score(meta):
        name = (meta.get('name') or '').lower()
        points = 4 if name == wanted else 2 if (wanted in name or name in wanted) else 0
        if g.get('year') and str(meta.get('releaseInfo') or '').startswith(str(g['year'])):
            points += 3
        return points

    best = max(metas[:8], key=score)
    if score(best) == 0 and g.get('year'):
        return None
    root = best.get('id') or ''
    if not root.startswith('tt'):
        return None
    if kind == 'series':
        return kind, f"{root}:{g.get('season') or 1}:{g.get('episode') or 1}"
    return kind, root


def decode(data):
    for enc in ('utf-8-sig', 'utf-16'):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode('cp1256', 'replace')


def fetch(target, video, limit=12):
    """Every Arabic subtitle for target next to video (name.ar.srt, name.ar-2.srt …). Returns how many."""
    kind, vid = target
    data = _json(f"{SUBTITLES}/subtitles/{kind}/{urllib.parse.quote(vid, safe=':')}.json")
    entries = [s for s in (data or {}).get('subtitles') or [] if (s.get('lang') or '').lower() in ('ara', 'ar', 'arabic')]
    stem = os.path.splitext(video)[0]
    existing = sum(1 for n in os.listdir(os.path.dirname(video))
                   if n.startswith(os.path.basename(stem) + '.ar') and media.ext(n) == 'srt')
    seen, saved = set(), 0
    for entry in entries:
        if saved >= limit or not entry.get('url'):
            continue
        try:
            req = urllib.request.Request(entry['url'], headers={'User-Agent': 'Nazzel-Desktop/1.0'})
            with urllib.request.urlopen(req, timeout=25, context=engine._ssl_context()) as resp:
                raw = resp.read()
        except Exception:
            continue
        text = decode(raw)
        if len(raw) < 200 or text.count('-->') < 5 or hash(text) in seen:
            continue
        seen.add(hash(text))
        number = existing + saved + 1
        lang = 'ar' if number == 1 else f'ar-{number}'
        with open(f'{stem}.{lang}.srt', 'w', encoding='utf-8') as fh:
            fh.write(text)
        saved += 1
    return saved


def fetch_for(video, page=None):
    target = target_from_page(page)
    if not target:
        g = guess(os.path.splitext(os.path.basename(video))[0])
        target = look_up(g) if g else None
    return fetch(target, video) if target else 0


# ---------------------------------------------------------------- Stremio add-on

def addon_base():
    text = (settings().get('stremio_addon') or '').strip()
    if text.lower().startswith('stremio://'):
        text = 'https://' + text[len('stremio://'):]
    if text.endswith('/manifest.json'):
        text = text[:-len('/manifest.json')]
    text = text.rstrip('/')
    return text if text.lower().startswith('http') else None


def addon_name():
    base = addon_base()
    data = _json(base + '/manifest.json') if base else None
    return (data or {}).get('name')


def streams(kind, vid):
    base = addon_base()
    if not base:
        return []
    data = _json(f"{base}/stream/{kind}/{urllib.parse.quote(vid, safe=':')}.json", timeout=45)
    return [s for s in (data or {}).get('streams') or [] if str(s.get('url', '')).lower().startswith('http')]


def best(found, quality):
    def score(s):
        text = ((s.get('name') or '') + ' ' + (s.get('title') or s.get('description') or '')).lower()
        p = 0
        if quality != 'any' and quality in text:
            p += 100
        if quality == '1080p' and ('2160p' in text or '4k' in text):
            p -= 30
        if '+]' in text or 'cached' in text or '⚡' in text:
            p += 50
        if 'download]' in text or 'uncached' in text:
            p -= 60
        if ' cam' in text or 'telesync' in text:
            p -= 80
        return p
    ranked = sorted(enumerate(found), key=lambda pair: (-score(pair[1]), pair[0]))
    return ranked[0][1] if ranked else None


def page_for(kind, vid):
    root = vid.split(':')[0]
    return f'https://web.stremio.com/#/detail/{kind}/{root}/{vid}'


def episodes(series_id):
    root = series_id.split(':')[0]
    data = _json(f'{CATALOG}/meta/series/{root}.json')
    meta = (data or {}).get('meta') or {}
    out = []
    for v in meta.get('videos') or []:
        season, number = v.get('season'), v.get('episode') or v.get('number')
        if season is None or number is None:
            continue
        out.append({'id': v.get('id') or f'{root}:{season}:{number}', 'season': season, 'number': number,
                    'name': v.get('name') or v.get('title') or ''})
    return {'title': meta.get('name') or '', 'episodes': sorted(out, key=lambda e: (e['season'], e['number']))}
