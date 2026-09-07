"""Wire format shared by the plugin host and plugin worker processes.

Kept dependency-free (no Django, no third-party imports) so a plugin worker's
minimal environment can import it without pulling in the host application.
"""

from datetime import datetime
import json
import os
import select
import struct
import time

_LENGTH_STRUCT = struct.Struct('>I')


class ProtocolError(RuntimeError):
    """The peer sent a malformed message or the connection closed unexpectedly."""


class ProtocolTimeout(ProtocolError):
    """No complete message arrived before the deadline."""


def write_message(stream, message):
    """Write one JSON message to a binary stream, length-prefixed."""
    body = json.dumps(message, default=_json_default).encode('utf-8')
    stream.write(_LENGTH_STRUCT.pack(len(body)))
    stream.write(body)
    stream.flush()


def read_message(stream, timeout=None):
    """Read one length-prefixed JSON message from a binary stream.

    With `timeout` set, raises ProtocolTimeout if no complete message arrives
    within that many seconds (requires `stream` to support fileno()).
    Raises ProtocolError if the stream is closed before a full message arrives.
    """
    deadline = None if timeout is None else time.monotonic() + timeout
    header = _read_exact(stream, _LENGTH_STRUCT.size, deadline)
    (length,) = _LENGTH_STRUCT.unpack(header)
    body = _read_exact(stream, length, deadline)
    try:
        return _restore_datetimes(json.loads(body.decode('utf-8')))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProtocolError(f'Malformed plugin protocol message: {error}') from error


def _read_exact(stream, size, deadline=None):
    if size == 0:
        return b''
    if deadline is not None:
        return _read_exact_timed(stream, size, deadline)
    chunks = []
    remaining = size
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            raise ProtocolError('Plugin worker connection closed unexpectedly')
        chunks.append(chunk)
        remaining -= len(chunk)
    return b''.join(chunks)


def _read_exact_timed(stream, size, deadline):
    # select() reports readiness of the raw fd, so read via os.read to bypass
    # the stream's userspace buffer. A buffered stream.read() would pull the
    # rest of a message out of the pipe into the buffer, after which select()
    # sees an empty fd and blocks until the deadline even though the bytes have
    # already arrived. Callers that pass a deadline must never mix buffered
    # reads on the same stream.
    fileno = stream.fileno()
    chunks = []
    remaining = size
    while remaining:
        remaining_time = deadline - time.monotonic()
        if remaining_time <= 0:
            raise ProtocolTimeout('Timed out waiting for plugin worker message')
        ready, _, _ = select.select([fileno], [], [], remaining_time)
        if not ready:
            raise ProtocolTimeout('Timed out waiting for plugin worker message')
        chunk = os.read(fileno, remaining)
        if not chunk:
            raise ProtocolError('Plugin worker connection closed unexpectedly')
        chunks.append(chunk)
        remaining -= len(chunk)
    return b''.join(chunks)


def _json_default(value):
    if isinstance(value, datetime):
        return {'__type__': 'datetime', 'value': value.isoformat()}
    raise TypeError(f'Object of type {type(value).__name__} is not JSON serializable')


def _restore_datetimes(value):
    if isinstance(value, dict):
        if value.get('__type__') == 'datetime' and 'value' in value:
            return datetime.fromisoformat(value['value'])
        return {key: _restore_datetimes(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_restore_datetimes(item) for item in value]
    return value
