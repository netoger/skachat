import io
import json
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch
import httpx

from webhook_cgi import RequestError, read_update

from telegram.ext import Application, ExtBot, MessageHandler, PicklePersistence, filters
from telegram.request import BaseRequest
from webhook_cgi import process_once


class FakeRequest(BaseRequest):
    async def initialize(self):
        pass

    async def shutdown(self):
        pass

    async def do_request(self, url, method, **kwargs):
        if not url.endswith('/getMe'):
            raise AssertionError('Тест не должен отправлять сообщения или запрашивать ИИ')
        return 200, json.dumps({'ok': True, 'result': {
            'id': 123, 'is_bot': True, 'first_name': 'Test', 'username': 'test_bot',
        }}).encode()


class PersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_repeated_delivery_is_skipped_and_dialog_survives_restart(self):
        calls = []

        async def handler(update, context):
            calls.append(update.update_id)
            context.user_data.setdefault('chat_history', []).append(update.message.text)

        root = Path(__file__).parent / 'Temp'
        root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=root) as folder:
            for update_id in [42, 42, 43]:
                persistence = PicklePersistence(filepath=Path(folder) / 'state', on_flush=True)
                app = (Application.builder().bot(ExtBot('123:TEST', request=FakeRequest(),
                       get_updates_request=FakeRequest())).updater(None)
                       .persistence(persistence).build())
                app.add_handler(MessageHandler(filters.TEXT, handler))
                payload = {'update_id': update_id, 'message': {
                    'message_id': update_id, 'date': 1, 'text': 'hello',
                    'chat': {'id': 7, 'type': 'private'},
                    'from': {'id': 7, 'is_bot': False, 'first_name': 'Test'},
                }}
                await process_once(app, payload)
            self.assertEqual(calls, [42, 43])
            self.assertEqual(app.user_data[7]['chat_history'], ['hello', 'hello'])


class RequestTests(unittest.TestCase):
    def test_registration_preserves_pending_updates(self):
        from configure_webhook import register

        def handler(request):
            self.assertTrue(request.url.path.endswith('/setWebhook'))
            body = json.loads(request.content)
            self.assertFalse(body['drop_pending_updates'])
            self.assertEqual(body['max_connections'], 1)
            self.assertEqual(body['secret_token'], 'test-secret')
            return httpx.Response(200, json={'ok': True, 'result': True})

        client = httpx.Client(transport=httpx.MockTransport(handler))
        with patch('net_patch.install') as install, patch('configure_webhook.httpx.Client', return_value=client):
            register('https://example.com/webhook.py', '123:TEST', 'test-secret')
            install.assert_called_once_with(watchdog=False)

    def request(self, payload=None, **changes):
        body = json.dumps(payload if payload is not None else {'update_id': 42}).encode()
        env = {'REQUEST_METHOD': 'POST', 'CONTENT_LENGTH': str(len(body)),
               'CONTENT_TYPE': 'application/json',
               'HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN': 'test-secret'}
        env.update(changes)
        return env, io.BytesIO(body)

    def test_valid_update(self):
        self.assertEqual(read_update(*self.request(), 'test-secret'), {'update_id': 42})

    def test_rejects_unauthorized_and_unconfigured_requests(self):
        for secret, headers, status in [
            ('', {}, 503),
            ('test-secret', {'HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN': ''}, 403),
            ('test-secret', {'REQUEST_METHOD': 'GET'}, 405),
            ('test-secret', {'CONTENT_LENGTH': '9999999'}, 413),
            ('test-secret', {'CONTENT_LENGTH': '-1'}, 400),
            ('test-secret', {'CONTENT_TYPE': 'text/plain'}, 415),
        ]:
            with self.subTest(status=status), self.assertRaises(RequestError) as caught:
                read_update(*self.request(**headers), secret)
            self.assertEqual(caught.exception.status, status)

    def test_rejects_bad_json_and_missing_update_id(self):
        for body in [b'{', b'[]', b'{}', b'{"update_id":true}', b'{"update_id":-1}']:
            env, _ = self.request(CONTENT_LENGTH=str(len(body)))
            with self.assertRaises(RequestError):
                read_update(env, io.BytesIO(body), 'test-secret')


if __name__ == '__main__':
    unittest.main()
