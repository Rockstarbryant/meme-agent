"""Webhook URL safety.

A user-supplied webhook URL makes OUR server send HTTP requests. Without a check, anyone with an account could point it
at internal addresses (cloud metadata at 169.254.169.254, localhost services, a private network) and use the server as a
proxy. A URL is accepted only when its host resolves exclusively to PUBLIC addresses. Redirects are never followed.

The check runs when the URL is saved and again right before each delivery (DNS can change in between). It cannot stop a
DNS-rebinding race between this check and the connection itself; for that, restrict outbound traffic at the network level.
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit


def _public(ip: str) -> bool:
    a = ipaddress.ip_address(ip.split("%")[0])
    if getattr(a, "ipv4_mapped", None):
        a = a.ipv4_mapped
    return a.is_global and not (a.is_multicast or a.is_reserved or a.is_loopback or a.is_link_local or a.is_private)


def check_webhook_url(url: str, *, allow_http: bool = True) -> tuple[bool, str]:
    """(ok, reason). ``reason`` is a short, user-facing explanation when not ok."""
    try:
        parts = urlsplit((url or "").strip())
    except ValueError:
        return False, "That is not a valid URL."
    if parts.scheme not in ("https", "http"):
        return False, "The URL must start with https:// (or http://)."
    if parts.scheme == "http" and not allow_http:
        return False, "Use https://, plain http is not accepted here."
    if parts.username or parts.password:
        return False, "Put credentials in the path or a header token, not before the host (user:pass@)."
    host = parts.hostname
    if not host:
        return False, "The URL has no host."
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError:
        return False, "The URL has an invalid port."
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return False, f"Cannot resolve {host}. Check the address."
    addrs = {i[4][0] for i in infos}
    if not addrs:
        return False, f"Cannot resolve {host}."
    bad = sorted(a for a in addrs if not _public(a))
    if bad:
        return False, "That address points to a private or internal network, which webhooks may not use."
    return True, ""
