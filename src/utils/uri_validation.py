"""
Outbound address validation.

This module provides two defensive helpers used to vet an address *before* the
app is made to contact it:

* :func:`validate_printer_uri` -- for a printer URI handed to a brother_ql
  backend (``tcp://`` / ``usb://``).
* :func:`validate_webhook_url` -- for the relay power-control webhook
  (``http://`` / ``https://``).

They are deliberately separate functions with separate scheme allowlists. The
printer validator must never learn to accept ``http://``: it guards a code path
that opens sockets to whatever it is given, and widening it to satisfy the
webhook would hand every printer setting an HTTP surface it has no use for.

Why this exists
---------------
A printer URI ultimately drives a backend that performs network connections
(``tcp://``) or device access (``usb://``). If an attacker (or a misconfigured
client) could supply an arbitrary scheme such as ``file://`` or ``lpt://`` they
could trigger arbitrary file writes, and unrestricted ``tcp://`` hosts could be
abused for SSRF against sensitive internal endpoints (notably the cloud
metadata service at ``169.254.169.254``).

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
  RFC1918 networks (e.g. ``tcp://192.168.1.100``) or are addressed by hostname,
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

# Schemes the relay webhook may use. A relay bridge (Node-RED, Shelly, Tasmota,
# ESPHome, Home Assistant) speaks HTTP; nothing else is on offer, so nothing
# else is accepted.
ALLOWED_WEBHOOK_SCHEMES = ("http", "https")

# Cloud metadata endpoints that are NOT caught by the link-local check below and
# therefore need naming. 169.254.169.254 (AWS/GCP/Azure/DO) and fe80::/10 are
# link-local and already covered; these three are not:
#
#   fd00:ec2::254      AWS IMDS over IPv6. It sits in fd00::/8, the unique-local
#                      range -- which is exactly the "private/LAN" space the
#                      relay allowance opens up, so without this entry it would
#                      be reachable.
#   100.100.100.200    Alibaba Cloud metadata, inside the carrier-grade NAT
#                      block 100.64.0.0/10 that ``is_private`` does not flag.
#   192.0.0.192        Oracle Cloud metadata (legacy endpoint).
_METADATA_ADDRESSES = frozenset({
    "fd00:ec2::254",
    "100.100.100.200",
    "192.0.0.192",
})

# Metadata services addressed by name rather than by address. DNS is not
# resolved here (see validate_printer_uri for why), so the names have to be
# refused literally.
_METADATA_HOSTNAMES = frozenset({
    "metadata",
    "metadata.google.internal",
    "metadata.goog",
})


def _reject(message: str) -> None:
    """Raise a ValueError with a consistent, descriptive prefix."""
    raise ValueError(f"Invalid printer URI: {message}")


def _reject_webhook(message: str) -> None:
    """Raise a ValueError with a consistent, descriptive prefix."""
    raise ValueError(f"Invalid webhook URL: {message}")


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
    # Extract the host. urlparse() puts "192.168.1.100" of "tcp://192.168.1.100"
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


def validate_webhook_url(url: str) -> None:
    """
    Validate a relay power-control webhook URL. Returns ``None`` when the URL is
    acceptable and raises :class:`ValueError` with a clear message otherwise.

    Why this is a separate, deliberately narrow allowance
    ----------------------------------------------------
    The thing being addressed here is a mains relay on the user's own LAN --
    ``http://192.168.1.42/relay/0`` or a Node-RED endpoint on the same subnet as
    the printer. A general-purpose SSRF guard would block precisely that, so the
    private ranges have to be opened up. What it must not become is a way to make
    the app issue an arbitrary HTTP request to anything at all, so the allowance
    is scoped to the smallest shape that lets a LAN relay work:

      * ``http://`` and ``https://`` only. No ``file://``, no ``gopher://``, no
        scheme-relative or scheme-less input.
      * Private / RFC1918 / unique-local / LAN addresses and plain hostnames are
        ALLOWED. This is the whole point.
      * Link-local (169.254.0.0/16, fe80::/10) is still REFUSED -- this is the
        cloud metadata endpoint and the classic SSRF target.
      * The metadata services that live outside link-local (AWS over IPv6,
        Alibaba, Oracle, ``metadata.google.internal``) are refused by name.
      * Loopback (127.0.0.0/8, ::1) and the unspecified address are still
        REFUSED, exactly as they are for a printer URI. A mains relay is a
        physical device on the network, never a service inside this app's own
        container, so nothing legitimate is lost -- while allowing it would turn
        a settings write into a way to POST at every other service bound on
        localhost, including this app's own API.
      * DNS is not resolved, for the same reasons as in
        :func:`validate_printer_uri`: it costs latency on every settings write
        and the resolution itself is an outbound request driven by attacker
        input. A hostname that resolves to a blocked address is not caught here;
        the guard is a narrowing of what may be *asked for*, not a promise about
        where the network ultimately routes.

    Args:
        url: The webhook URL to check.

    Raises:
        ValueError: When the URL is empty, malformed, uses a scheme other than
            http/https, or names a refused host.
    """
    if url is None or not isinstance(url, str) or not url.strip():
        _reject_webhook("URL must be a non-empty string.")

    url = url.strip()

    parsed = urlparse(url)
    scheme = (parsed.scheme or "").lower()

    if scheme not in ALLOWED_WEBHOOK_SCHEMES:
        _reject_webhook(
            f"unsupported scheme '{parsed.scheme}'. "
            f"Allowed schemes: {', '.join(s + '://' for s in ALLOWED_WEBHOOK_SCHEMES)}."
        )

    host = parsed.hostname
    if not host:
        _reject_webhook("URL is missing a host.")

    # Port, if given, has to be a real port. urlparse defers validation until
    # .port is read, and then raises -- so read it here rather than letting it
    # blow up later inside urllib at request time.
    try:
        port = parsed.port
    except ValueError:
        _reject_webhook(f"invalid port in '{url}'.")
    else:
        if port is not None and not (1 <= port <= 65535):
            _reject_webhook(f"invalid port {port} in '{url}'.")

    host = host.lower()

    if host in _METADATA_HOSTNAMES:
        _reject_webhook(f"cloud metadata endpoints are not allowed ({host}).")

    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        # Not an IP literal => hostname => allowed (a relay is very often
        # addressed as e.g. "shelly-relay.local" or "nodered.lan").
        return

    if str(ip) in _METADATA_ADDRESSES or ip.compressed in _METADATA_ADDRESSES:
        _reject_webhook(f"cloud metadata endpoints are not allowed ({host}).")

    if ip.is_loopback:
        _reject_webhook(
            f"loopback addresses are not allowed ({host}). A mains relay is a "
            "device on the network, not a service inside this container."
        )

    if ip.is_link_local:
        _reject_webhook(f"link-local / metadata addresses are not allowed ({host}).")

    if ip.is_unspecified:
        _reject_webhook(f"unspecified address is not allowed ({host}).")

    # Private/LAN and public addresses alike are accepted from here on: the
    # relay lives on the LAN, and a hosted relay bridge is a legitimate setup
    # too.
    return
