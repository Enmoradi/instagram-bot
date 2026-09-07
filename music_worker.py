"""Isolated yt-dlp worker. The parent enforces a wall-clock deadline."""
import json
import os
import re
import sys

import yt_dlp

MAX_BYTES = 49 * 1024 * 1024
MAX_DURATION = 15 * 60


def options():
    opts = {
        'quiet': True, 'no_warnings': True, 'noplaylist': True,
        'socket_timeout': 15, 'retries': 1, 'fragment_retries': 1,
        'cachedir': False,
        'js_runtimes': {'node': {}},
    }
    if os.environ.get('MUSIC_COOKIES_FILE'):
        opts['cookiefile'] = os.environ['MUSIC_COOKIES_FILE']
    if os.environ.get('MUSIC_PROXY_URL'):
        opts['proxy'] = os.environ['MUSIC_PROXY_URL']
    return opts


def search(query):
    opts = options()
    opts.update(extract_flat='in_playlist', skip_download=True, ignoreerrors=True)
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info('ytsearch8:' + query[:200], download=False)
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
    return results[:5]


def download(video_id, directory):
    if not re.fullmatch(r'[A-Za-z0-9_-]{11}', video_id):
        raise ValueError('Invalid track id')

    def guard(info, *, incomplete=False):
        if info.get('is_live') or (info.get('duration') or 0) > MAX_DURATION:
            return 'Track is live or exceeds 15 minutes'

    def progress(data):
        if (data.get('downloaded_bytes') or 0) > MAX_BYTES:
            raise ValueError('Track exceeds download limit')

    opts = options()
    opts.update({
        'format': 'bestaudio/best', 'max_filesize': MAX_BYTES,
        'outtmpl': os.path.join(directory, 'music.%(ext)s'),
        'match_filter': guard, 'progress_hooks': [progress],
        'postprocessors': [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '192'}],
    })
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.extract_info('https://www.youtube.com/watch?v=' + video_id, download=True)
    output = os.path.join(directory, 'music.mp3')
    if not os.path.isfile(output) or not 0 < os.path.getsize(output) <= MAX_BYTES:
        raise ValueError('No uploadable audio was produced')
    return {'path': output}


if __name__ == '__main__':
    try:
        result = search(sys.argv[2]) if sys.argv[1] == 'search' else download(sys.argv[2], sys.argv[3])
        print(json.dumps(result, ensure_ascii=False))
    except Exception as exc:
        print(type(exc).__name__ + ': music provider failed', file=sys.stderr)
        sys.exit(1)
