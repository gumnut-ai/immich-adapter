"""User agent parsing utilities."""

import re
from dataclasses import dataclass

from user_agents import parse as parse_user_agent


@dataclass
class DeviceInfo:
    """Device information extracted from User-Agent string."""

    device_type: str
    device_os: str
    app_version: str


def extract_device_info(ua_string: str) -> DeviceInfo:
    """
    Extract device information from a User-Agent string.

    Parses the User-Agent to determine device type, OS, and app version.
    Handles both browser User-Agents and Immich mobile app User-Agents.

    Args:
        ua_string: The User-Agent header value

    Returns:
        DeviceInfo with device_type, device_os, and app_version
    """
    user_agent = parse_user_agent(ua_string)

    # Extract Immich app version from UA string format Immich_{platform}_{version} (e.g., "Immich_iOS_1.94.0")
    app_version = ""
    if ua_string:
        match = re.match(r"^Immich_(?:Android|iOS)_(.+)$", ua_string)
        if match:
            app_version = match.group(1)

    device_type = user_agent.browser.family or user_agent.device.family or ""

    # Use just the OS name without version to match Immich's ua-parser-js behavior
    # The frontend checks exact values like 'iOS', 'macOS', 'Android' for icons
    device_os = user_agent.os.family or ""

    # Normalize OS names to match what Immich frontend expects
    if device_os == "Mac OS X":
        device_os = "macOS"

    return DeviceInfo(
        device_type=device_type,
        device_os=device_os.strip(),
        app_version=app_version,
    )
