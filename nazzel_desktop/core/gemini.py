"""Google Gemini: the assistant (with actions in the app), AI Arabic subtitles and transcripts."""
import base64
import json
import mimetypes
import os
import re
import tempfile
import urllib.error
import urllib.request

from . import engine, media
from .settings import settings

BASE = 'https://generativelanguage.googleapis.com'
FALLBACK = ['gemini-2.5-flash', 'gemini-2.5-flash-lite', 'gemini-flash-lite-latest', 'gemini-2.0-flash']

SYSTEM = """أنت «مساعد نزّل» داخل برنامج «نزّل» على الكمبيوتر. رد بلغة المستخدم ولهجته (غالباً عربي خليجي)، \
بأسلوب ودود ومختصر وواضح. تقدر تجاوب أي سؤال، وتلخّص وتترجم وتشرح الملفات والصور والصوتيات والفيديوهات، \
وتلخّص فيديوهات يوتيوب من رابطها.
عندك أدوات تنفّذ فيها داخل البرنامج: تحميل رابط (فيديو/صوت/صور)، تحميل آخر مقاطع قناة، متابعة قناة، البحث في \
ملفات المستخدم وتشغيلها. لما يطلب شي منها نفّذه على طول بالأداة، وبعدين قل له باختصار وش سويت."""


class GeminiError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message

    @property
    def retry_elsewhere(self):
        text = self.message.lower()
        if 'api key' in text or 'api_key' in text:
            return False
        return self.code in (429, 404, 403) or (self.code == 400 and any(w in text for w in ('not found', 'not supported',
                                                                                              'search', 'tool')))

    def friendly(self):
        if self.code == 400 and 'api key' in self.message.lower():
            return 'المفتاح غلط، تأكد منه من الإعدادات'
        if self.code == 429:
            return 'Google رفض الطلب (429): ' + self.message[:200]
        return f'خطأ من Gemini ({self.code}): {self.message[:200]}'


def _key():
    key = (settings().get('gemini_key') or '').strip()
    if not key:
        raise GeminiError(0, 'حط مفتاح Gemini من الإعدادات (مجاني من aistudio.google.com/apikey)')
    return key


def _post(path, body, key, timeout=300):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode(), method='POST',
                                 headers={'Content-Type': 'application/json', 'x-goog-api-key': key})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=engine._ssl_context()) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raw = e.read().decode('utf-8', 'replace')
        try:
            message = json.loads(raw)['error']['message']
        except Exception:
            message = raw[:300]
        raise GeminiError(e.code, message)


def available_models(key=None):
    try:
        req = urllib.request.Request(BASE + '/v1beta/models?pageSize=200', headers={'x-goog-api-key': key or _key()})
        with urllib.request.urlopen(req, timeout=20, context=engine._ssl_context()) as resp:
            data = json.loads(resp.read().decode())
    except Exception:
        return []
    return [m['name'].replace('models/', '') for m in data.get('models', [])
            if 'generateContent' in (m.get('supportedGenerationMethods') or [])]


def _upload(path, key):
    """Gemini's file upload for big files (videos); returns the file uri."""
    size = os.path.getsize(path)
    mime = mimetypes.guess_type(path)[0] or 'application/octet-stream'
    start = urllib.request.Request(BASE + '/upload/v1beta/files', method='POST',
                                   data=json.dumps({'file': {'display_name': os.path.basename(path)[:100]}}).encode(),
                                   headers={'x-goog-api-key': key, 'X-Goog-Upload-Protocol': 'resumable',
                                            'X-Goog-Upload-Command': 'start',
                                            'X-Goog-Upload-Header-Content-Length': str(size),
                                            'X-Goog-Upload-Header-Content-Type': mime, 'Content-Type': 'application/json'})
    with urllib.request.urlopen(start, timeout=60, context=engine._ssl_context()) as resp:
        upload_url = resp.headers['X-Goog-Upload-URL']
    with open(path, 'rb') as fh:
        send = urllib.request.Request(upload_url, data=fh.read(), method='POST',
                                      headers={'X-Goog-Upload-Command': 'upload, finalize', 'X-Goog-Upload-Offset': '0'})
    with urllib.request.urlopen(send, timeout=1800, context=engine._ssl_context()) as resp:
        info = json.loads(resp.read().decode())['file']
    import time
    for _ in range(100):
        if info.get('state') != 'PROCESSING':
            break
        time.sleep(3)
        req = urllib.request.Request(BASE + '/v1beta/' + info['name'], headers={'x-goog-api-key': key})
        with urllib.request.urlopen(req, timeout=30, context=engine._ssl_context()) as resp:
            info = json.loads(resp.read().decode())
    return info['uri'], mime


def file_part(path, key):
    mime = mimetypes.guess_type(path)[0] or 'application/octet-stream'
    if media.ext(path) in ('srt', 'vtt', 'txt', 'md', 'log', 'csv', 'json'):
        mime = 'text/plain'
    if os.path.getsize(path) <= 14 * 1024 * 1024:
        with open(path, 'rb') as fh:
            return {'inline_data': {'mime_type': mime, 'data': base64.b64encode(fh.read()).decode()}}
    uri, mime = _upload(path, key)
    return {'file_data': {'mime_type': mime, 'file_uri': uri}}


def generate(contents, system=SYSTEM, search=False, functions=None, model=None, low_media=False):
    """One answer (text, sources, function calls) on the first model that accepts it."""
    key = _key()
    chosen = model or settings().get('gemini_model') or FALLBACK[0]
    plans = [(chosen, search)] + ([(chosen, False)] if search else [])
    tried, index, last = set(), 0, None
    while index < len(plans):
        name, with_search = plans[index]
        index += 1
        if (name, with_search) in tried:
            continue
        tried.add((name, with_search))
        body = {'systemInstruction': {'parts': [{'text': system}]}, 'contents': contents}
        tools = []
        if functions:
            tools.append({'functionDeclarations': functions})
        if with_search:
            tools.append({'google_search': {}})
        if tools:
            body['tools'] = tools
        if low_media:
            body['generationConfig'] = {'mediaResolution': 'MEDIA_RESOLUTION_LOW'}
        try:
            data = _post(f'/v1beta/models/{name}:generateContent', body, key)
        except GeminiError as e:
            last = e
            if not e.retry_elsewhere:
                raise
            if index == len(plans):
                for m in FALLBACK + [x for x in available_models(key) if 'flash' in x and x not in FALLBACK]:
                    plans.append((m, False))
            continue
        candidate = (data.get('candidates') or [{}])[0]
        parts = (candidate.get('content') or {}).get('parts') or []
        text = ''.join(p.get('text', '') for p in parts if not p.get('thought'))
        calls = [p for p in parts if 'functionCall' in p]
        sources = []
        for chunk in (candidate.get('groundingMetadata') or {}).get('groundingChunks') or []:
            web = chunk.get('web') or {}
            if web.get('uri'):
                sources.append({'title': web.get('title') or web['uri'], 'url': web['uri']})
        if name != chosen:
            settings().update({'gemini_model': name})
        return {'text': text, 'calls': calls, 'parts': parts, 'sources': sources, 'model': name}
    raise last or GeminiError(0, 'ما فيه نموذج متاح')


# ------------------------------------------------------------------ subtitles / transcript

AI_SUBS = """You write subtitles. Listen to the audio and output subtitles in Arabic (Modern Standard Arabic). \
If the speech is not Arabic, translate it to Arabic. Output ONLY valid SRT with times from the start of THIS audio. \
Each subtitle at most 2 short lines and at most 6 seconds. No markdown, no code fences. No speech: output nothing."""

TRANSCRIPT = """Transcribe the speech word for word in the language it is spoken (do not translate). Short paragraphs, \
each starting with its time from the start of THIS audio as [MM:SS]. Mark speakers as "المتحدث ١:" when there are \
several. Plain text only."""


def _parts_of(path, seconds=600):
    length = media.duration(path)
    count = max(1, int(-(-length // seconds)))
    return length, [(i * seconds, min(seconds, length - i * seconds)) for i in range(count)]


def _srt_cues(text):
    cues = []
    for block in re.split(r'\n\s*\n', text.replace('\r', '').replace('```srt', '').replace('```', '')):
        m = re.search(r'(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})\s*-->\s*(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})', block)
        if not m:
            continue
        g = [int(x) for x in m.groups()]
        start = g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 1000
        end = g[4] * 3600 + g[5] * 60 + g[6] + g[7] / 1000
        body = block[m.end():].strip()
        if body:
            cues.append((start, end, body))
    return cues


def _stamp(t):
    ms = int(round(t * 1000))
    return f'{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}'


def ai_subtitles(path, progress=None):
    key = _key()
    length, parts = _parts_of(path)
    cues = []
    for i, (start, seconds) in enumerate(parts):
        with tempfile.TemporaryDirectory() as tmp:
            audio = media.audio_part(path, start, seconds, os.path.join(tmp, 'part.m4a'))
            answer = generate([{'role': 'user', 'parts': [file_part(audio, key), {'text': 'Write the Arabic SRT.'}]}],
                              system=AI_SUBS)['text']
        for a, b, body in _srt_cues(answer):
            cues.append((a + start, min(b + start, start + seconds + 1), body))
        if progress:
            progress((i + 1) / len(parts))
    if not cues:
        raise GeminiError(0, 'ما لقيت كلام في الملف')
    out = os.path.splitext(path)[0] + '.ar-ai.srt'
    with open(out, 'w', encoding='utf-8') as fh:
        for n, (a, b, body) in enumerate(sorted(cues), 1):
            fh.write(f'{n}\n{_stamp(a)} --> {_stamp(b)}\n{body}\n\n')
    return out


def transcript(path, progress=None):
    key = _key()
    length, parts = _parts_of(path)
    text = ''
    for i, (start, seconds) in enumerate(parts):
        with tempfile.TemporaryDirectory() as tmp:
            audio = media.audio_part(path, start, seconds, os.path.join(tmp, 'part.m4a'))
            answer = generate([{'role': 'user', 'parts': [file_part(audio, key), {'text': 'Transcribe this audio.'}]}],
                              system=TRANSCRIPT)['text']

        def shift(m):
            t = int(m.group(1)) * 60 + int(m.group(2)) + int(start)
            return f'[{t // 3600}:{t // 60 % 60:02d}:{t % 60:02d}]' if t >= 3600 else f'[{t // 60:02d}:{t % 60:02d}]'
        text += re.sub(r'\[(\d{1,2}):(\d{2})\]', shift, answer).strip() + '\n\n'
        if progress:
            progress((i + 1) / (len(parts) + 1))
    summary = ''
    try:
        summary = generate([{'role': 'user', 'parts': [{'text': text[:120000]}]}],
                           system='لخّص النص في ٥ نقاط قصيرة بنفس لغة النص. نص عادي.')['text'].strip()
    except GeminiError:
        pass
    body = (f'الملخص:\n{summary}\n\n────────\n\n' if summary else '') + text
    out = media.unique(os.path.splitext(path)[0] + ' (نص).txt')
    with open(out, 'w', encoding='utf-8') as fh:
        fh.write(body)
    return out


def transcribe_audio(b64, mime='audio/webm'):
    """Short spoken request (base64) → the words, in the language spoken."""
    mime = (mime or 'audio/webm').split(';')[0]
    answer = generate([{'role': 'user', 'parts': [
        {'inline_data': {'mime_type': mime, 'data': b64}},
        {'text': 'اكتب بالضبط الكلام المنطوق في المقطع كنص فقط بدون أي إضافة أو شرح. إذا ما في كلام اكتب: ...'}]}],
        system='You are a precise speech-to-text engine. Output only the transcription.', model=None)
    text = (answer.get('text') or '').strip()
    return '' if text == '...' else text


def read_image_text(image_path, translate=False):
    """The text in a picture (a photo, a screenshot, a video frame). translate=True: the text translated to Arabic."""
    part = file_part(image_path, _key())
    ask = ('اقرأ كل النص الظاهر في الصورة بالضبط وبنفس لغته وترتيبه، بدون أي شرح أو إضافة. '
           'إذا ما في نص اكتب: ما لقيت نص.')
    if translate:
        ask = ('اقرأ كل النص الظاهر في الصورة، وترجمه للعربية بشكل واضح. اكتب النص الأصلي أولاً ثم الترجمة تحته '
               'تحت عنوان «الترجمة». إذا ما في نص اكتب: ما لقيت نص.')
    answer = generate([{'role': 'user', 'parts': [part, {'text': ask}]}], search=False,
                      system='You are a precise OCR and translation engine.', low_media=False)
    return (answer.get('text') or '').strip()
