"""Read-only LRCLIB client with a bounded response and network deadline."""
import json
import httpx


async def fetch_lyrics(path, params=None):
    async with httpx.AsyncClient(timeout=10, headers={'User-Agent':'MusicTelegramBot/1.0'}) as client:
        async with client.stream('GET', 'https://lrclib.net/api/' + path, params=params) as response:
            response.raise_for_status()
            data = bytearray()
            async for chunk in response.aiter_bytes():
                data.extend(chunk)
                if len(data) > 1024 * 1024:
                    raise ValueError('Lyrics response exceeds limit')
            return json.loads(data)
