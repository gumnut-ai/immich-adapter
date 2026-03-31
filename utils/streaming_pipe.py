"""Thread-safe pipe for streaming upload data between producer and consumer threads.

Used by the streaming upload path to bridge the multipart parser (producer) and
httpx sync upload (consumer) without buffering the entire file to disk or memory.
"""

import queue
from io import RawIOBase

# Timeout in seconds for blocking operations. Prevents permanent hangs if either
# the parser or upload thread dies unexpectedly.
_STALL_TIMEOUT = 300  # 5 minutes


class StreamingPipe(RawIOBase):
    """Thread-safe pipe bridging multipart parser to httpx upload.

    The parser thread calls put() to feed file data chunks.
    The upload thread calls read()/readinto() to consume data.
    queue.Queue provides backpressure and thread safety.

    Args:
        maxsize: Maximum number of chunks in the queue. Each chunk is typically
            64KB (python-multipart default), so maxsize=64 ≈ 4MB buffer.
    """

    def __init__(self, maxsize: int = 64) -> None:
        super().__init__()
        self._queue: queue.Queue[bytes | None] = queue.Queue(maxsize=maxsize)
        self._error: BaseException | None = None
        self._leftover = b""

    def put(self, data: bytes) -> None:
        """Feed data into the pipe. Called by parser callbacks.

        Blocks if the queue is full (backpressure).
        Raises if an error has been set from either side.
        """
        if self._error:
            raise self._error
        try:
            self._queue.put(data, timeout=_STALL_TIMEOUT)
        except queue.Full:
            raise TimeoutError(
                f"Upload pipe stalled — queue full for {_STALL_TIMEOUT}s"
            )

    def close_writer(self) -> None:
        """Signal EOF from the writer side."""
        try:
            self._queue.put(None, timeout=10)
        except queue.Full:
            pass

    def set_error(self, error: BaseException) -> None:
        """Propagate an error from either side, unblocking the other."""
        self._error = error
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass

    def readinto(self, b: bytearray | memoryview) -> int:  # type: ignore[override]
        """Read data into buffer. Called by httpx via RawIOBase protocol.

        Blocks until data is available or EOF/error is signaled.
        """
        if self._error:
            raise self._error

        # Serve leftover from a previous partial read first
        if self._leftover:
            n = min(len(b), len(self._leftover))
            b[:n] = self._leftover[:n]
            self._leftover = self._leftover[n:]
            return n

        try:
            data = self._queue.get(timeout=_STALL_TIMEOUT)
        except queue.Empty:
            raise TimeoutError(f"Upload pipe stalled — no data for {_STALL_TIMEOUT}s")

        if data is None:
            return 0  # EOF

        if self._error:
            raise self._error

        n = min(len(b), len(data))
        b[:n] = data[:n]
        if len(data) > n:
            self._leftover = data[n:]
        return n

    def readable(self) -> bool:
        return True
