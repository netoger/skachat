import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

import httpx

from external_download import ServiceError, download_media, resolve_tiktok


class ServiceTests(unittest.TestCase):
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
