"""Tests for the StreamingPipe utility."""

import threading

import pytest

from utils.streaming_pipe import StreamingPipe


class TestStreamingPipe:
    def test_put_and_read_basic(self):
        """Test basic put/read data flow."""
        pipe = StreamingPipe(maxsize=4)
        pipe.put(b"hello")
        pipe.put(b" world")
        pipe.close_writer()

        result = pipe.read(1024)
        assert result == b"hello"
        result = pipe.read(1024)
        assert result == b" world"
        result = pipe.read(1024)
        assert result == b""  # EOF

    def test_readinto_partial(self):
        """Test readinto with a buffer smaller than the chunk."""
        pipe = StreamingPipe(maxsize=4)
        pipe.put(b"hello world")
        pipe.close_writer()

        buf = bytearray(5)
        n = pipe.readinto(buf)
        assert n == 5
        assert buf == b"hello"

        # Leftover should be served next
        buf2 = bytearray(10)
        n2 = pipe.readinto(buf2)
        assert n2 == 6
        assert buf2[:n2] == b" world"

    def test_eof_returns_zero(self):
        """Test that readinto returns 0 on EOF."""
        pipe = StreamingPipe(maxsize=4)
        pipe.close_writer()

        buf = bytearray(10)
        assert pipe.readinto(buf) == 0

    def test_error_propagation_to_reader(self):
        """Test that set_error causes read to raise."""
        pipe = StreamingPipe(maxsize=4)
        pipe.set_error(ValueError("test error"))

        with pytest.raises(ValueError, match="test error"):
            pipe.read(1024)

    def test_error_propagation_to_writer(self):
        """Test that set_error causes put to raise."""
        pipe = StreamingPipe(maxsize=4)
        pipe.set_error(ValueError("test error"))

        with pytest.raises(ValueError, match="test error"):
            pipe.put(b"data")

    def test_concurrent_put_and_read(self):
        """Test concurrent producer/consumer threads."""
        pipe = StreamingPipe(maxsize=4)
        chunks = [b"chunk1", b"chunk2", b"chunk3"]
        received = []

        def producer():
            for chunk in chunks:
                pipe.put(chunk)
            pipe.close_writer()

        def consumer():
            while True:
                data = pipe.read(1024)
                if not data:
                    break
                received.append(data)

        t1 = threading.Thread(target=producer)
        t2 = threading.Thread(target=consumer)
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

        assert received == chunks

    def test_readable(self):
        pipe = StreamingPipe()
        assert pipe.readable() is True
