import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from wifi_app.transfer import CHUNK_SIZE, Limits, TransferError, TransferManager, safe_name


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.manager = TransferManager(Path(self.temp.name), Limits(2, 3, 15, 0))
        self.token = self.manager.token

    def tearDown(self):
        self.manager.close()
        self.temp.cleanup()

    def test_filename_is_local_and_unicode_safe(self):
        self.assertEqual(safe_name('C:\\private\\تصویر.jpg'), 'تصویر.jpg')
        self.assertEqual(safe_name('../../نام.txt'), 'نام.txt')
        self.assertEqual(safe_name('CON.txt'), '_CON.txt')
        with self.assertRaises(TransferError):
            safe_name('..')

    def test_resumable_out_of_order_and_duplicate_chunks(self):
        first = b'Z' * CHUNK_SIZE
        tail = b'persian'
        initial = self.manager.init_file(self.token, 'آزمایش.bin', len(first) + len(tail), 'stable-file-key')
        file_id = initial['id']
        self.manager.write_chunk(self.token, file_id, 1, tail, hashlib.sha256(tail).hexdigest())
        info = self.manager.init_file(self.token, 'آزمایش.bin', len(first) + len(tail), 'stable-file-key')
        self.assertEqual(info['received'], [1])
        with self.assertRaises(TransferError):
            self.manager.finish(self.token, file_id)
        with self.assertRaises(TransferError):
            self.manager.write_chunk(self.token, file_id, 0, first, 'bad')
        self.manager.write_chunk(self.token, file_id, 0, first)
        self.manager.write_chunk(self.token, file_id, 0, first)
        self.manager.finish(self.token, file_id)
        self.assertEqual((self.manager.folder / 'آزمایش.bin').read_bytes(), first + tail)
        self.assertEqual(self.manager.finish(self.token, file_id)['status'], 'complete')

    def test_zero_bytes_and_duplicate_file_names(self):
        for n in range(2):
            info = self.manager.init_file(self.token, 'سلام.txt', 0, f'key-{n}')
            self.manager.write_chunk(self.token, info['id'], 0, b'')
            self.manager.finish(self.token, info['id'])
        self.assertTrue((self.manager.folder / 'سلام.txt').exists())
        self.assertTrue((self.manager.folder / 'سلام (2).txt').exists())

    def test_cancel_releases_temp_and_ends_session(self):
        info = self.manager.init_file(self.token, 'file.txt', 3, 'one')
        self.manager.write_chunk(self.token, info['id'], 0, b'abc')
        self.manager.cancel(self.token, info['id'])
        self.assertEqual(list(self.manager.folder.glob('.part-*')), [])
        info2 = self.manager.init_file(self.token, 'keep.txt', 4, 'two')
        self.manager.write_chunk(self.token, info2['id'], 0, b'keep')
        self.manager.finish(self.token, info2['id'])
        path = self.manager.folder
        self.manager.new_session()
        self.assertEqual((path / 'keep.txt').read_bytes(), b'keep')
        with self.assertRaises(TransferError):
            self.manager.init_file(self.token, 'bad.txt', 1, 'old')

    def test_limits_and_invalid_tokens(self):
        with self.assertRaises(TransferError):
            self.manager.init_file('wrong', 'bad.txt', 1, 'key')
        with self.assertRaises(TransferError):
            self.manager.init_file(self.token, 'huge.dat', 3 * 1024**3, 'huge')
        with self.assertRaises(TransferError):
            self.manager.init_file(self.token, 'x', -1, 'neg')
        with patch('wifi_app.transfer.shutil.disk_usage') as disk:
            disk.return_value.free = 30
            with self.assertRaises(TransferError):
                self.manager.init_file(self.token, 'x', 50, 'size')

    def test_incomplete_cleanup_when_new_customer_starts(self):
        info = self.manager.init_file(self.token, 'unfinished.dat', 9, 'one')
        self.manager.write_chunk(self.token, info['id'], 0, b'123456789')
        path = self.manager.folder
        self.manager.new_session()
        self.assertFalse(path.exists())


if __name__ == '__main__':
    unittest.main()
