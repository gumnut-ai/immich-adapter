"""Thread-safe pipe for streaming upload data between producer and consumer threads.

Used by the streaming upload path to bridge the multipart parser (producer) and
httpx sync upload (consumer) without buffering the entire file to disk or memory.
"""

import queue
from io import RawIOBase

# Timeout in seconds for blocking operations. Prevents permanent hangs if either
# the parser or upload thread dies unexpectedly.
_STALL_TIMEOUT = 300  # 5 minutes

# Short timeout for retry loops so errors are observed quickly.
_POLL_INTERVAL = 1.0


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
        self._writer_closed = False
        self._eof = False

    def put(self, data: bytes) -> None:
        """Feed data into the pipe. Called by parser callbacks.

        Uses a short timeout loop so set_error() is observed quickly rather
        than blocking for the full stall timeout.
        """
        elapsed = 0.0
        while True:
            if self._error:
                raise self._error
            try:
                self._queue.put(data, timeout=_POLL_INTERVAL)
                break
            except queue.Full:
                elapsed += _POLL_INTERVAL
                if elapsed >= _STALL_TIMEOUT:
                    raise TimeoutError(
                        f"Upload pipe stalled — queue full for {_STALL_TIMEOUT}s"
                    )
        if self._error:
            raise self._error

    def close_writer(self) -> None:
        """Signal EOF from the writer side.

        Retries with short timeout until the sentinel is enqueued, checking
        for errors between attempts so we don't block indefinitely.
        """
        if self._writer_closed:
            return
        self._writer_closed = True
        elapsed = 0.0
        while True:
            if self._error:
                return
            try:
                self._queue.put(None, timeout=_POLL_INTERVAL)
                return
            except queue.Full:
                elapsed += _POLL_INTERVAL
                if elapsed >= _STALL_TIMEOUT:
                    raise TimeoutError(
                        f"Upload pipe stalled — queue full for {_STALL_TIMEOUT}s"
                    )

    def set_error(self, error: BaseException) -> None:
        """Propagate an error from either side, unblocking the other.

        Drains the queue to free space so a blocked producer can complete
        and observe the error.
        """
        self._error = error
        try:
            while True:
                self._queue.get_nowait()
        except queue.Empty:
            pass
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
        if self._eof:
            return 0

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
            # If we were unblocked due to set_error(), raise instead of EOF.
            if self._error:
                raise self._error
            self._eof = True
            return 0

        if self._error:
            raise self._error

        n = min(len(b), len(data))
        b[:n] = data[:n]
        if len(data) > n:
            self._leftover = data[n:]
        return n

    def readable(self) -> bool:
        return True
