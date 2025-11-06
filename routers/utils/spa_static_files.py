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

        headers["Vary"] = "Accept-Encoding"
        if compressed_path and encoding:
            headers["Content-Encoding"] = encoding

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

        headers_obj = Headers(scope=scope)
        accept_encoding = headers_obj.get("accept-encoding", "")
        method = scope.get("method", "GET")
        accept = headers_obj.get("accept", "")

        # Securely resolve the requested path using StaticFiles' lookup_path
        full_path, stat_result = self.lookup_path(path)

        # If file exists, serve it (possibly compressed)
        if stat_result is not None:
            return await self._serve_file(full_path, accept_encoding)

        # SPA fallback only for GET/HEAD and when HTML is acceptable or path has no extension
        # This prevents serving index.html for missing CSS/JS/image files
        if method in ("GET", "HEAD") and (
            "text/html" in accept or "." not in path.split("/")[-1]
        ):
            index_path, index_stat = self.lookup_path("index.html")
            if index_stat is not None:
                return await self._serve_file(index_path, accept_encoding)

        # No file and no SPA fallback - return 404
        raise HTTPException(status_code=404, detail="Not found")
