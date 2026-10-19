import tempfile
import unittest
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer
from wifi_app.server import create_app
from wifi_app.transfer import Limits, TransferManager


class HTTPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.manager = TransferManager(Path(self.temp.name), Limits(1, 2, 10, 0))
        self.server = TestServer(create_app(self.manager))
        self.client = TestClient(self.server)
        await self.client.start_server()
        self.headers = {'X-Session-Token': self.manager.token}

    async def asyncTearDown(self):
        await self.client.close()
        self.manager.close()
        self.temp.cleanup()

    async def test_upload_pause_resume_finish_via_http(self):
        res = await self.client.get('/?token=' + self.manager.token)
        self.assertEqual(res.status, 200)
        self.assertIn('آنی', await res.text())
        res = await self.client.post('/api/files', headers=self.headers,
                                     json={'name':'data.bin','size':6,'key':'unique-key'})
        self.assertEqual(res.status, 200)
        fid = (await res.json())['id']
        res = await self.client.put(f'/api/files/{fid}/chunks/0', headers=self.headers, data=b'ABCDEF')
        self.assertEqual(res.status, 200)
        res = await self.client.get(f'/api/files/{fid}', headers=self.headers)
        self.assertEqual((await res.json())['received'], [0])
        res = await self.client.post(f'/api/files/{fid}/complete', headers=self.headers)
        self.assertEqual(res.status, 200)
        self.assertEqual((self.manager.folder / 'data.bin').read_bytes(), b'ABCDEF')

    async def test_bad_auth_cross_origin_and_cancellation(self):
        res = await self.client.get('/api/session')
        self.assertEqual(res.status, 403)
        res = await self.client.get('/api/session', headers={**self.headers,'Origin':'http://malicious.invalid'})
        self.assertEqual(res.status, 403)
        res = await self.client.post('/api/files', headers=self.headers,
                                     json={'name':'abort','size':6,'key':'abort'})
        fid = (await res.json())['id']
        res = await self.client.delete(f'/api/files/{fid}', headers=self.headers)
        self.assertEqual(res.status, 200)
        res = await self.client.put(f'/api/files/{fid}/chunks/0', headers=self.headers, data=b'ABCDEF')
        self.assertEqual(res.status, 409)

    async def test_oversized_payload_is_rejected(self):
        res = await self.client.post('/api/files', headers=self.headers,
                                     json={'name':'short','size':1,'key':'short'})
        fid = (await res.json())['id']
        res = await self.client.put(f'/api/files/{fid}/chunks/0', headers=self.headers, data=b'123')
        self.assertEqual(res.status, 400)


if __name__ == '__main__':
    unittest.main()
