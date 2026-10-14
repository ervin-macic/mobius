"""Stored app identities and their bound manifest fetch addresses."""

from dataclasses import dataclass
from urllib.parse import quote, unquote, urlparse

MANIFEST_ID_MARKER = "#manifest-id="
RAW_GITHUB_HOST = "raw.githubusercontent.com"


@dataclass(frozen=True)
class RootManifest:
  """A package at the root of one GitHub repository, at one ref.

  ``filename`` is ``"mobius.json"`` when the address names the manifest file
  itself and ``None`` when it names the package base.
  """

  owner: str
  repo: str
  ref: str
  filename: str | None

  @property
  def repository(self) -> tuple[str, str]:
    """GitHub owner and repository names compare case-insensitively."""
    return self.owner.lower(), self.repo.lower()

  @property
  def origin_url(self) -> str:
    return f"https://github.com/{self.owner}/{self.repo}.git"

  @property
  def url(self) -> str:
    """The raw address: the manifest file, or the package base with a slash.

    Segments are re-encoded, since ``parse_root_manifest`` decoded them: a
    ref ``fix#1`` must stay one path segment, not become a fragment.
    """
    segments = (self.owner, self.repo, self.ref)
    base = f"https://{RAW_GITHUB_HOST}/" + "".join(
      f"{quote(segment, safe='')}/" for segment in segments
    )
    return base + (self.filename or "")


def parse_root_manifest(url: str) -> RootManifest | None:
  """The one parser for raw-GitHub package addresses.

  Accepts ``https://raw.githubusercontent.com/<owner>/<repo>/<ref>`` with an
  optional trailing ``/mobius.json``, ignoring any query or fragment (a stored
  identity key carries one). The host compares case-insensitively and may name
  the default port. Anything else is not a root package and returns None: a
  manifest in a repository subdirectory, a ref containing a slash, another
  file name, credentials, or an empty, dot, dash-leading, backslash or
  encoded-slash segment.
  """
  if not isinstance(url, str) or not url:
    return None
  parsed = urlparse(url)
  try:
    port = parsed.port
  except ValueError:
    return None
  if (
    parsed.scheme.lower() != "https"
    or parsed.hostname != RAW_GITHUB_HOST
    or port not in (None, 443)
    or parsed.username is not None
    or parsed.password is not None
  ):
    return None
  parts = [unquote(part) for part in parsed.path.split("/") if part]
  filename = None
  if len(parts) == 4 and parts[3] == "mobius.json":
    filename = parts.pop()
  if len(parts) != 3 or any(
    part in ("", ".", "..") or part.startswith("-")
    or "\\" in part or "/" in part  # `%2F` decodes to a slash.
    for part in parts
  ):
    return None
  owner, repo, ref = parts
  return RootManifest(owner, repo, ref, filename)


def canonical_manifest_base(url_or_base: str) -> str:
  """Drop URL decoration so equivalent fetch locators identify one app."""
  base = url_or_base.split("#", 1)[0].split("?", 1)[0]
  if base.endswith("/mobius.json"):
    base = base[: -len("/mobius.json")]
  return base.rstrip("/")


def canonical_manifest_identity_key(url_or_base: str, manifest_id: str) -> str:
  """Identity shared by inline and URL installs; the fragment is never fetched."""
  return f"{canonical_manifest_base(url_or_base)}{MANIFEST_ID_MARKER}{manifest_id}"


def stored_manifest_fetch_url(identity_key: str) -> str:
  """The raw manifest behind an `App.manifest_url` identity key."""
  return canonical_manifest_base(identity_key) + "/mobius.json"


def requested_manifest_source(manifest_url: str) -> tuple[str, str | None]:
  """Accept an installed app's own identity key wherever a manifest is fetched.

  Returns the URL to fetch and, for an identity key, the manifest id it binds.
  The fetched package must still carry that id (or name it as `previous_id`),
  so the key keeps meaning one package. Any other URL passes through as is.
  """
  base, marker, manifest_id = manifest_url.rpartition(MANIFEST_ID_MARKER)
  if not marker or not base or not manifest_id:
    return manifest_url, None
  return stored_manifest_fetch_url(manifest_url), manifest_id


def require_permanent_package(manifest: dict, package_id: str | None) -> None:
  """An installed row's permanent `package_id` is its whole identity.

  Install, update discovery and update preview all apply this one rule, so
  discovery never offers a candidate that install would refuse.
  """
  if not package_id:
    return
  if manifest.get("package_id") is None:
    raise ValueError("This installed app requires its permanent package identity.")
  if manifest.get("package_id") != package_id:
    raise ValueError("Permanent package identity changed.")


def require_bound_manifest(
  manifest: dict, bound_id: str | None, package_id: str | None = None,
) -> None:
  """A stored address names this package or its declared predecessor only.

  When the caller knows the installed row's permanent `package_id`, only a
  manifest carrying that same `package_id` is the package, even if its
  manifest id changed without `previous_id`, as the installer's rules require.
  """
  if package_id:
    require_permanent_package(manifest, package_id)
    return
  if bound_id is not None and bound_id not in (
    manifest.get("id"), manifest.get("previous_id"),
  ):
    raise ValueError(
      f"The manifest at this address is no longer the {bound_id!r} app.",
    )
