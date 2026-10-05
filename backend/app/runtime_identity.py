"""Narrow async client for the root-owned runtime identity broker."""

from __future__ import annotations

import os
import ssl
from typing import Any

import httpx


DEFAULT_SOCKET = "/run/mobius-identity-broker.sock"


def private_socket_tls_context() -> ssl.SSLContext:
  """Return the TLS context for an HTTP transport bound to a private socket.

  Broker clients speak plain HTTP over a private Unix socket, but HTTPX
  builds a TLS context for every transport and would load the public CA
  bundle each time. Keep verification and hostname checks enabled with no
  trusted roots, so an accidental HTTPS request fails closed. Each transport
  gets its own context; nothing is shared or retained between uses.
  """
  return ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)


def broker_socket_path() -> str:
  """Return the private identity-broker socket path for this runtime."""
  return os.environ.get("MOBIUS_IDENTITY_BROKER_SOCKET", DEFAULT_SOCKET)


def broker_transport() -> httpx.HTTPTransport:
  """Return a synchronous transport bound only to the private broker socket."""
  return httpx.HTTPTransport(
    uds=broker_socket_path(), verify=private_socket_tls_context(),
  )


def broker_async_transport(
  socket_path: str | None = None,
) -> httpx.AsyncHTTPTransport:
  """Return an async transport bound only to the private broker socket."""
  return httpx.AsyncHTTPTransport(
    uds=socket_path or broker_socket_path(),
    verify=private_socket_tls_context(),
  )


def broker_async_client(*, timeout: float = 10.0) -> httpx.AsyncClient:
  """Return an async client connected only to the private broker socket."""
  return httpx.AsyncClient(
    base_url="http://broker", transport=broker_async_transport(), timeout=timeout,
  )


def broker_client(*, timeout: float = 10.0) -> httpx.Client:
  """Return a synchronous client connected only to the private broker socket."""
  return httpx.Client(
    base_url="http://broker", transport=broker_transport(), timeout=timeout,
  )


async def broker_request(
  method: str,
  route: str,
  payload: dict[str, Any] | None = None,
  *,
  timeout: float = 10.0,
) -> dict[str, Any]:
  """Call one private identity route over the broker's Unix socket."""
  async with broker_async_client(timeout=timeout) as client:
    response = await client.request(method, route, json=payload)
    response.raise_for_status()
    value = response.json()
  if not isinstance(value, dict):
    raise ValueError("identity broker returned an invalid response")
  return value
