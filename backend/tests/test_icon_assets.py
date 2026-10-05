"""One icon size rule: a pixel ceiling, with everything below it scaled down."""

import io

import pytest
from PIL import Image

from app import icon_assets


def test_large_icon_within_the_pixel_ceiling_is_scaled_down():
  """A large photo-sized icon is accepted everywhere, not refused at 4096 px."""
  output = io.BytesIO()
  Image.new("RGB", (5000, 4800), (20, 120, 200)).save(output, format="PNG")
  normalized = icon_assets.normalize_icon(output.getvalue())
  assert Image.open(io.BytesIO(normalized)).size == (1024, 1024)


def test_icon_beyond_the_pixel_ceiling_is_refused_before_decoding():
  output = io.BytesIO()
  Image.new("1", (6000, 6000)).save(output, format="PNG")
  with pytest.raises(icon_assets.InvalidIcon, match="32 million pixels"):
    icon_assets.normalize_icon(output.getvalue())
