"""Stored app identities and their bound manifest fetch addresses."""

from fastapi import HTTPException


_MANIFEST_ID_MARKER = "#manifest-id="


def _canonical_base(url_or_base: str) -> str:
  """Drop URL decoration so equivalent fetch locators identify one app."""
  base = url_or_base.split("#", 1)[0].split("?", 1)[0]
  if base.endswith("/mobius.json"):
    base = base[: -len("/mobius.json")]
  return base.rstrip("/")


def _canonical_identity_key(url_or_base: str, manifest_id: str) -> str:
  """Identity shared by inline and URL installs; the fragment is never fetched."""
  return f"{_canonical_base(url_or_base)}{_MANIFEST_ID_MARKER}{manifest_id}"


def stored_manifest_fetch_url(identity_key: str) -> str:
  """The raw manifest behind an `App.manifest_url` identity key."""
  return _canonical_base(identity_key) + "/mobius.json"


def requested_manifest_source(manifest_url: str) -> tuple[str, str | None]:
  """Accept an installed app's own identity key wherever a manifest is fetched.

  Returns the URL to fetch and, for an identity key, the manifest id it binds.
  The fetched package must still carry that id (or name it as `previous_id`),
  so the key keeps meaning one package. Any other URL passes through as is.
  """
  base, marker, manifest_id = manifest_url.rpartition(_MANIFEST_ID_MARKER)
  if not marker or not base or not manifest_id:
    return manifest_url, None
  return stored_manifest_fetch_url(manifest_url), manifest_id


def require_bound_manifest(manifest: dict, bound_id: str | None) -> None:
  """A stored address names this package or its declared predecessor only."""
  if bound_id is not None and bound_id not in (
    manifest.get("id"), manifest.get("previous_id"),
  ):
    raise HTTPException(
      409,
      f"The manifest at this address is no longer the {bound_id!r} app.",
    )

