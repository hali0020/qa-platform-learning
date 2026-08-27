from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from urllib.parse import unquote, urlparse

from app.pipeline.providers.errors import (
    ProviderConfigurationError,
    ProviderSecurityError,
    ProviderTransportError,
)

AddressResolver = Callable[[str, int], Awaitable[Iterable[str]]]

_SELF_HOSTED_BOUNDARIES = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("::1/128"),
)


def _is_self_hosted_network(
    network: ipaddress.IPv4Network | ipaddress.IPv6Network,
) -> bool:
    inside_lab_boundary = any(
        network.version == boundary.version and network.subnet_of(boundary)
        for boundary in _SELF_HOSTED_BOUNDARIES
    )
    minimum_prefix = 24 if network.version == 4 else 64
    return inside_lab_boundary and network.prefixlen >= minimum_prefix


def _is_self_hosted_address(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> bool:
    return any(
        address.version == boundary.version and address in boundary
        for boundary in _SELF_HOSTED_BOUNDARIES
    )


def _normalized_host(host: str) -> str:
    candidate = host.strip().rstrip(".").lower()
    if not candidate:
        raise ProviderConfigurationError("provider host is empty")
    try:
        return candidate.encode("idna").decode("ascii")
    except UnicodeError as error:
        raise ProviderConfigurationError("provider host is invalid") from error


@dataclass(frozen=True, slots=True)
class OutboundPolicy:
    """Explicit egress allowlist used by every self-hosted lab provider.

    Only RFC1918/ULA private or loopback ranges are accepted, and every DNS
    result must also match an explicit CIDR. Public, link-local, reserved,
    multicast and unspecified destinations are never supported by this policy.
    """

    allowed_hosts: tuple[str, ...]
    allowed_ports: tuple[int, ...] = (443,)
    allowed_networks: tuple[str, ...] = ()
    allow_loopback_http: bool = False
    allowed_http_hosts: tuple[str, ...] = ()
    max_response_bytes: int = 1_048_576

    def __post_init__(self) -> None:
        normalized = tuple(_normalized_host(host) for host in self.allowed_hosts)
        if not normalized:
            raise ProviderConfigurationError("provider host allowlist is empty")
        if not self.allowed_ports or any(
            port < 1 or port > 65535 for port in self.allowed_ports
        ):
            raise ProviderConfigurationError("provider port allowlist is invalid")
        if self.max_response_bytes < 1:
            raise ProviderConfigurationError("response size limit must be positive")
        try:
            parsed_networks = tuple(
                ipaddress.ip_network(value, strict=True)
                for value in self.allowed_networks
            )
        except ValueError as error:
            raise ProviderConfigurationError("provider network allowlist is invalid") from error
        if not parsed_networks:
            raise ProviderConfigurationError("provider network allowlist is empty")
        if any(not _is_self_hosted_network(network) for network in parsed_networks):
            raise ProviderConfigurationError(
                "provider networks must be self-hosted private or loopback CIDRs"
            )
        http_hosts = tuple(
            _normalized_host(host) for host in self.allowed_http_hosts
        )
        if any(host not in normalized for host in http_hosts):
            raise ProviderConfigurationError(
                "HTTP provider hosts must also be in the host allowlist"
            )
        for host in http_hosts:
            try:
                address = ipaddress.ip_address(host)
            except ValueError as error:
                raise ProviderConfigurationError(
                    "HTTP provider hosts must be exact IP literals"
                ) from error
            if not _is_self_hosted_address(address) or not any(
                address.version == network.version and address in network
                for network in parsed_networks
            ):
                raise ProviderConfigurationError(
                    "HTTP provider hosts must be inside an allowed private network"
                )
        networks = tuple(str(network) for network in parsed_networks)
        object.__setattr__(self, "allowed_hosts", normalized)
        object.__setattr__(self, "allowed_networks", networks)
        object.__setattr__(self, "allowed_http_hosts", http_hosts)

    @property
    def parsed_networks(self) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
        return tuple(ipaddress.ip_network(value) for value in self.allowed_networks)


def validate_base_url(value: str, policy: OutboundPolicy) -> str:
    parsed = urlparse(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ProviderConfigurationError("provider base URL must be HTTP(S)")
    if parsed.username is not None or parsed.password is not None:
        raise ProviderConfigurationError("provider base URL cannot contain credentials")
    if parsed.query or parsed.fragment:
        raise ProviderConfigurationError("provider base URL cannot contain query or fragment")

    host = _normalized_host(parsed.hostname)
    if host not in policy.allowed_hosts:
        raise ProviderSecurityError("provider host is not allowlisted")
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as error:
        raise ProviderConfigurationError("provider port is invalid") from error
    if port not in policy.allowed_ports:
        raise ProviderSecurityError("provider port is not allowlisted")

    if parsed.scheme != "https":
        try:
            is_loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            is_loopback = host == "localhost"
        loopback_allowed = policy.allow_loopback_http and is_loopback
        exact_private_http_allowed = host in policy.allowed_http_hosts
        if not loopback_allowed and not exact_private_http_allowed:
            raise ProviderSecurityError("external providers require HTTPS")

    decoded_path = unquote(parsed.path)
    if "\\" in decoded_path or any(ord(character) < 32 for character in decoded_path):
        raise ProviderConfigurationError("provider base path is invalid")
    if any(segment in {".", ".."} for segment in decoded_path.split("/")):
        raise ProviderConfigurationError("provider base path cannot traverse directories")
    return value.strip().rstrip("/")


def validate_relative_path(path: str) -> str:
    parsed = urlparse(path)
    if (
        not path.startswith("/")
        or path.startswith("//")
        or parsed.scheme
        or parsed.netloc
        or parsed.query
        or parsed.fragment
    ):
        raise ProviderSecurityError("provider request path must be relative to its base URL")
    decoded = unquote(parsed.path)
    if "\\" in decoded or any(ord(character) < 32 for character in decoded):
        raise ProviderSecurityError("provider request path is invalid")
    if any(segment in {".", ".."} for segment in decoded.split("/")):
        raise ProviderSecurityError("provider request path cannot traverse directories")
    return path


async def default_resolver(host: str, port: int) -> tuple[str, ...]:
    def resolve() -> tuple[str, ...]:
        records = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        return tuple(sorted({record[4][0] for record in records}))

    try:
        return await asyncio.to_thread(resolve)
    except OSError as error:
        raise ProviderTransportError("provider host resolution failed") from error


async def validate_resolved_addresses(
    host: str,
    port: int,
    policy: OutboundPolicy,
    resolver: AddressResolver = default_resolver,
) -> tuple[str, ...]:
    try:
        addresses = tuple(await resolver(host, port))
    except ProviderSecurityError:
        raise
    except Exception as error:
        raise ProviderSecurityError("provider host resolution failed safely") from error
    if not addresses:
        raise ProviderSecurityError("provider host did not resolve")

    allowed_networks = policy.parsed_networks
    normalized: list[str] = []
    for value in addresses:
        try:
            address = ipaddress.ip_address(value)
        except ValueError as error:
            raise ProviderSecurityError("provider host resolved to an invalid address") from error
        if not _is_self_hosted_address(address):
            raise ProviderSecurityError(
                "provider host resolved outside self-hosted private networks"
            )
        explicitly_allowed = any(address in network for network in allowed_networks)
        if not explicitly_allowed:
            raise ProviderSecurityError("provider host resolved outside the allowed networks")
        normalized.append(str(address))
    return tuple(sorted(set(normalized)))


__all__ = [
    "AddressResolver",
    "OutboundPolicy",
    "default_resolver",
    "validate_base_url",
    "validate_relative_path",
    "validate_resolved_addresses",
]
