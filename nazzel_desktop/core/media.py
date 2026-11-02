"""Video / audio work with ffmpeg: convert, MP3, compress, GIF, frames, thumbnails, montage."""
import json
import os
import re
import subprocess
import tempfile

from .settings import tool, cache_dir

VIDEO_EXTS = {'mp4', 'mov', 'm4v', 'webm', 'mkv', 'ts', '3gp', 'avi', 'flv', 'wmv', 'mpg', 'mpeg', 'm2ts', 'mts', 'ogv'}
AUDIO_EXTS = {'m4a', 'mp3', 'aac', 'opus', 'ogg', 'wav', 'flac', 'wma', 'mka', 'm4r', 'weba', 'aiff', 'ac3'}
IMAGE_EXTS = {'jpg', 'jpeg', 'png', 'gif', 'webp', 'heic', 'bmp', 'avif'}
# what the built-in player (Chromium / WebView2) plays directly
BROWSER_PLAYABLE = {'mp4', 'm4v', 'mov', 'webm', 'mp3', 'm4a', 'aac', 'wav', 'ogg', 'opus', 'flac', 'weba', 'mkv'}

CREATE_NO_WINDOW = 0x08000000 if os.name == 'nt' else 0


class MediaError(Exception):
    pass


def ext(path):
    return path.rsplit('.', 1)[-1].lower() if '.' in os.path.basename(path) else ''


def kind(path):
    e = ext(path)
    if e in VIDEO_EXTS:
        return 'video'
    if e in AUDIO_EXTS:
        return 'audio'
    if e in IMAGE_EXTS:
        return 'image'
    return 'file'


def _run(args, progress=None, duration=None, cancel=None):
    """Runs ffmpeg; reports progress (0…1) from its -progress output."""
    ffmpeg = tool('ffmpeg')
    if not ffmpeg:
        raise MediaError('ffmpeg مو موجود')
    cmd = [ffmpeg, '-hide_banner', '-nostdin', '-y', '-progress', 'pipe:1', '-nostats'] + args
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8',
                            errors='replace', creationflags=CREATE_NO_WINDOW)
    tail = []
    import threading

    def read_err():
        for line in proc.stderr:
            tail.append(line)
            del tail[:-40]

    reader = threading.Thread(target=read_err, daemon=True)
    reader.start()
    for line in proc.stdout:
        if cancel and cancel():
            proc.kill()
            raise MediaError('تم الإلغاء')
        if progress and duration and line.startswith('out_time_us='):
            try:
                progress(min(1.0, int(line.split('=')[1]) / 1e6 / duration))
            except ValueError:
                pass
    proc.wait()
    reader.join(timeout=2)
    if proc.returncode != 0:
        raise MediaError(''.join(tail[-6:]).strip() or f'ffmpeg exit {proc.returncode}')
    if progress:
        progress(1.0)


def probe(path):
    ffprobe = tool('ffprobe')
    if not ffprobe:
        return {}
    try:
        out = subprocess.run([ffprobe, '-v', 'error', '-print_format', 'json', '-show_format', '-show_streams', path],
                             capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=60,
                             creationflags=CREATE_NO_WINDOW)
        return json.loads(out.stdout or '{}')
    except Exception:
        return {}


def duration(path):
    info = probe(path)
    try:
        return float(info.get('format', {}).get('duration') or 0)
    except ValueError:
        return 0.0


def streams(path, codec_type):
    return [s for s in probe(path).get('streams', []) if s.get('codec_type') == codec_type]


def thumbnail(path, size=360):
    """A JPEG frame (cached), for the library."""
    import hashlib
    key = hashlib.md5((path + str(os.path.getmtime(path))).encode('utf-8', 'replace')).hexdigest()
    out = os.path.join(cache_dir(), 'thumbs', key + f'-{size}.jpg')
    if os.path.exists(out):
        return out
    os.makedirs(os.path.dirname(out), exist_ok=True)
    k = kind(path)
    try:
        if k == 'video':
            length = duration(path)
            at = min(max(length * 0.1, 1), 120) if length > 5 else 0.5
            _run(['-ss', f'{at:.2f}', '-i', path, '-frames:v', '1', '-vf', f'scale={size}:-2', '-q:v', '4', out])
        elif k == 'image':
            _run(['-i', path, '-frames:v', '1', '-vf', f'scale={size}:-2', out])
        elif k == 'audio':
            _run(['-i', path, '-an', '-frames:v', '1', '-vf', f'scale={size}:-2', out])   # cover art, if any
        else:
            return None
    except MediaError:
        return None
    return out if os.path.exists(out) else None


def unique(path):
    if not os.path.exists(path):
        return path
    stem, dot, e = path.rpartition('.')
    if not dot:
        stem, e = path, ''
    n = 2
    while True:
        candidate = f'{stem} ({n})' + (f'.{e}' if e else '')
        if not os.path.exists(candidate):
            return candidate
        n += 1


# ---------------------------------------------------------------------- tools

def to_mp4(src, progress=None, cancel=None):
    """MKV / AVI / … → MP4. Picture and sound copied when MP4 allows it (instant), otherwise the
    sound becomes AAC; text subtitles become .srt files next to the MP4."""
    out = unique(src.rsplit('.', 1)[0] + '.mp4')
    info = probe(src)
    length = float(info.get('format', {}).get('duration') or 0)
    vcodec = next((s.get('codec_name') for s in info.get('streams', []) if s.get('codec_type') == 'video'), '')
    audio_ok = all(s.get('codec_name') in ('aac', 'mp3', 'ac3', 'eac3', 'alac')
                   for s in info.get('streams', []) if s.get('codec_type') == 'audio')
    video_args = ['-c:v', 'copy'] if vcodec in ('h264', 'hevc', 'av1', 'mpeg4') else ['-c:v', 'libx264', '-crf', '20',
                                                                                    '-preset', 'veryfast']
    if vcodec == 'hevc':
        video_args += ['-tag:v', 'hvc1']
    audio_args = ['-c:a', 'copy'] if audio_ok else ['-c:a', 'aac', '-b:a', '192k']
    _run(['-i', src, '-map', '0:v:0?', '-map', '0:a?', *video_args, *audio_args, '-sn', '-movflags', '+faststart', out],
         progress, length, cancel)
    # subtitles inside the file → sidecar .srt
    for index, s in enumerate(x for x in info.get('streams', []) if x.get('codec_type') == 'subtitle'):
        if s.get('codec_name') not in ('subrip', 'ass', 'ssa', 'mov_text', 'webvtt', 'text'):
            continue
        lang = (s.get('tags') or {}).get('language') or f'sub{index + 1}'
        lang = {'ara': 'ar', 'eng': 'en', 'fre': 'fr', 'fra': 'fr', 'spa': 'es', 'tur': 'tr'}.get(lang, lang)
        target = unique(out.rsplit('.', 1)[0] + f'.{lang}.srt')
        try:
            _run(['-i', src, '-map', f'0:s:{index}', target])
        except MediaError:
            pass
    return out


def to_audio(src, fmt='mp3', kbps=256, progress=None, cancel=None):
    out = unique(src.rsplit('.', 1)[0] + f'.{fmt}')
    length = duration(src)
    codec = ['-c:a', 'libmp3lame', '-b:a', f'{kbps}k'] if fmt == 'mp3' else ['-c:a', 'aac', '-b:a', f'{kbps}k']
    _run(['-i', src, '-vn', *codec, '-map_metadata', '0', '-id3v2_version', '3', out], progress, length, cancel)
    return out


def compress(src, target_mb, progress=None, cancel=None):
    """Re-encodes to fit `target_mb` (WhatsApp 16 MB …): bitrate from the size, resolution from the bitrate."""
    out = unique(src.rsplit('.', 1)[0] + ' (مضغوط).mp4')
    length = duration(src)
    if length <= 0:
        raise MediaError('ما قدرت أقرأ مدة الفيديو')
    budget = target_mb * 8_000_000 * 0.93
    audio = 128_000 if budget / length > 900_000 else 64_000
    video = max(100_000, budget / length - audio)
    height = 1080 if video > 2_500_000 else 720 if video > 1_200_000 else 540 if video > 600_000 else 360
    scale = f"scale='if(gt(ih,iw),-2,min({height * 16 // 9},iw))':'if(gt(ih,iw),min({height * 16 // 9},ih),-2)'"
    import uuid
    passlog = os.path.join(tempfile.gettempdir(), 'nazzel-pass-' + uuid.uuid4().hex[:8])
    common = ['-i', src, '-vf', scale, '-c:v', 'libx264', '-preset', 'medium', '-b:v', str(int(video)),
              '-maxrate', str(int(video * 1.4)), '-bufsize', str(int(video * 2)), '-pix_fmt', 'yuv420p']
    # two passes for the picture only (the sound has no first pass)
    _run([*common, '-pass:v', '1', '-passlogfile:v', passlog, '-an', '-f', 'mp4', os.devnull],
         (lambda p: progress(p * 0.5)) if progress else None, length, cancel)
    _run([*common, '-pass:v', '2', '-passlogfile:v', passlog, '-c:a', 'aac', '-b:a', str(audio), '-movflags', '+faststart', out],
         (lambda p: progress(0.5 + p * 0.5)) if progress else None, length, cancel)
    for leftover in (passlog + '-0.log', passlog + '-0.log.mbtree'):
        try:
            os.remove(leftover)
        except OSError:
            pass
    return out


def gif(src, start, seconds, fps=12, width=480, progress=None):
    out = unique(src.rsplit('.', 1)[0] + ' (GIF).gif')
    graph = f'fps={fps},scale={width}:-1:flags=lanczos,split[a][b];[a]palettegen=stats_mode=diff[p];[b][p]paletteuse=dither=bayer'
    _run(['-ss', f'{start:.2f}', '-t', f'{seconds:.2f}', '-i', src, '-filter_complex', graph, '-loop', '0', out],
         progress, seconds)
    return out


def frame(src, at):
    out = unique(src.rsplit('.', 1)[0] + f' (لقطة {int(at)}).png')
    _run(['-ss', f'{at:.3f}', '-i', src, '-frames:v', '1', out])
    return out


def trim(src, start, end, progress=None):
    out = unique(src.rsplit('.', 1)[0] + ' (مقطع).' + (ext(src) or 'mp4'))
    _run(['-ss', f'{start:.3f}', '-to', f'{end:.3f}', '-i', src, '-c', 'copy', '-avoid_negative_ts', 'make_zero', out],
         progress, end - start)
    return out


def audio_part(src, start, seconds, out):
    """A small mono M4A of part of a file (for Gemini)."""
    _run(['-ss', f'{start:.2f}', '-t', f'{seconds:.2f}', '-i', src, '-vn', '-ac', '1', '-c:a', 'aac', '-b:a', '64k', out])
    return out


def montage(clips, out, music=None, music_volume=0.6, clip_volume=1.0, progress=None):
    """Clips one after another (fitted into the first clip's frame) with optional looping music."""
    first = next((s for s in streams(clips[0], 'video')), None)
    if not first:
        raise MediaError('المقطع الأول بدون صورة')
    w, h = int(first.get('width') or 1280), int(first.get('height') or 720)
    rotate = str((first.get('tags') or {}).get('rotate', '0'))
    if rotate in ('90', '270', '-90'):
        w, h = h, w
    scale = min(1.0, 1920 / max(w, h))
    w, h = int(w * scale) // 2 * 2, int(h * scale) // 2 * 2
    inputs, chains, total = [], [], 0.0
    for i, clip in enumerate(clips):
        inputs += ['-i', clip]
        has_audio = bool(streams(clip, 'audio'))
        total += duration(clip)
        chains.append(f'[{i}:v]scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,'
                      f'setsar=1,fps=30,format=yuv420p[v{i}]')
        if has_audio:
            chains.append(f'[{i}:a]aresample=44100,aformat=channel_layouts=stereo,volume={clip_volume}[a{i}]')
        else:
            chains.append(f'anullsrc=r=44100:cl=stereo,atrim=0:{duration(clip):.3f}[a{i}]')
    joined = ''.join(f'[v{i}][a{i}]' for i in range(len(clips)))
    chains.append(f'{joined}concat=n={len(clips)}:v=1:a=1[v][a0]')
    audio_out = '[a0]'
    if music:
        inputs += ['-stream_loop', '-1', '-i', music]
        m = len(clips)
        fade = max(0.0, total - 2)
        chains.append(f'[{m}:a]volume={music_volume},atrim=0:{total:.3f},afade=t=out:st={fade:.3f}:d=2[m]')
        chains.append('[a0][m]amix=inputs=2:duration=first:dropout_transition=0[a]')
        audio_out = '[a]'
    _run([*inputs, '-filter_complex', ';'.join(chains), '-map', '[v]', '-map', audio_out, '-c:v', 'libx264',
          '-preset', 'veryfast', '-crf', '21', '-c:a', 'aac', '-b:a', '192k', '-movflags', '+faststart', out],
         progress, total)
    return out


def srt_to_vtt(text):
    text = text.replace('\r\n', '\n').replace('\r', '\n').lstrip('﻿')
    text = re.sub(r'(\d{2}:\d{2}:\d{2}),(\d{3})', r'\1.\2', text)
    return 'WEBVTT\n\n' + text


# ---------------------------------------------------------------- audio studio

def audio_studio(src, opts, progress=None, cancel=None):
    """Trim, volume, speed, fade, bass/treble and loudness normalising in one pass. Writes a new file."""
    fmt = opts.get('fmt') or 'mp3'
    length = duration(src)
    start = max(0.0, float(opts.get('start') or 0))
    end = float(opts.get('end') or 0) or length
    end = min(end, length) if length else end
    if end and end <= start:
        raise MediaError('نهاية المقطع قبل بدايته')
    speed = min(2.0, max(0.5, float(opts.get('speed') or 1)))
    out_len = ((end - start) if end else 0) / speed
    chain = []
    if abs(speed - 1) > 0.01:
        chain.append(f'atempo={speed:.3f}')
    volume = float(opts.get('volume') or 1)
    if abs(volume - 1) > 0.01:
        chain.append(f'volume={volume:.3f}')
    if float(opts.get('bass') or 0):
        chain.append(f'bass=g={float(opts["bass"]):.1f}')
    if float(opts.get('treble') or 0):
        chain.append(f'treble=g={float(opts["treble"]):.1f}')
    if opts.get('normalize'):
        chain.append('loudnorm=I=-16:TP=-1.5:LRA=11')
    fade_in, fade_out = float(opts.get('fade_in') or 0), float(opts.get('fade_out') or 0)
    if fade_in > 0:
        chain.append(f'afade=t=in:st=0:d={fade_in:.2f}')
    if fade_out > 0 and out_len:
        chain.append(f'afade=t=out:st={max(0.0, out_len - fade_out):.2f}:d={fade_out:.2f}')
    if opts.get('reverse'):
        chain.append('areverse')
    codec = ['-c:a', 'libmp3lame', '-b:a', '256k'] if fmt == 'mp3' else ['-c:a', 'aac', '-b:a', '256k']
    base = src.rsplit('.', 1)[0]
    out = unique(f'{base} (معدّل).{fmt}')
    args = []
    if start:
        args += ['-ss', f'{start:.3f}']
    if end and end < length - 0.05:
        args += ['-to', f'{end:.3f}']
    args += ['-i', src, '-vn']
    if chain:
        args += ['-af', ','.join(chain)]
    _run([*args, *codec, '-map_metadata', '0', out], progress, (out_len or length) * speed if length else None, cancel)
    return out


# ---------------------------------------------------------------- stickers (WhatsApp style WebP)

_STICKER_FIT = ('scale=512:512:force_original_aspect_ratio=decrease,'
                'pad=512:512:(ow-iw)/2:(oh-ih)/2:color=0x00000000,format=yuva420p')


def sticker(src, start=0.0, seconds=0.0, progress=None):
    """512×512 WebP sticker: still (image or one video frame) or animated (a short video clip)."""
    still = kind(src) == 'image' or seconds <= 0
    out = unique(src.rsplit('.', 1)[0] + (' (ملصق).webp'))
    if still:
        pre = ['-ss', f'{start:.3f}'] if kind(src) == 'video' else []
        _run([*pre, '-i', src, '-frames:v', '1', '-vf', _STICKER_FIT, '-c:v', 'libwebp', '-q:v', '85', out])
        return out
    seconds = min(seconds, 8.0)
    for quality in (60, 45, 30, 18):          # animated stickers must stay under 1 MB
        _run(['-ss', f'{start:.3f}', '-t', f'{seconds:.2f}', '-i', src, '-an', '-vf', f'fps=15,{_STICKER_FIT}',
              '-c:v', 'libwebp', '-q:v', str(quality), '-loop', '0', '-compression_level', '6', '-y', out],
             progress, seconds)
        if os.path.getsize(out) < 1_000_000:
            break
    return out


# ---------------------------------------------------------------- built-in player compatibility

_PLAY_VIDEO = {'h264', 'vp8', 'vp9', 'av1'}
_PLAY_AUDIO = {'aac', 'mp3', 'opus', 'vorbis', 'flac', 'pcm_s16le', 'pcm_s24le'}


def needs_prepare(src):
    """True when WebView2 probably can't play this file as it is (HEVC, AC-3/DTS sound, 10-bit, AVI…)."""
    info = probe(src)
    for s in info.get('streams', []):
        codec = s.get('codec_name')
        if s.get('codec_type') == 'video' and (s.get('disposition') or {}).get('attached_pic') != 1:
            if codec not in _PLAY_VIDEO or (s.get('pix_fmt') or 'yuv420p') not in ('yuv420p', 'yuvj420p'):
                return True
        if s.get('codec_type') == 'audio' and codec not in _PLAY_AUDIO:
            return True
    return ext(src) not in ('mp4', 'm4v', 'mov', 'webm', 'mkv')


def playable_copy(src, progress=None, cancel=None):
    """A cached MP4 (H.264 + AAC) the player can always play. Picture is copied when it already is H.264."""
    import hashlib
    st = os.stat(src)
    tag = hashlib.sha1(f'{src}|{st.st_size}|{int(st.st_mtime)}'.encode('utf-8')).hexdigest()[:16]
    folder = os.path.join(cache_dir(), 'play')
    os.makedirs(folder, exist_ok=True)
    out = os.path.join(folder, tag + '.mp4')
    if os.path.isfile(out) and os.path.getsize(out) > 0:
        return out
    info = probe(src)
    v = next((s for s in info.get('streams', []) if s.get('codec_type') == 'video'), {})
    a_streams = [s for s in info.get('streams', []) if s.get('codec_type') == 'audio']
    video_ok = v.get('codec_name') == 'h264' and (v.get('pix_fmt') or 'yuv420p') in ('yuv420p', 'yuvj420p')
    video_args = ['-c:v', 'copy'] if video_ok else ['-c:v', 'libx264', '-preset', 'veryfast', '-crf', '23', '-pix_fmt', 'yuv420p']
    audio_ok = bool(a_streams) and all(s.get('codec_name') in ('aac', 'mp3') for s in a_streams)
    audio_args = ['-c:a', 'copy'] if audio_ok else ['-c:a', 'aac', '-b:a', '192k', '-ac', '2']
    tmp = out + '.part.mp4'
    try:
        _run(['-i', src, '-map', '0:v:0?', '-map', '0:a:0?', *video_args, *audio_args, '-sn', '-movflags', '+faststart', tmp],
             progress, float(info.get('format', {}).get('duration') or 0), cancel)
        os.replace(tmp, out)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
    return out


def extract_text_subs(src):
    """Text subtitle tracks inside a video → "<name>.<lang>.srt" next to it (skipped when already there)."""
    info = probe(src)
    base = src.rsplit('.', 1)[0]
    made = []
    for index, s in enumerate(x for x in info.get('streams', []) if x.get('codec_type') == 'subtitle'):
        if s.get('codec_name') not in ('subrip', 'ass', 'ssa', 'mov_text', 'webvtt', 'text'):
            continue
        lang = (s.get('tags') or {}).get('language') or f'sub{index + 1}'
        lang = {'ara': 'ar', 'eng': 'en', 'fre': 'fr', 'fra': 'fr', 'spa': 'es', 'tur': 'tr'}.get(lang, lang)
        target = f'{base}.{lang}.srt'
        if os.path.exists(target):
            continue
        try:
            _run(['-i', src, '-map', f'0:s:{index}', target])
            made.append(target)
        except MediaError:
            pass
    return made
