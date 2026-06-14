"""
Printer URI validation.

This module provides a single defensive helper, :func:`validate_printer_uri`,
used to vet a printer URI *before* it is ever handed to a brother_ql backend.

Why this exists
---------------
A printer URI ultimately drives a backend that performs network connections
(``tcp://``) or device access (``usb://``). If an attacker (or a misconfigured
client) could supply an arbitrary scheme such as ``file://`` or ``lpt://`` they
could trigger arbitrary file writes, and unrestricted ``tcp://`` hosts could be
abused for SSRF against sensitive internal endpoints (notably the cloud
metadata service at ``169.254.169.254``).

Design goals
------------
* Allow only the schemes the app actually supports: ``tcp://`` and ``usb://``.
* Keep real-world LAN usage working: Brother label printers live on private
  RFC1918 networks (e.g. ``tcp://10.50.60.20``) or are addressed by hostname,
  so those MUST remain valid.
* Block only the genuinely dangerous targets for ``tcp://``: link-local /
  cloud-metadata (169.254.0.0/16), loopback (127.0.0.0/8, ::1) and the
  unspecified address 0.0.0.0 / ::.
* No third-party dependencies -- standard library only.
"""

import ipaddress
from urllib.parse import urlparse

# Schemes the application knows how to talk to. Anything else (file://, lpt://,
# http://, an empty scheme, ...) is rejected outright.
ALLOWED_SCHEMES = ("tcp", "usb")


def _reject(message: str) -> None:
    """Raise a ValueError with a consistent, descriptive prefix."""
    raise ValueError(f"Invalid printer URI: {message}")


def validate_printer_uri(uri: str) -> None:
    """
    Validate a printer URI. Returns ``None`` when the URI is acceptable and
    raises :class:`ValueError` with a clear message otherwise.

    Rules:
      * Only ``tcp://`` and ``usb://`` schemes are allowed.
      * For ``tcp://`` the host is extracted and, *if it is an IP address*,
        checked against a small blocklist (link-local/metadata, loopback,
        unspecified). Private/LAN IPs and plain hostnames are allowed.
    """
    # --- Basic shape checks -------------------------------------------------
    if uri is None or not isinstance(uri, str) or not uri.strip():
        _reject("URI must be a non-empty string.")

    uri = uri.strip()

    parsed = urlparse(uri)
    scheme = (parsed.scheme or "").lower()

    if scheme not in ALLOWED_SCHEMES:
        _reject(
            f"unsupported scheme '{parsed.scheme}'. "
            f"Allowed schemes: {', '.join(s + '://' for s in ALLOWED_SCHEMES)}."
        )

    # --- usb:// ------------------------------------------------------------
    # USB device addressing does not carry a network host, so there is no SSRF
    # surface to guard against here. Having a valid, allowed scheme is enough.
    if scheme == "usb":
        return

    # --- tcp:// ------------------------------------------------------------
    # Extract the host. urlparse() puts "10.50.60.20" of "tcp://10.50.60.20"
    # into .hostname (lower-cased, port stripped, brackets removed for IPv6).
    host = parsed.hostname

    # Fallback: some malformed inputs (e.g. "tcp://" with nothing after) leave
    # hostname as None/empty -> reject explicitly.
    if not host:
        _reject("tcp:// URI is missing a host.")

    # Try to interpret the host as an IP address. If it is NOT a valid IP we
    # treat it as a hostname, which is explicitly allowed (printers are often
    # reachable by name on the LAN). We do not perform DNS resolution here --
    # that would add latency and could itself be an SSRF vector; backends will
    # resolve at connect time.
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        # Not an IP literal => hostname => allowed.
        return

    # The host is a literal IP address: apply the targeted blocklist. Private
    # / LAN ranges are intentionally NOT blocked because that is exactly where
    # the printers live.
    if ip.is_loopback:
        # 127.0.0.0/8 and ::1
        _reject(f"loopback addresses are not allowed ({host}).")

    if ip.is_link_local:
        # 169.254.0.0/16 and fe80::/10 -- includes the cloud metadata
        # endpoint 169.254.169.254. Blocking this is the core SSRF guard.
        _reject(f"link-local / metadata addresses are not allowed ({host}).")

    if ip.is_unspecified:
        # 0.0.0.0 and ::
        _reject(f"unspecified address is not allowed ({host}).")

    # Everything else (private RFC1918, public, etc.) is accepted.
    return
