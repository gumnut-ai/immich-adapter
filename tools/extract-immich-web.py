#!/usr/bin/env python3

"""
Script to extract web files from immich-server container.

Usage: ./extract-immich-web.py [OPTIONS] <output-directory>
Example: ./extract-immich-web.py -t release ./web-files
"""

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


class Colors:
    """ANSI color codes for terminal output."""

    RED = "\033[0;31m"
    GREEN = "\033[0;32m"
    YELLOW = "\033[1;33m"
    NC = "\033[0m"  # No Color


def print_error(message: str) -> None:
    """Print error message in red to stderr."""
    print(f"{Colors.RED}ERROR: {message}{Colors.NC}", file=sys.stderr)


def print_success(message: str) -> None:
    """Print success message in green."""
    print(f"{Colors.GREEN}SUCCESS: {message}{Colors.NC}")


def print_info(message: str) -> None:
    """Print info message in yellow."""
    print(f"{Colors.YELLOW}INFO: {message}{Colors.NC}")


def run_command(
    cmd: list[str], check: bool = True, capture_output: bool = False
) -> subprocess.CompletedProcess:
    """Run a shell command and return the result."""
    return subprocess.run(
        cmd,
        check=check,
        capture_output=capture_output,
        text=True,
        stderr=subprocess.DEVNULL if not capture_output else None,
    )


def cleanup(container_id: str | None, temp_dir: Path | None) -> None:
    """Clean up temporary resources."""
    if container_id:
        print_info(f"Cleaning up container: {container_id}")
        try:
            run_command(["docker", "rm", container_id], check=False)
        except Exception:
            pass

    if temp_dir and temp_dir.exists():
        print_info(f"Removing temporary directory: {temp_dir}")
        try:
            shutil.rmtree(temp_dir)
        except Exception:
            pass


def get_directory_size(path: Path) -> str | None:
    """Get human-readable size of directory using du command."""
    try:
        result = run_command(["du", "-sh", str(path)], check=False, capture_output=True)
        if result.returncode == 0:
            return result.stdout.split()[0]
    except Exception:
        pass
    return None


def count_files(path: Path) -> int:
    """Count number of files in directory recursively."""
    try:
        return sum(1 for p in path.rglob("*") if p.is_file())
    except Exception:
        return 0


def main() -> int:
    """Main function."""
    parser = argparse.ArgumentParser(
        description="Extract web files from immich-server Docker container.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s ./web-files                    # Read tag from .immich-container-tag
  %(prog)s -t release ./web-files         # Use 'release' tag
  %(prog)s --tag v2.2.2 ./web-files       # Use specific version
  %(prog)s -t release -f ./web-files      # With force overwrite
  %(prog)s -s ./web-files                 # Skip pull, use tag file
""",
    )

    parser.add_argument(
        "output_directory",
        help="Directory to extract web files into",
    )
    parser.add_argument(
        "-t",
        "--tag",
        help="Specify Docker image tag (e.g., 'release', 'v2.2.2'). "
        "If not specified, reads from .immich-container-tag file",
    )
    parser.add_argument(
        "-f",
        "--force",
        action="store_true",
        help="Overwrite output directory if it exists",
    )
    parser.add_argument(
        "-s",
        "--skip-pull",
        action="store_true",
        help="Skip pulling image if it exists locally",
    )

    args = parser.parse_args()

    container_id: str | None = None
    temp_dir: Path | None = None

    try:
        # Initialize variables
        tag_file = Path(".immich-container-tag")
        output_dir = Path(args.output_directory)

        # Determine tag
        if args.tag:
            tag = args.tag
            print_info(f"Using tag from command line: {tag}")
        else:
            if not tag_file.exists():
                print_error(
                    "Tag not specified and tag file not found: " + str(tag_file)
                )
                print_error(f"Either use -t/--tag option or create a {tag_file} file")
                return 1

            tag = tag_file.read_text().strip()
            if not tag:
                print_error(f"Tag file is empty: {tag_file}")
                return 1

            print_info(f"Read tag from {tag_file}: {tag}")

        # Validate tag
        if not tag:
            print_error("Tag cannot be empty")
            return 1

        # Validate output directory
        if not args.output_directory:
            print_error("Output directory cannot be empty")
            return 1

        # Check if output directory exists
        if output_dir.exists():
            if not args.force:
                print_error(f"Output directory already exists: {output_dir}")
                print_error("Use --force to overwrite")
                return 1
            else:
                print_info(
                    "Output directory exists, will overwrite due to --force flag"
                )
                # Preserve .gitkeep if it exists
                gitkeep_path = output_dir / ".gitkeep"
                gitkeep_content = None
                if gitkeep_path.exists():
                    gitkeep_content = gitkeep_path.read_bytes()

                shutil.rmtree(output_dir)

                # Restore .gitkeep if it was backed up
                if gitkeep_content is not None:
                    output_dir.mkdir(parents=True, exist_ok=True)
                    gitkeep_path.write_bytes(gitkeep_content)

        # Check if docker is available
        if shutil.which("docker") is None:
            print_error("Docker is not installed or not in PATH")
            return 1

        image_name = f"ghcr.io/immich-app/immich-server:{tag}"

        # Pull image unless --skip-pull is set
        if args.skip_pull:
            print_info("Skipping image pull (--skip-pull flag set)")
            # Check if image exists locally
            result = run_command(
                ["docker", "image", "inspect", image_name],
                check=False,
                capture_output=True,
            )
            if result.returncode != 0:
                print_error(f"Image not found locally: {image_name}")
                print_error("Remove --skip-pull flag to pull the image")
                return 1
        else:
            print_info(f"Pulling image: {image_name}")
            result = run_command(["docker", "pull", image_name], check=False)
            if result.returncode != 0:
                print_error(f"Failed to pull image: {image_name}")
                return 1

        # Create container
        print_info("Creating temporary container from image")
        result = run_command(
            ["docker", "create", image_name], capture_output=True, check=False
        )
        if result.returncode != 0 or not result.stdout.strip():
            print_error("Failed to create container")
            return 1

        container_id = result.stdout.strip()
        print_info(f"Container created: {container_id}")

        # Create temporary directory
        temp_dir = Path(tempfile.mkdtemp())
        print_info(f"Using temporary directory: {temp_dir}")

        # Extract files from container
        print_info("Extracting web files from /build/www")
        result = run_command(
            ["docker", "cp", f"{container_id}:/build/www", str(temp_dir) + "/"],
            check=False,
        )
        if result.returncode != 0:
            print_error("Failed to extract web files from container")
            return 1

        # Verify extraction
        www_dir = temp_dir / "www"
        if not (www_dir / "index.html").exists():
            print_error("Extracted files appear incomplete - index.html not found")
            return 1

        if not (www_dir / "_app").is_dir():
            print_error("Extracted files appear incomplete - _app directory not found")
            return 1

        # Move contents of www to output directory (not the www directory itself)
        print_info(f"Moving web files to {output_dir}")
        output_dir.mkdir(parents=True, exist_ok=True)

        # Move all files and directories from www to output_dir
        for item in www_dir.iterdir():
            dest = output_dir / item.name
            # Skip .gitkeep if it already exists in output_dir
            if item.name == ".gitkeep" and dest.exists():
                continue
            shutil.move(str(item), str(dest))

        # Remove container
        print_info(f"Removing temporary container: {container_id}")
        result = run_command(["docker", "rm", container_id], check=False)
        if result.returncode != 0:
            print_error(f"Warning: Failed to remove container {container_id}")
            print_error(
                f"You may need to remove it manually with: docker rm {container_id}"
            )
        else:
            container_id = None  # Clear so cleanup doesn't try again

        print_success(f"Web files extracted successfully to: {output_dir}")

        # Show stats
        size = get_directory_size(output_dir)
        if size:
            print_info(f"Total size: {size}")

        file_count = count_files(output_dir)
        print_info(f"Total files: {file_count}")

        # Verify key files
        if (output_dir / "index.html").exists() and (output_dir / "_app").is_dir():
            print_success("Verification passed: Key files found (index.html, _app/)")
        else:
            print_error("Warning: Expected files may be missing")
            return 1

        print_success("Done! Container cleaned up.")
        return 0

    except KeyboardInterrupt:
        print_error("\nScript interrupted by user")
        return 1
    except Exception as e:
        print_error(f"Unexpected error: {e}")
        return 1
    finally:
        # Always clean up
        cleanup(container_id, temp_dir)


if __name__ == "__main__":
    sys.exit(main())
