#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "datamodel-code-generator>=0.25.0",
#     "click>=8.1.0",
#     "requests>=2.31.0",
#     "pyyaml>=6.0",
# ]
# ///

"""
Immich Pydantic Model Generator

This tool generates Pydantic v2 models from the Immich OpenAPI specification.
It can fetch from URLs or local files and outputs comprehensive type-safe models.
"""

import json
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import click
import requests
import yaml


def fetch_spec(spec_path: str) -> tuple[str, bool]:
    """
    Fetch OpenAPI spec from URL or file path and return path to local file.

    Args:
        spec_path: URL or file path to OpenAPI spec

    Returns:
        Tuple of (path to local file containing the spec, is_temp_file)
    """
    # Check if it's a URL
    parsed = urlparse(spec_path)
    if parsed.scheme in ("http", "https"):
        # Handle GitHub raw URLs
        if "github.com" in parsed.netloc and "/blob/" in spec_path:
            # Convert GitHub blob URL to raw URL
            spec_path = spec_path.replace("github.com", "raw.githubusercontent.com")
            spec_path = spec_path.replace("/blob/", "/")

        try:
            print(f"Fetching spec from: {spec_path}")
            response = requests.get(spec_path, timeout=30)
            response.raise_for_status()

            # Parse content first to avoid creating temp file on parse failure
            try:
                spec_data = response.json()
            except json.JSONDecodeError:
                # Try YAML if JSON fails
                try:
                    spec_data = yaml.safe_load(response.text)
                except yaml.YAMLError as ye:
                    print(
                        f"Failed to parse spec as JSON or YAML: {ye}",
                        file=sys.stderr,
                    )
                    sys.exit(1)
            
            # Only create temp file after successful parsing
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".json", delete=False
            ) as f:
                json.dump(spec_data, f, indent=2)
                return f.name, True

        except requests.RequestException as e:
            print(f"Error fetching spec from {spec_path}: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        # Local file
        path = Path(spec_path)
        if not path.exists():
            print(f"File not found: {spec_path}", file=sys.stderr)
            sys.exit(1)
        return str(path), False


@click.command()
@click.option(
    "--immich-spec",
    default="immich.json",
    help="URL or path to Immich OpenAPI specification (default: immich.json)",
)
@click.option(
    "--output",
    default="routers/immich_models.py",
    help="Output file path for generated models (default: routers/immich_models.py)",
)
@click.option(
    "--verbose",
    is_flag=True,
    help="Show detailed output including command and subprocess stdout/stderr",
)
def main(immich_spec: str, output: str, verbose: bool):
    """Generate Pydantic v2 models from Immich OpenAPI specification."""
    # Resolve paths relative to current working directory for consistency
    output_file = Path(output).resolve()

    # Fetch the spec (returns local file path and temp flag)
    spec_file, is_temp_file = fetch_spec(immich_spec)

    try:
        # Ensure output directory exists
        output_file.parent.mkdir(parents=True, exist_ok=True)

        # Generate models using datamodel-code-generator
        cmd = [
            "datamodel-codegen",
            "--input",
            spec_file,
            "--input-file-type",
            "openapi",
            "--output-model-type",
            "pydantic_v2.BaseModel",
            "--field-constraints",
            "--use-annotated",
            "--set-default-enum-member",
            "--output",
            str(output_file),
        ]

        if verbose:
            print("Generating Pydantic models...")
            print(f"Command: {' '.join(cmd)}")
        else:
            print("Generating Pydantic models...")

        try:
            result = subprocess.run(cmd, check=True, capture_output=True, text=True)
            print(f"✓ Successfully generated models to {output_file}")

            if verbose and result.stdout:
                print("STDOUT:", result.stdout)
            if verbose and result.stderr:
                print("STDERR:", result.stderr)

        except FileNotFoundError:
            print(
                "datamodel-codegen not found on PATH. Install it or run via 'uv run' "
                "so inline dependencies are available.",
                file=sys.stderr,
            )
            sys.exit(1)
        except subprocess.CalledProcessError as e:
            print(f"Error generating models: {e}", file=sys.stderr)
            if e.stdout:
                print("STDOUT:", e.stdout, file=sys.stderr)
            if e.stderr:
                print("STDERR:", e.stderr, file=sys.stderr)
            sys.exit(1)

    finally:
        # Clean up temporary file if it was created
        if is_temp_file and Path(spec_file).exists():
            Path(spec_file).unlink()


if __name__ == "__main__":
    main()
