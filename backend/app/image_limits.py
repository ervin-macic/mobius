"""Header-based allocation bounds shared by icons and chat image previews."""

from PIL import Image


MAX_IMAGE_PIXELS = 32_000_000
# Defense in depth for other Pillow consumers. Icons and previews check headers
# explicitly, independent of process-global limits and warning filters.
Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS


def check_image_size(image: Image.Image) -> None:
  """Refuse oversized images before loading their compressed pixel data."""
  if image.width * image.height > MAX_IMAGE_PIXELS:
    raise Image.DecompressionBombError(
      f"Image exceeds the {MAX_IMAGE_PIXELS} pixel limit."
    )
