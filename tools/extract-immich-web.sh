#!/bin/bash

# Script to extract web files from immich-server container
# Usage: ./extract-immich-web.sh [OPTIONS] <tag> <output-directory>
# Example: ./extract-immich-web.sh release ./web-files

set -e  # Exit on error
set -u  # Exit on undefined variable
set -o pipefail  # Exit on pipe failure

# Color codes for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Function to print colored messages
print_error() {
    echo -e "${RED}ERROR: $1${NC}" >&2
}

print_success() {
    echo -e "${GREEN}SUCCESS: $1${NC}"
}

print_info() {
    echo -e "${YELLOW}INFO: $1${NC}"
}

# Function to cleanup on exit
cleanup() {
    local exit_code=$?
    if [ -n "${CONTAINER_ID:-}" ]; then
        print_info "Cleaning up container: $CONTAINER_ID"
        docker rm "$CONTAINER_ID" >/dev/null 2>&1 || true
    fi
    if [ $exit_code -ne 0 ]; then
        print_error "Script failed with exit code $exit_code"
    fi
}

# Function to show usage
show_usage() {
    echo "Usage: $0 [OPTIONS] <output-directory>"
    echo ""
    echo "Extract web files from immich-server Docker container."
    echo ""
    echo "Arguments:"
    echo "  <output-directory> Directory to extract web files into"
    echo ""
    echo "Options:"
    echo "  -t, --tag <tag>    Specify Docker image tag (e.g., 'release', 'v2.2.2')"
    echo "                     If not specified, reads from .immich-container-tag file"
    echo "  -f, --force        Overwrite output directory if it exists"
    echo "  -s, --skip-pull    Skip pulling image if it exists locally"
    echo "  -h, --help         Show this help message"
    echo ""
    echo "Examples:"
    echo "  $0 ./web-files                    # Read tag from .immich-container-tag"
    echo "  $0 -t release ./web-files         # Use 'release' tag"
    echo "  $0 --tag v2.2.2 ./web-files       # Use specific version"
    echo "  $0 -t release -f ./web-files      # With force overwrite"
    echo "  $0 -s ./web-files                 # Skip pull, use tag file"
}

# Set trap to cleanup on script exit
trap cleanup EXIT INT TERM

# Initialize variables
CONTAINER_ID=""
FORCE=false
SKIP_PULL=false
TAG=""
TAG_FILE=".immich-container-tag"

# Parse options
while [[ $# -gt 0 ]]; do
    case $1 in
        -h|--help)
            show_usage
            exit 0
            ;;
        -f|--force)
            FORCE=true
            shift
            ;;
        -s|--skip-pull)
            SKIP_PULL=true
            shift
            ;;
        -t|--tag)
            if [ -z "${2:-}" ]; then
                print_error "Option -t/--tag requires a value"
                show_usage
                exit 1
            fi
            TAG="$2"
            shift 2
            ;;
        -*)
            print_error "Unknown option: $1"
            show_usage
            exit 1
            ;;
        *)
            break
            ;;
    esac
done

# Check arguments - expect exactly 1 (output directory)
if [ $# -ne 1 ]; then
    print_error "Invalid number of arguments"
    show_usage
    exit 1
fi

OUTPUT_DIR="$1"

# If tag not specified via option, read from file
if [ -z "$TAG" ]; then
    if [ ! -f "$TAG_FILE" ]; then
        print_error "Tag not specified and tag file not found: $TAG_FILE"
        print_error "Either use -t/--tag option or create a $TAG_FILE file"
        exit 1
    fi

    TAG=$(cat "$TAG_FILE" | tr -d '[:space:]')
    if [ -z "$TAG" ]; then
        print_error "Tag file is empty: $TAG_FILE"
        exit 1
    fi

    print_info "Read tag from $TAG_FILE: $TAG"
else
    print_info "Using tag from command line: $TAG"
fi

# Validate tag is not empty
if [ -z "$TAG" ]; then
    print_error "Tag cannot be empty"
    exit 1
fi

# Validate output directory is not empty
if [ -z "$OUTPUT_DIR" ]; then
    print_error "Output directory cannot be empty"
    exit 1
fi

# Check if output directory already exists
if [ -e "$OUTPUT_DIR" ]; then
    if [ "$FORCE" = false ]; then
        print_error "Output directory already exists: $OUTPUT_DIR"
        print_error "Use --force to overwrite"
        exit 1
    else
        print_info "Output directory exists, will overwrite due to --force flag"
        rm -rf "$OUTPUT_DIR"
    fi
fi

# Check if docker is available
if ! command -v docker &> /dev/null; then
    print_error "Docker is not installed or not in PATH"
    exit 1
fi

IMAGE_NAME="ghcr.io/immich-app/immich-server:$TAG"

# Pull image unless --skip-pull is set
if [ "$SKIP_PULL" = true ]; then
    print_info "Skipping image pull (--skip-pull flag set)"
    # Check if image exists locally
    if ! docker image inspect "$IMAGE_NAME" >/dev/null 2>&1; then
        print_error "Image not found locally: $IMAGE_NAME"
        print_error "Remove --skip-pull flag to pull the image"
        exit 1
    fi
else
    print_info "Pulling image: $IMAGE_NAME"
    if ! docker pull "$IMAGE_NAME"; then
        print_error "Failed to pull image: $IMAGE_NAME"
        exit 1
    fi
fi

print_info "Creating temporary container from image"
CONTAINER_ID=$(docker create "$IMAGE_NAME")
if [ -z "$CONTAINER_ID" ]; then
    print_error "Failed to create container"
    exit 1
fi
print_info "Container created: $CONTAINER_ID"

# Create a temporary directory for extraction
TEMP_DIR=$(mktemp -d)
print_info "Using temporary directory: $TEMP_DIR"

print_info "Extracting web files from /build/www"
if ! docker cp "$CONTAINER_ID:/build/www" "$TEMP_DIR/"; then
    print_error "Failed to extract web files from container"
    rm -rf "$TEMP_DIR"
    exit 1
fi

# Verify extraction - check for expected files
if [ ! -f "$TEMP_DIR/www/index.html" ]; then
    print_error "Extracted files appear incomplete - index.html not found"
    rm -rf "$TEMP_DIR"
    exit 1
fi

if [ ! -d "$TEMP_DIR/www/_app" ]; then
    print_error "Extracted files appear incomplete - _app directory not found"
    rm -rf "$TEMP_DIR"
    exit 1
fi

# Move the contents of www to the output directory (not the www directory itself)
print_info "Moving web files to $OUTPUT_DIR"
mv "$TEMP_DIR/www" "$OUTPUT_DIR"

# Clean up temp directory
rm -rf "$TEMP_DIR"

print_info "Removing temporary container: $CONTAINER_ID"
if ! docker rm "$CONTAINER_ID" >/dev/null 2>&1; then
    print_error "Warning: Failed to remove container $CONTAINER_ID"
    print_error "You may need to remove it manually with: docker rm $CONTAINER_ID"
else
    CONTAINER_ID=""  # Clear so cleanup trap doesn't try to remove again
fi

print_success "Web files extracted successfully to: $OUTPUT_DIR"

# Show some stats about extracted files
if command -v du &> /dev/null; then
    SIZE=$(du -sh "$OUTPUT_DIR" 2>/dev/null | cut -f1)
    print_info "Total size: $SIZE"
fi

if command -v find &> /dev/null; then
    FILE_COUNT=$(find "$OUTPUT_DIR" -type f 2>/dev/null | wc -l | tr -d ' ')
    print_info "Total files: $FILE_COUNT"
fi

# Verify key files exist
if [ -f "$OUTPUT_DIR/index.html" ] && [ -d "$OUTPUT_DIR/_app" ]; then
    print_success "Verification passed: Key files found (index.html, _app/)"
else
    print_error "Warning: Expected files may be missing"
fi

print_success "Done! Container cleaned up."
