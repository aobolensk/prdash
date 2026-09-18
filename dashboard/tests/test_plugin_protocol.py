import io
import struct
from unittest.mock import patch

from django.test import SimpleTestCase

from prdash.plugin_protocol import ProtocolError, read_message, write_message


class PluginProtocolFrameSizeTests(SimpleTestCase):
    def test_read_rejects_oversized_frame_before_reading_body(self):
        stream = io.BytesIO(struct.pack('>I', 4))

        with patch('prdash.plugin_protocol.MAX_FRAME_SIZE', 3):
            with self.assertRaises(ProtocolError):
                read_message(stream)

        self.assertEqual(stream.tell(), struct.calcsize('>I'))

    def test_write_rejects_oversized_frame_before_writing(self):
        stream = io.BytesIO()

        with patch('prdash.plugin_protocol.MAX_FRAME_SIZE', 3):
            with self.assertRaises(ProtocolError):
                write_message(stream, {'value': 'too large'})

        self.assertEqual(stream.getvalue(), b'')
