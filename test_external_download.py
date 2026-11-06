import tempfile
import json
import os
import unittest
from unittest.mock import patch
from pathlib import Path

import httpx

from external_download import ServiceError, download_media, resolve_tiktok, resolve_instagram, resolve_instagram_embed

os.environ.setdefault('SYSTEM_PROMPT_PATH', str(Path(__file__).parent / 'system_prompt.example.txt'))


class ServiceTests(unittest.TestCase):
    def test_proxy_media_uses_same_embed_agent_as_metadata(self):
        video = b'\x00\x00\x00\x18ftypmp42' + b'x' * 10
        def handler(request):
            self.assertEqual(request.headers['User-Agent'], 'TelegramBot (like TwitterBot)')
            return httpx.Response(200, content=video)
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent / 'Temp') as folder:
            with self.client(handler) as client:
                path = Path(folder) / 'video.mp4'
                download_media('https://www.vxinstagram.com/offload/example/0.mp4', path, 200, client)
                self.assertEqual(path.read_bytes(), video)

    def test_bot_reels_route_resolves_once_and_downloads_without_ytdlp(self):
        import bot
        video = b'\x00\x00\x00\x18ftypmp42' + b'x' * 100
        requests = []

        def handler(request):
            requests.append(request.url.host)
            if request.url.path.startswith('/reel/'):
                return httpx.Response(200, text='<meta property="og:video" content="https://www.vxinstagram.com/offload/example/0.mp4">')
            return httpx.Response(200, content=video)

        with tempfile.TemporaryDirectory(dir=Path(__file__).parent / 'Temp') as folder:
            with patch.object(bot, 'service_client', side_effect=lambda *a: self.client(handler)), \
                    patch.object(bot, 'YoutubeDL', side_effect=AssertionError('Reels не должен вызывать yt-dlp')):
                url = 'https://www.instagram.com/reel/example/'
                info = bot.probe_video(url)
                path = bot.download_video(url, Path(folder), info)
                self.assertEqual(path.read_bytes(), video)
        self.assertEqual(requests, ['www.vxinstagram.com', 'www.vxinstagram.com'])

    def test_instagram_embed_handles_nested_json_without_personal_cookies(self):
        video = 'https://scontent-fra3-2.cdninstagram.com/video.mp4?a=1&b=2'
        for page in [json.dumps({'video_url': video}),
                     '<script>load(' + json.dumps(json.dumps({'video_url': video})) + ')</script>']:
            def handler(request):
                self.assertEqual(request.url.path, '/p/DfHCqvPtdf6/embed/captioned/')
                self.assertNotIn('cookie', request.headers)
                return httpx.Response(200, text=page)
            with self.client(handler) as client:
                self.assertEqual(resolve_instagram_embed('https://www.instagram.com/reel/DfHCqvPtdf6/?tracking=1', client), video)

    def test_instagram_proxy_failure_uses_anonymous_embed(self):
        def handler(request):
            if request.url.host == 'www.vxinstagram.com':
                return httpx.Response(503)
            return httpx.Response(200, text='{"video_url":"https://scontent.cdninstagram.com/video.mp4"}')
        with self.client(handler) as client:
            self.assertEqual(resolve_instagram('https://instagram.com/reel/example/', client), 'https://scontent.cdninstagram.com/video.mp4')

    def test_instagram_proxy_never_returns_an_untrusted_host(self):
        page = '<meta property="og:video" content="https://127.0.0.1/offload/example/0.mp4">'
        with self.client(lambda r: httpx.Response(200, text=page)) as client:
            with self.assertRaises(ServiceError):
                resolve_instagram('https://instagram.com/reel/example/', client)

    def test_instagram_rejects_missing_video_and_untrusted_cdn(self):
        for page in ['<html>Login required</html>', '{"video_url":"https://127.0.0.1/video.mp4"}',
                     '{"video_url":"https://cdninstagram.com.evil.example/video.mp4"}']:
            with self.client(lambda r: httpx.Response(200, text=page)) as client:
                with self.assertRaises(ServiceError):
                    resolve_instagram('https://instagram.com/reel/example/', client)

    def test_public_reels_ignore_personal_cookies_but_youtube_keeps_support(self):
        import bot
        with patch.object(bot, 'resolve_cookiefile', return_value=Path('private-cookies.txt')):
            self.assertNotIn('cookiefile', bot.build_ydl_opts('https://www.instagram.com/reel/test/'))
            self.assertEqual(bot.build_ydl_opts('https://youtube.com/shorts/test')['cookiefile'], 'private-cookies.txt')

    @classmethod
    def setUpClass(cls):
        (Path(__file__).parent / 'Temp').mkdir(exist_ok=True)

    def client(self, handler):
        return httpx.Client(transport=httpx.MockTransport(handler))

    def test_resolves_video_without_sending_browser_cookies(self):
        def handler(request):
            self.assertNotIn('cookie', request.headers)
            if request.method == 'GET':
                return httpx.Response(200, text="var s_tt = 'fresh-token';")
            self.assertIn(b'tt=fresh-token', request.content)
            return httpx.Response(200, text='<a href="https://tikcdn.io/video">Без водяного знака</a><a href="https://tikcdn.io/audio">Скачать MP3</a>')
        with self.client(handler) as client:
            self.assertEqual(resolve_tiktok('https://vt.tiktok.com/example/', client, 'ssstik'), 'https://tikcdn.io/video')

    def test_rejects_unexpected_service_links(self):
        with self.client(lambda r: httpx.Response(200, text="s_tt = 'x';" if r.method == 'GET' else '<a href="http://127.0.0.1/video">Без водяного знака</a>')) as client:
            with self.assertRaises(ServiceError):
                resolve_tiktok('https://vt.tiktok.com/example/', client, 'ssstik')

    def test_tikwm_returns_video_and_rejects_errors_or_unsafe_urls(self):
        url = 'https://v19.tiktokcdn-us.com/video.mp4'
        responses = [
            {'code': 0, 'data': {'play': url}},
            {'code': -1, 'msg': 'rate limit'},
            {'code': 0, 'data': {'play': 'http://127.0.0.1/private'}},
            [],
        ]
        for payload in responses:
            with self.client(lambda r: httpx.Response(200, json=payload)) as client:
                if payload == responses[0]:
                    self.assertEqual(resolve_tiktok('https://vt.tiktok.com/example/', client), url)
                else:
                    with self.assertRaises(ServiceError):
                        resolve_tiktok('https://vt.tiktok.com/example/', client)

    def test_download_and_size_limit_and_html_rejection(self):
        video = b'\x00\x00\x00\x18ftypmp42' + b'x' * 100
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent / 'Temp') as folder:
            path = Path(folder) / 'video.mp4'
            with self.client(lambda r: httpx.Response(200, content=video)) as client:
                download_media('https://tikcdn.io/video', path, 200, client)
                self.assertEqual(path.read_bytes(), video)
            for content, limit in [(video, 20), (b'<html>blocked</html>', 200)]:
                with self.client(lambda r: httpx.Response(200, content=content)) as client:
                    with self.assertRaises(ServiceError):
                        download_media('https://tikcdn.io/video', path, limit, client)
                    self.assertFalse(path.exists())

    def test_rejects_redirects_to_private_addresses(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent / 'Temp') as folder:
            path = Path(folder) / 'video.mp4'
            with self.client(lambda r: httpx.Response(302, headers={'Location': 'http://127.0.0.1/'})) as client:
                with self.assertRaises(ServiceError):
                    download_media('https://tikcdn.io/video', path, 200, client)


if __name__ == '__main__':
    unittest.main()
