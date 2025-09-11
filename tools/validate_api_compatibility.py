#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "click>=8.1.0",
#     "requests>=2.31.0",
#     "deepdiff>=6.7.0",
#     "pyyaml>=6.0",
#     "rich>=13.0.0",
# ]
# ///

"""
OpenAPI Specification Compatibility Validator

This tool compares OpenAPI specifications between Immich and immich-adapter
to ensure API compatibility.
"""

import json
import sys
from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import urlparse

import click
import requests
import yaml
from deepdiff import DeepDiff
from rich.console import Console
from rich.table import Table
from rich.text import Text

console = Console()


class SpecFetcher:
    """Handles fetching OpenAPI specs from URLs or local files."""

    @staticmethod
    def fetch(spec_path: str) -> Dict[str, Any]:
        """
        Fetch OpenAPI spec from URL or file path.

        Args:
            spec_path: URL or file path to OpenAPI spec

        Returns:
            Parsed OpenAPI specification
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
                response = requests.get(spec_path, timeout=30)
                response.raise_for_status()

                # Try to parse as JSON first
                try:
                    return response.json()
                except json.JSONDecodeError:
                    # Try YAML if JSON fails
                    return yaml.safe_load(response.text)
            except requests.RequestException as e:
                console.print(f"[red]Error fetching spec from {spec_path}: {e}[/red]")
                sys.exit(1)
        else:
            # Local file
            path = Path(spec_path)
            if not path.exists():
                console.print(f"[red]File not found: {spec_path}[/red]")
                sys.exit(1)

            try:
                with open(path, "r") as f:
                    if path.suffix.lower() in [".yaml", ".yml"]:
                        return yaml.safe_load(f)
                    else:
                        return json.load(f)
            except (json.JSONDecodeError, yaml.YAMLError) as e:
                console.print(f"[red]Error parsing spec from {spec_path}: {e}[/red]")
                sys.exit(1)


class EndpointFilter:
    """Filters OpenAPI specs to specific endpoints."""

    @staticmethod
    def filter_spec(spec: Dict[str, Any], endpoints: List[str]) -> Dict[str, Any]:
        """
        Filter OpenAPI spec to only include specified endpoints.

        Args:
            spec: Complete OpenAPI specification
            endpoints: List of endpoint prefixes to include

        Returns:
            Filtered OpenAPI specification
        """
        if not endpoints:
            return spec

        filtered_spec = spec.copy()
        filtered_paths = {}

        # Convert endpoint names to path prefixes
        # Handle both with and without /api prefix
        path_prefixes = []
        for endpoint in endpoints:
            # Clean endpoint name
            endpoint = endpoint.strip().lower()
            # Add common variations
            path_prefixes.extend(
                [
                    f"/api/{endpoint}",
                    f"/api/{endpoint}/",
                    f"/{endpoint}",
                    f"/{endpoint}/",
                ]
            )

        # Filter paths
        for path, path_data in spec.get("paths", {}).items():
            for prefix in path_prefixes:
                if path.lower().startswith(prefix):
                    filtered_paths[path] = path_data
                    break

        filtered_spec["paths"] = filtered_paths
        return filtered_spec


class SpecComparator:
    """Compares two OpenAPI specifications."""

    def __init__(self, immich_spec: Dict[str, Any], adapter_spec: Dict[str, Any]):
        self.immich_spec = immich_spec
        self.adapter_spec = adapter_spec
        self.differences = []

    def _normalize_path(self, path: str) -> str:
        """
        Normalize path by removing /api prefix if present.
        This allows comparison between Immich paths (without /api)
        and adapter paths (with /api).
        """
        if path.startswith("/api/"):
            return path[4:]  # Remove '/api' prefix
        return path

    def _format_deepdiff(self, diff: DeepDiff) -> str:
        """
        Format DeepDiff output for readable display.
        """
        parts = []

        if "values_changed" in diff:
            for key, change in diff["values_changed"].items():
                # Extract clean key name
                clean_key = key.split("['")[-1].rstrip("']")
                old_val = change.get("old_value", "N/A")
                new_val = change.get("new_value", "N/A")
                parts.append(f"'{clean_key}': {old_val} → {new_val}")

        if "type_changes" in diff:
            for key, change in diff["type_changes"].items():
                clean_key = key.split("['")[-1].rstrip("']")
                old_type = (
                    change.get("old_type", "N/A").__name__
                    if hasattr(change.get("old_type"), "__name__")
                    else str(change.get("old_type"))
                )
                new_type = (
                    change.get("new_type", "N/A").__name__
                    if hasattr(change.get("new_type"), "__name__")
                    else str(change.get("new_type"))
                )
                parts.append(f"'{clean_key}' type: {old_type} → {new_type}")

        if "dictionary_item_added" in diff:
            added_keys = [
                k.split("['")[-1].rstrip("']") for k in diff["dictionary_item_added"]
            ]
            if added_keys:
                parts.append(f"Added in adapter: {', '.join(added_keys)}")

        if "dictionary_item_removed" in diff:
            removed_keys = [
                k.split("['")[-1].rstrip("']") for k in diff["dictionary_item_removed"]
            ]
            if removed_keys:
                parts.append(f"Missing in adapter: {', '.join(removed_keys)}")

        if "iterable_item_added" in diff:
            parts.append(f"Items added: {len(diff['iterable_item_added'])}")

        if "iterable_item_removed" in diff:
            parts.append(f"Items removed: {len(diff['iterable_item_removed'])}")

        if not parts:
            # Fallback to string representation if we couldn't parse it
            return str(diff)[:500]

        return "; ".join(parts)

    def compare(self) -> List[Dict[str, Any]]:
        """
        Compare the two specifications.

        Returns:
            List of differences found
        """
        self.differences = []

        # Get paths from both specs and create normalized mappings
        immich_paths_raw = self.immich_spec.get("paths", {})
        adapter_paths_raw = self.adapter_spec.get("paths", {})

        # Create normalized path mappings
        immich_paths_normalized = {
            self._normalize_path(p): p for p in immich_paths_raw.keys()
        }
        adapter_paths_normalized = {
            self._normalize_path(p): p for p in adapter_paths_raw.keys()
        }

        # Compare normalized paths
        immich_normalized_set = set(immich_paths_normalized.keys())
        adapter_normalized_set = set(adapter_paths_normalized.keys())

        # Find missing and extra paths
        missing_paths = immich_normalized_set - adapter_normalized_set
        extra_paths = adapter_normalized_set - immich_normalized_set
        common_paths = immich_normalized_set & adapter_normalized_set

        for normalized_path in missing_paths:
            actual_path = immich_paths_normalized[normalized_path]
            self.differences.append(
                {
                    "type": "missing_endpoint",
                    "severity": "error",
                    "path": actual_path,
                    "message": "Endpoint exists in Immich but not in adapter",
                }
            )

        for normalized_path in extra_paths:
            actual_path = adapter_paths_normalized[normalized_path]
            self.differences.append(
                {
                    "type": "extra_endpoint",
                    "severity": "warning",
                    "path": actual_path,
                    "message": "Endpoint exists in adapter but not in Immich",
                }
            )

        # Compare common paths
        for normalized_path in common_paths:
            immich_actual_path = immich_paths_normalized[normalized_path]
            adapter_actual_path = adapter_paths_normalized[normalized_path]
            self._compare_path_normalized(immich_actual_path, adapter_actual_path)

        return self.differences

    def _compare_path_normalized(self, immich_path_key: str, adapter_path_key: str):
        """Compare specific paths between the two specs, handling different path prefixes."""
        immich_path = self.immich_spec["paths"][immich_path_key]
        adapter_path = self.adapter_spec["paths"][adapter_path_key]

        # Get methods from both
        immich_methods = set(
            k
            for k in immich_path.keys()
            if k in ["get", "post", "put", "delete", "patch", "head", "options"]
        )
        adapter_methods = set(
            k
            for k in adapter_path.keys()
            if k in ["get", "post", "put", "delete", "patch", "head", "options"]
        )

        missing_methods = immich_methods - adapter_methods
        extra_methods = adapter_methods - immich_methods
        common_methods = immich_methods & adapter_methods

        for method in missing_methods:
            self.differences.append(
                {
                    "type": "missing_method",
                    "severity": "error",
                    "path": immich_path_key,
                    "method": method.upper(),
                    "message": f"Method {method.upper()} exists in Immich but not in adapter",
                }
            )

        for method in extra_methods:
            self.differences.append(
                {
                    "type": "extra_method",
                    "severity": "warning",
                    "path": adapter_path_key,
                    "method": method.upper(),
                    "message": f"Method {method.upper()} exists in adapter but not in Immich",
                }
            )

        # Compare common methods
        for method in common_methods:
            self._compare_method(immich_path_key, adapter_path_key, method)

    def _compare_method(self, immich_path_key: str, adapter_path_key: str, method: str):
        """Compare a specific method of a path."""
        immich_method = self.immich_spec["paths"][immich_path_key][method]
        adapter_method = self.adapter_spec["paths"][adapter_path_key][method]

        # Use the immich path for display (it's the canonical one)
        display_path = immich_path_key

        # Compare parameters
        self._compare_parameters(display_path, method, immich_method, adapter_method)

        # Compare request body
        self._compare_request_body(display_path, method, immich_method, adapter_method)

        # Compare responses
        self._compare_responses(display_path, method, immich_method, adapter_method)

    def _compare_parameters(
        self, path: str, method: str, immich_method: Dict, adapter_method: Dict
    ):
        """Compare parameters between methods."""
        immich_params = immich_method.get("parameters", [])
        adapter_params = adapter_method.get("parameters", [])

        # Create parameter maps by name and location
        immich_param_map = {(p.get("name"), p.get("in")): p for p in immich_params}
        adapter_param_map = {(p.get("name"), p.get("in")): p for p in adapter_params}

        missing_params = set(immich_param_map.keys()) - set(adapter_param_map.keys())
        extra_params = set(adapter_param_map.keys()) - set(immich_param_map.keys())
        common_params = set(immich_param_map.keys()) & set(adapter_param_map.keys())

        for param_key in missing_params:
            param = immich_param_map[param_key]
            severity = "error" if param.get("required", False) else "warning"
            self.differences.append(
                {
                    "type": "missing_parameter",
                    "severity": severity,
                    "path": path,
                    "method": method.upper(),
                    "parameter": param_key[0],
                    "location": param_key[1],
                    "message": f"Parameter '{param_key[0]}' in {param_key[1]} missing in adapter",
                }
            )

        for param_key in extra_params:
            self.differences.append(
                {
                    "type": "extra_parameter",
                    "severity": "info",
                    "path": path,
                    "method": method.upper(),
                    "parameter": param_key[0],
                    "location": param_key[1],
                    "message": f"Parameter '{param_key[0]}' in {param_key[1]} exists in adapter but not in Immich",
                }
            )

        # Compare common parameters
        for param_key in common_params:
            immich_param = immich_param_map[param_key]
            adapter_param = adapter_param_map[param_key]

            # Check if required status matches
            if immich_param.get("required", False) != adapter_param.get(
                "required", False
            ):
                self.differences.append(
                    {
                        "type": "parameter_mismatch",
                        "severity": "warning",
                        "path": path,
                        "method": method.upper(),
                        "parameter": param_key[0],
                        "location": param_key[1],
                        "message": f"Parameter '{param_key[0]}' required status mismatch",
                    }
                )

            # Check schema differences
            if "schema" in immich_param and "schema" in adapter_param:
                schema_diff = DeepDiff(
                    immich_param["schema"],
                    adapter_param["schema"],
                    ignore_order=True,
                    exclude_regex_paths=[
                        r".*\['description'\]",
                        r".*\['title'\]",
                        r".*\['example'\]",
                    ],
                )
                if schema_diff:
                    details = self._format_deepdiff(schema_diff)
                    self.differences.append(
                        {
                            "type": "parameter_schema_mismatch",
                            "severity": "warning",
                            "path": path,
                            "method": method.upper(),
                            "parameter": param_key[0],
                            "location": param_key[1],
                            "message": f"Parameter '{param_key[0]}' schema differs",
                            "details": details,
                        }
                    )

    def _compare_request_body(
        self, path: str, method: str, immich_method: Dict, adapter_method: Dict
    ):
        """Compare request body between methods."""
        immich_body = immich_method.get("requestBody")
        adapter_body = adapter_method.get("requestBody")

        if immich_body and not adapter_body:
            severity = "error" if immich_body.get("required", False) else "warning"
            self.differences.append(
                {
                    "type": "missing_request_body",
                    "severity": severity,
                    "path": path,
                    "method": method.upper(),
                    "message": "Request body exists in Immich but not in adapter",
                }
            )
        elif adapter_body and not immich_body:
            self.differences.append(
                {
                    "type": "extra_request_body",
                    "severity": "warning",
                    "path": path,
                    "method": method.upper(),
                    "message": "Request body exists in adapter but not in Immich",
                }
            )
        elif immich_body and adapter_body:
            # Compare content types
            immich_content = immich_body.get("content", {})
            adapter_content = adapter_body.get("content", {})

            missing_content_types = set(immich_content.keys()) - set(
                adapter_content.keys()
            )
            for content_type in missing_content_types:
                self.differences.append(
                    {
                        "type": "missing_content_type",
                        "severity": "warning",
                        "path": path,
                        "method": method.upper(),
                        "content_type": content_type,
                        "message": f"Content type '{content_type}' missing in adapter request body",
                    }
                )

            # Compare schemas for common content types
            common_content_types = set(immich_content.keys()) & set(
                adapter_content.keys()
            )
            for content_type in common_content_types:
                immich_schema = immich_content[content_type].get("schema", {})
                adapter_schema = adapter_content[content_type].get("schema", {})

                schema_diff = DeepDiff(
                    immich_schema,
                    adapter_schema,
                    ignore_order=True,
                    exclude_regex_paths=[
                        r".*\['description'\]",
                        r".*\['title'\]",
                        r".*\['example'\]",
                    ],
                )
                if schema_diff:
                    details = self._format_deepdiff(schema_diff)
                    self.differences.append(
                        {
                            "type": "request_body_schema_mismatch",
                            "severity": "warning",
                            "path": path,
                            "method": method.upper(),
                            "content_type": content_type,
                            "message": f"Request body schema differs for '{content_type}'",
                            "details": details,
                        }
                    )

    def _compare_responses(
        self, path: str, method: str, immich_method: Dict, adapter_method: Dict
    ):
        """Compare responses between methods."""
        immich_responses = immich_method.get("responses", {})
        adapter_responses = adapter_method.get("responses", {})

        # Compare status codes
        immich_statuses = set(immich_responses.keys())
        adapter_statuses = set(adapter_responses.keys())

        missing_statuses = immich_statuses - adapter_statuses

        # Only report missing success status codes as errors
        for status in missing_statuses:
            if status.startswith("2"):  # Success responses
                self.differences.append(
                    {
                        "type": "missing_response_status",
                        "severity": "error",
                        "path": path,
                        "method": method.upper(),
                        "status": status,
                        "message": f"Response status '{status}' missing in adapter",
                    }
                )
            else:  # Error responses are less critical
                self.differences.append(
                    {
                        "type": "missing_response_status",
                        "severity": "info",
                        "path": path,
                        "method": method.upper(),
                        "status": status,
                        "message": f"Response status '{status}' missing in adapter",
                    }
                )

        # Compare schemas for common status codes
        common_statuses = immich_statuses & adapter_statuses
        for status in common_statuses:
            immich_response = immich_responses[status]
            adapter_response = adapter_responses[status]

            # Compare content
            immich_content = immich_response.get("content", {})
            adapter_content = adapter_response.get("content", {})

            if immich_content and adapter_content:
                # Compare schemas for common content types
                common_content_types = set(immich_content.keys()) & set(
                    adapter_content.keys()
                )
                for content_type in common_content_types:
                    immich_schema = immich_content[content_type].get("schema", {})
                    adapter_schema = adapter_content[content_type].get("schema", {})

                    schema_diff = DeepDiff(
                        immich_schema,
                        adapter_schema,
                        ignore_order=True,
                        exclude_regex_paths=[
                            r".*\['description'\]",
                            r".*\['title'\]",
                            r".*\['example'\]",
                        ],
                    )
                    if schema_diff:
                        details = self._format_deepdiff(schema_diff)
                        self.differences.append(
                            {
                                "type": "response_schema_mismatch",
                                "severity": "warning",
                                "path": path,
                                "method": method.upper(),
                                "status": status,
                                "content_type": content_type,
                                "message": f"Response schema differs for status '{status}'",
                                "details": details,
                            }
                        )


def display_results(differences: List[Dict[str, Any]], verbose: bool = False) -> int:
    """
    Display comparison results in a formatted table.

    Args:
        differences: List of differences found
        verbose: Whether to show verbose output

    Returns:
        Exit code (number of incompatible differences)
    """
    if not differences:
        console.print(
            "[green]✓ No differences found! The specifications are compatible.[/green]"
        )
        return 0

    # Group differences by severity
    errors = [d for d in differences if d["severity"] == "error"]
    warnings = [d for d in differences if d["severity"] == "warning"]
    infos = [d for d in differences if d["severity"] == "info"]

    # Create summary table
    table = Table(
        title="API Compatibility Report", show_header=True, header_style="bold magenta"
    )
    table.add_column("Severity", style="bold", width=10)
    table.add_column("Type", width=25)
    table.add_column("Path", width=40)
    table.add_column("Method", width=8)
    table.add_column("Message", width=50)

    # Add errors first
    for diff in errors:
        severity_text = Text("ERROR", style="bold red")
        table.add_row(
            severity_text,
            diff["type"],
            diff["path"],
            diff.get("method", "-"),
            diff["message"],
        )

    # Add warnings
    for diff in warnings:
        severity_text = Text("WARNING", style="bold yellow")
        table.add_row(
            severity_text,
            diff["type"],
            diff["path"],
            diff.get("method", "-"),
            diff["message"],
        )

    # Add info if verbose
    if verbose:
        for diff in infos:
            severity_text = Text("INFO", style="bold blue")
            table.add_row(
                severity_text,
                diff["type"],
                diff["path"],
                diff.get("method", "-"),
                diff["message"],
            )

    console.print(table)

    # Print summary
    console.print("\n[bold]Summary:[/bold]")
    console.print(f"  [red]Errors:[/red] {len(errors)}")
    console.print(f"  [yellow]Warnings:[/yellow] {len(warnings)}")
    if verbose:
        console.print(f"  [blue]Info:[/blue] {len(infos)}")

    # Print detailed differences for items with details
    detailed_diffs = [
        d
        for d in differences
        if "details" in d
        or d["type"]
        in [
            "parameter_schema_mismatch",
            "request_body_schema_mismatch",
            "response_schema_mismatch",
            "parameter_mismatch",
        ]
    ]

    if detailed_diffs:
        console.print("\n[bold]Detailed Differences:[/bold]\n")

        # Group by path and method for better organization
        from collections import defaultdict

        grouped = defaultdict(list)
        for diff in detailed_diffs:
            key = (diff["path"], diff.get("method", "N/A"))
            grouped[key].append(diff)

        for (path, method), diffs in sorted(grouped.items()):
            console.print(f"[cyan]{path}[/cyan] - [yellow]{method}[/yellow]")

            for diff in diffs:
                if diff["severity"] == "error":
                    style = "red"
                elif diff["severity"] == "warning":
                    style = "yellow"
                else:
                    style = "blue"

                # Print the specific difference type
                console.print(f"  [{style}]• {diff['type']}[/{style}]")

                # Print additional context if available
                if "parameter" in diff:
                    console.print(
                        f"    Parameter: {diff['parameter']} ({diff.get('location', 'unknown')})"
                    )
                if "content_type" in diff:
                    console.print(f"    Content-Type: {diff['content_type']}")
                if "status" in diff:
                    console.print(f"    Status Code: {diff['status']}")

                # Print details if available
                if "details" in diff:
                    details = diff["details"]
                    if isinstance(details, str):
                        # Truncate very long details
                        if len(details) > 500:
                            details = details[:500] + "..."
                        console.print(f"    Details: {details}")
                    else:
                        console.print(f"    Details: {details}")

                console.print()

    # Return number of errors as exit code
    return len(errors)


@click.command()
@click.option(
    "--endpoints",
    default="",
    help='Comma-separated list of endpoint names to validate (e.g., "server,system-config")',
)
@click.option(
    "--immich-spec", required=True, help="URL or path to Immich OpenAPI specification"
)
@click.option(
    "--adapter-spec",
    required=True,
    help="URL or path to immich-adapter OpenAPI specification",
)
@click.option(
    "--verbose",
    is_flag=True,
    help="Show verbose output including info-level differences",
)
def main(endpoints: str, immich_spec: str, adapter_spec: str, verbose: bool):
    """
    Validate API compatibility between Immich and immich-adapter.
    
    This tool compares OpenAPI specifications to ensure that immich-adapter
    correctly implements the Immich API endpoints.
    
    Examples:
    
    \b
    # Compare specific endpoints from remote specs
    uv run tools/validate_api_compatibility.py \\
        --endpoints=server,system-config \\
        --immich-spec=https://github.com/immich-app/immich/blob/main/open-api/immich-openapi-specs.json \\
        --adapter-spec=http://localhost:3001/openapi.json
    
    \b
    # Compare all endpoints from local files
    uv run tools/validate_api_compatibility.py \\
        --immich-spec=./immich-openapi.json \\
        --adapter-spec=./adapter-openapi.json
    """
    console.print("[bold]OpenAPI Compatibility Validator[/bold]\n")

    # Parse endpoints
    endpoint_list = [e.strip() for e in endpoints.split(",")] if endpoints else []
    if endpoint_list:
        console.print(f"Validating endpoints: {', '.join(endpoint_list)}")
    else:
        console.print("Validating all endpoints")

    # Fetch specifications
    console.print(f"\nFetching Immich spec from: {immich_spec}")
    immich_spec_data = SpecFetcher.fetch(immich_spec)

    console.print(f"Fetching adapter spec from: {adapter_spec}")
    adapter_spec_data = SpecFetcher.fetch(adapter_spec)

    # Filter specifications if endpoints specified
    if endpoint_list:
        console.print("\nFiltering specifications to specified endpoints...")
        immich_spec_data = EndpointFilter.filter_spec(immich_spec_data, endpoint_list)
        adapter_spec_data = EndpointFilter.filter_spec(adapter_spec_data, endpoint_list)

        # Check if any endpoints were found
        immich_paths = len(immich_spec_data.get("paths", {}))
        adapter_paths = len(adapter_spec_data.get("paths", {}))

        if immich_paths == 0 and adapter_paths == 0:
            console.print(
                f"[yellow]Warning: No endpoints found matching: {', '.join(endpoint_list)}[/yellow]"
            )
            console.print("Check that the endpoint names are correct.")
            sys.exit(0)

        console.print(f"  Found {immich_paths} paths in Immich spec")
        console.print(f"  Found {adapter_paths} paths in adapter spec")

    # Compare specifications
    console.print("\nComparing specifications...")
    comparator = SpecComparator(immich_spec_data, adapter_spec_data)
    differences = comparator.compare()

    # Display results
    console.print("")
    exit_code = display_results(differences, verbose)

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
