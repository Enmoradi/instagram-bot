"""Isolated yt-dlp worker. The parent enforces a wall-clock deadline."""
import json
import os
import re
import sys
from urllib.parse import urlsplit

import requests
import yt_dlp

MAX_BYTES = max(5, min(49, int(os.environ.get('MUSIC_MAX_MB', '49')))) * 1024 * 1024
MAX_DURATION = max(1, min(180, int(os.environ.get('MUSIC_MAX_MINUTES', '60')))) * 60
SEARCH_RESULTS = max(3, min(10, int(os.environ.get('MUSIC_SEARCH_RESULTS', '5'))))
BITRATE = str(int(os.environ.get('MUSIC_BITRATE', '128')))


def enabled(name, default=True):
    return os.environ.get(name, '1' if default else '0').lower() in ('1', 'true', 'yes', 'on')


def options():
    opts = {
        'quiet': True, 'no_warnings': True, 'noplaylist': True,
        'socket_timeout': 20,
        'retries': max(1, min(5, int(os.environ.get('MUSIC_RETRIES', '2')))),
        'fragment_retries': max(1, min(5, int(os.environ.get('MUSIC_RETRIES', '2')))),
        'cachedir': False,
        'js_runtimes': {'node': {}},
    }
    if os.environ.get('MUSIC_COOKIES_FILE'):
        opts['cookiefile'] = os.environ['MUSIC_COOKIES_FILE']
    if os.environ.get('MUSIC_PROXY_URL'):
        opts['proxy'] = os.environ['MUSIC_PROXY_URL']
    return opts


def search(query):
    if not enabled('MUSIC_YOUTUBE_ENABLED'):
        return search_soundcloud(query)
    opts = options()
    opts.update(extract_flat='in_playlist', skip_download=True, ignoreerrors=True)
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f'ytsearch{max(8, SEARCH_RESULTS)}:' + query[:200], download=False)
    except Exception:
        return search_soundcloud(query)
    results = []
    for entry in (info or {}).get('entries') or []:
        if not entry or not re.fullmatch(r'[A-Za-z0-9_-]{11}', entry.get('id', '')):
            continue
        duration = entry.get('duration')
        if entry.get('is_live') or (duration and duration > MAX_DURATION):
            continue
        results.append({
            'id': entry['id'], 'title': (entry.get('title') or 'Unknown')[:180],
            'artist': (entry.get('artist') or entry.get('uploader') or '')[:120],
            'duration': int(duration or 0),
        })
    if results:
        return results[:SEARCH_RESULTS]
    return search_soundcloud(query)


def search_soundcloud(query):
    if not enabled('MUSIC_SOUNDCLOUD_ENABLED'):
        return []
    opts = options()
    opts.update(extract_flat='in_playlist', skip_download=True, ignoreerrors=True)
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f'scsearch{SEARCH_RESULTS}:' + query[:200], download=False)
    results = []
    for entry in (info or {}).get('entries') or []:
        if not entry:
            continue
        duration = entry.get('duration')
        if duration and duration > MAX_DURATION:
            continue
        source = entry.get('webpage_url') or entry.get('url')
        if not source or (urlsplit(source).hostname or '').lower() not in ('soundcloud.com', 'www.soundcloud.com'):
            continue
        results.append({
            'id': source,
            'title': (entry.get('title') or 'Unknown')[:180],
            'artist': (entry.get('uploader') or entry.get('artist') or '')[:120],
            'duration': int(duration or 0),
        })
    return results[:SEARCH_RESULTS]


def _cleanup(directory):
    for name in os.listdir(directory):
        path = os.path.join(directory, name)
        if os.path.isfile(path):
            try:
                os.remove(path)
            except OSError:
                pass


def _download_ytdlp(source, directory):
    def guard(info, *, incomplete=False):
        if info.get('is_live') or (info.get('duration') or 0) > MAX_DURATION:
            return f'Track is live or exceeds {MAX_DURATION // 60} minutes'

    def progress(data):
        if (data.get('downloaded_bytes') or 0) > MAX_BYTES:
            raise ValueError('Track exceeds Telegram upload limit')

    opts = options()
    opts.update({
        'format': 'bestaudio/best', 'max_filesize': MAX_BYTES,
        'outtmpl': os.path.join(directory, 'music.%(ext)s'),
        'match_filter': guard, 'progress_hooks': [progress],
        'postprocessors': [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3',
                            'preferredquality': BITRATE}],
    })
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.extract_info(source, download=True)


def _download_cobalt(video_id, directory):
    base = os.environ.get('MUSIC_COBALT_API_URL', '').rstrip('/')
    if not base or not re.fullmatch(r'[A-Za-z0-9_-]{11}', video_id):
        raise ValueError('Cobalt is unavailable for this source')
    response = requests.post(base + '/', json={
        'url': 'https://www.youtube.com/watch?v=' + video_id,
        'downloadMode': 'audio',
        'audioFormat': 'mp3',
        'filenameStyle': 'basic',
    }, headers={'Accept': 'application/json'}, timeout=(10, 80))
    response.raise_for_status()
    data = response.json()
    media_url = data.get('url') if data.get('status') in ('tunnel', 'redirect') else None
    if not media_url:
        code = (data.get('error') or {}).get('code') or data.get('status')
        raise RuntimeError('Cobalt: ' + str(code))
    path = os.path.join(directory, 'music.mp3')
    size = 0
    with requests.get(media_url, stream=True, timeout=(10, 120)) as media:
        media.raise_for_status()
        with open(path, 'wb') as handle:
            for chunk in media.iter_content(1024 * 1024):
                if not chunk:
                    continue
                size += len(chunk)
                if size > MAX_BYTES:
                    raise ValueError('Track exceeds Telegram upload limit')
                handle.write(chunk)
    if size <= 0:
        raise ValueError('Cobalt returned an empty file')


def download(source_id, directory, query=''):
    is_youtube = bool(re.fullmatch(r'[A-Za-z0-9_-]{11}', source_id))
    is_soundcloud = (urlsplit(source_id).hostname or '').lower() in ('soundcloud.com', 'www.soundcloud.com')
    if not is_youtube and not is_soundcloud:
        raise ValueError('Invalid track source')

    errors = []
    if enabled('MUSIC_YOUTUBE_ENABLED') or is_soundcloud:
        try:
            source = 'https://www.youtube.com/watch?v=' + source_id if is_youtube else source_id
            _download_ytdlp(source, directory)
        except Exception as exc:
            errors.append('yt-dlp: ' + str(exc))
            _cleanup(directory)
    if is_youtube and not os.path.isfile(os.path.join(directory, 'music.mp3')) and enabled('MUSIC_COBALT_ENABLED'):
        try:
            _download_cobalt(source_id, directory)
        except Exception as exc:
            errors.append('cobalt: ' + str(exc))
            _cleanup(directory)
    if not os.path.isfile(os.path.join(directory, 'music.mp3')) and query and enabled('MUSIC_SOUNDCLOUD_ENABLED'):
        try:
            opts = options()
            opts.update(extract_flat=True, skip_download=True)
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info('scsearch1:' + query[:200], download=False)
            entry = next((item for item in (info or {}).get('entries') or [] if item), None)
            source = (entry or {}).get('webpage_url') or (entry or {}).get('url')
            if not source:
                raise ValueError('No SoundCloud match')
            _download_ytdlp(source, directory)
        except Exception as exc:
            errors.append('soundcloud: ' + str(exc))
            _cleanup(directory)
    output = os.path.join(directory, 'music.mp3')
    if not os.path.isfile(output) or not 0 < os.path.getsize(output) <= MAX_BYTES:
        raise RuntimeError(' | '.join(errors)[-900:] or 'No uploadable audio was produced')
    return {'path': output}


if __name__ == '__main__':
    try:
        result = (search(sys.argv[2]) if sys.argv[1] == 'search' else
                  download(sys.argv[2], sys.argv[3], sys.argv[4] if len(sys.argv) > 4 else ''))
        print(json.dumps(result, ensure_ascii=False))
    except Exception as exc:
        print(type(exc).__name__ + ': ' + str(exc)[:900], file=sys.stderr)
        sys.exit(1)
