"""Loopback-aware HTTP helper (`server/net_utils.py`).

`urllib.request.urlopen` honours `HTTP(S)_PROXY` even for `127.0.0.1` on Linux/macOS
unless `NO_PROXY` is set. Many users run a system proxy (Clash, V2Ray, corporate
proxies) exported as environment variables, which silently routes loopback calls
to SillyTavern or a local embedding server through the proxy and makes them fail.

`open_url()` bypasses proxies for loopback hosts only; remote hosts keep the user's
proxy settings untouched.
"""

from __future__ import annotations

import ipaddress
import urllib.parse
import urllib.request

_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def is_loopback_host(host: str | None) -> bool:
    host = (host or "").strip("[]").lower()
    if host in ("", "localhost") or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def is_loopback_url(url: str) -> bool:
    return is_loopback_host(urllib.parse.urlparse(url).hostname)


def open_url(req: urllib.request.Request | str, timeout: float):
    """`urlopen` that never sends loopback traffic through an environment proxy."""
    full_url = req.full_url if isinstance(req, urllib.request.Request) else str(req)
    if is_loopback_url(full_url):
        return _NO_PROXY_OPENER.open(req, timeout=timeout)
    return urllib.request.urlopen(req, timeout=timeout)
