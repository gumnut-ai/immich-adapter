#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "fastapi[standard]>=0.116.1",
#     "pydantic-settings>=2.10.1",
#     "python-socketio>=5.13.0",
#     "sentry-sdk[fastapi]>=2.37.1",
#     "gumnut-sdk",
#     "b64uuid>=0.1",
#     "shortuuid>=1.0.13",
# ]
# ///

"""
OpenAPI JSON Dumper

This tool dumps the OpenAPI specification from the FastAPI app to stdout.
Useful for debugging and comparing the generated OpenAPI spec.
"""

import json
import sys
from pathlib import Path

# Add the parent directory to the path so we can import from the main app
sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    from main import app
except ImportError as e:
    print(f"Error importing main app: {e}", file=sys.stderr)
    print(
        "Make sure you're running this from the project root directory", file=sys.stderr
    )
    sys.exit(1)


def main():
    """Dump the OpenAPI specification to stdout."""
    try:
        # Generate the OpenAPI spec from the FastAPI app
        openapi_spec = app.openapi()

        # Pretty print to stdout
        json.dump(openapi_spec, sys.stdout, indent=2)
        print()  # Add a newline at the end

    except Exception as e:
        print(f"Error generating OpenAPI spec: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
