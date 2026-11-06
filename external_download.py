"""Скачивание публичных TikTok и Reels без личных cookies."""

import re
import json
import time
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

import httpx


class ServiceError(Exception):
    pass


def service_client(referer='https://ssstik.io/ru'):
    # Личные cookies и настроенный для yt-dlp прокси сюда не передаются.
    return httpx.Client(timeout=15, trust_env=False,
                        transport=httpx.HTTPTransport(local_address='0.0.0.0', retries=2), headers={
        'User-Agent': 'Mozilla/5.0', 'Referer': referer,
    })


def valid_media_url(url):
    parsed = urlsplit(url)
    hostname = parsed.hostname or ''
    return (parsed.scheme == 'https' and (hostname == 'tikcdn.io'
            or hostname.endswith('.tiktokcdn-us.com')
            or hostname == 'www.vxinstagram.com'
            or hostname.endswith('.cdninstagram.com')
            or hostname.endswith('.fbcdn.net'))
            and parsed.port in (None, 443) and not parsed.username and not parsed.password)


class VideoLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []
        self.href = ''
        self.label = ''

    def handle_starttag(self, tag, attrs):
        if tag == 'a':
            self.href = dict(attrs).get('href', '')
            self.label = ''

    def handle_data(self, data):
        self.label += data

    def handle_endtag(self, tag):
        if tag == 'a':
            if ('без водяного знака' in self.label.lower()
                    or 'without watermark' in self.label.lower()):
                self.links.append(self.href)
            self.href = ''


def instagram_shortcode(url):
    shortcode = re.search(r'/(?:reels?|p)/([A-Za-z0-9_-]{1,50})(?:/|$)', urlsplit(url).path)
    if not shortcode:
        raise ServiceError('Нужна прямая ссылка на публичный Reels или видеопост Instagram.')
    return shortcode.group(1)


class InstagramVideoMetadata(HTMLParser):
    def __init__(self):
        super().__init__()
        self.urls = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'meta' and attrs.get('property') in ('og:video', 'og:video:secure_url'):
            self.urls.append(attrs.get('content', ''))


def resolve_instagram(url, client):
    shortcode = instagram_shortcode(url)
    try:
        response = client.get('https://www.vxinstagram.com/reel/' + shortcode + '/', headers={
            'User-Agent': 'TelegramBot (like TwitterBot)', 'Referer': 'https://www.vxinstagram.com/',
        })
        response.raise_for_status()
        if len(response.content) > 4 * 1024 * 1024:
            raise ServiceError('Сервис вернул слишком большую страницу.')
        metadata = InstagramVideoMetadata()
        metadata.feed(response.text)
        for video_url in metadata.urls:
            parsed = urlsplit(video_url)
            if (valid_media_url(video_url) and parsed.hostname == 'www.vxinstagram.com'
                    and parsed.path.startswith('/offload/' + shortcode + '/')):
                return video_url
    except (httpx.HTTPError, ValueError, ServiceError):
        pass
    # Резерв без авторизации, если внешний сервис недоступен.
    return resolve_instagram_embed(url, client)


def resolve_instagram_embed(url, client):
    shortcode = instagram_shortcode(url)
    try:
        response = client.get('https://www.instagram.com/p/' + shortcode + '/embed/captioned/')
        response.raise_for_status()
        if len(response.content) > 4 * 1024 * 1024:
            raise ServiceError('Instagram вернул слишком большую страницу.')
        # В embed данные встречаются и как JSON, и как JSON внутри строки JavaScript.
        json_string = r'"(?:[^"\\]|\\.)*"'
        layers = [response.text]
        for token in re.finditer(json_string, response.text):
            if 'video_url' in token.group(0):
                decoded = json.loads(token.group(0))
                if isinstance(decoded, str):
                    layers.append(decoded)
        for layer in layers:
            match = re.search(r'"video_url"\s*:\s*(' + json_string + ')', layer)
            if match:
                video_url = unescape(json.loads(match.group(1)))
                host = urlsplit(video_url).hostname or ''
                if valid_media_url(video_url) and host.endswith(('.cdninstagram.com', '.fbcdn.net')):
                    return video_url
        raise ServiceError('Instagram не отдал публичное видео. Проверь, что ролик доступен без входа.')
    except (httpx.HTTPError, ValueError) as exc:
        raise ServiceError('Не удалось получить Reels с публичной страницы Instagram.') from exc


def resolve_tiktok(url, client, provider='tikwm'):
    if provider == 'ssstik':
        return resolve_ssstik(url, client)
    try:
        response = client.get('https://www.tikwm.com/api/', params={'url': url})
        response.raise_for_status()
        payload = response.json()
        data = payload.get('data') if isinstance(payload, dict) else None
        link = data.get('play') if isinstance(data, dict) else None
        if payload.get('code') != 0 or not isinstance(link, str) or not valid_media_url(link):
            raise ServiceError('TikWM не вернул доступное видео.')
        return link
    except (httpx.HTTPError, ValueError, AttributeError) as exc:
        raise ServiceError('Не удалось получить видео через TikWM.') from exc


def resolve_ssstik(url, client):
    try:
        page = client.get('https://ssstik.io/ru')
        page.raise_for_status()
        token = re.search(r'''s_tt\s*=\s*['"]([^'"]+)''', page.text)
        if not token:
            raise ServiceError('ssstik изменил форму или запросил проверку браузера.')
        response = client.post('https://ssstik.io/abc?url=dl', data={
            'id': url, 'locale': 'ru', 'tt': token.group(1),
        })
        response.raise_for_status()
        parser = VideoLinks()
        parser.feed(response.text)
        for link in parser.links:
            if valid_media_url(link):
                return link
        raise ServiceError('ssstik не вернул ссылку на видео.')
    except (httpx.HTTPError, ValueError) as exc:
        raise ServiceError('Не удалось получить видео через ssstik.') from exc


def download_media(url, path: Path, max_bytes, client):
    try:
        if not valid_media_url(url):
            raise ServiceError('Сервис вернул неподдерживаемый адрес файла.')
        started = time.monotonic()
        # Редиректы намеренно не разрешены: адрес получен от внешнего сервиса.
        with client.stream('GET', url, follow_redirects=False) as response:
            response.raise_for_status()
            size = response.headers.get('Content-Length')
            if size and int(size) > max_bytes:
                raise ServiceError('Видео превышает лимит отправки бота.')
            total = 0
            prefix = bytearray()
            with path.open('wb') as output:
                for chunk in response.iter_bytes(64 * 1024):
                    total += len(chunk)
                    if total > max_bytes:
                        raise ServiceError('Видео превышает лимит отправки бота.')
                    if time.monotonic() - started > 120:
                        raise ServiceError('Сервис слишком долго отдаёт видео.')
                    if len(prefix) < 12:
                        prefix.extend(chunk[:12 - len(prefix)])
                    if len(prefix) >= 8 and prefix[4:8] != b'ftyp':
                        raise ServiceError('Вместо MP4 сервис вернул другой ответ.')
                    output.write(chunk)
            if len(prefix) < 12 or prefix[4:8] != b'ftyp':
                raise ServiceError('Сервис вернул пустой или повреждённый MP4.')
    except (httpx.HTTPError, ValueError) as exc:
        path.unlink(missing_ok=True)
        raise ServiceError('Не удалось скачать файл с сервера загрузки.') from exc
    except Exception:
        path.unlink(missing_ok=True)
        raise
