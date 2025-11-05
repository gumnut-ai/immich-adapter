"""Custom StaticFiles handler for Single Page Applications with precompressed file support."""

from fastapi.staticfiles import StaticFiles
from starlette.responses import FileResponse
from starlette.exceptions import HTTPException
from starlette.datastructures import Headers
import os
import mimetypes


class SPAStaticFiles(StaticFiles):
    """
    Serves static files with SPA fallback and precompressed file support.

    - Serves precompiled .br/.gz files when client supports them
    - Falls back to index.html for missing files (SPA routing)
    """

    def _find_compressed_file(
        self, file_path: str, accept_encoding: str
    ) -> tuple[str | None, str | None]:
        """
        Find compressed version of file if available and supported by client.

        Returns: (compressed_path, encoding) or (None, None)
        """
        encodings = accept_encoding.lower()

        # Prefer brotli (better compression)
        if "br" in encodings and os.path.exists(f"{file_path}.br"):
            return f"{file_path}.br", "br"

        if "gzip" in encodings and os.path.exists(f"{file_path}.gz"):
            return f"{file_path}.gz", "gzip"

        return None, None

    def _get_content_type(self, original_path: str) -> str:
        """Get MIME type from original path (ignoring .br/.gz extensions)."""
        content_type, _ = mimetypes.guess_type(original_path)
        return content_type or "application/octet-stream"

    async def _serve_file(self, file_path: str, accept_encoding: str) -> FileResponse:
        """Serve a file, using compressed version if available."""
        compressed_path, encoding = self._find_compressed_file(
            file_path, accept_encoding
        )

        headers: dict[str, str] = {}
        # Immutable files in _app/immutable get aggressive caching (1 year)
        if "/_app/immutable" in file_path:
            headers = {"Cache-Control": "public,max-age=31536000,immutable"}

        if compressed_path and encoding:
            headers["Content-Encoding"] = encoding
            headers["Vary"] = "Accept-Encoding"

            return FileResponse(
                path=compressed_path,
                media_type=self._get_content_type(file_path),
                headers=headers,
            )

        # Serve uncompressed file
        return FileResponse(
            path=file_path,
            media_type=self._get_content_type(file_path),
            headers=headers,
        )

    async def get_response(self, path: str, scope):
        """Serve file with compression support, or index.html for SPA routes."""
        if not self.directory:
            raise HTTPException(
                status_code=500, detail="Static files directory not configured"
            )

        directory = str(self.directory)
        full_path = os.path.join(directory, path)
        accept_encoding = Headers(scope=scope).get("accept-encoding", "")

        # If requested file exists, serve it (possibly compressed)
        if os.path.isfile(full_path):
            return await self._serve_file(full_path, accept_encoding)

        # File not found - serve index.html for SPA routing
        index_path = os.path.join(directory, "index.html")
        if os.path.isfile(index_path):
            return await self._serve_file(index_path, accept_encoding)

        # No file and no index.html - return 404
        raise HTTPException(status_code=404, detail="Not found")
