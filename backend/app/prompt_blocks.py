"""Keep recalled text inside the tagged block that carries it to the agent."""

from __future__ import annotations

import re


def as_block_data(text: str, tag: str) -> str:
  """Neutralize ``<tag>`` and ``</tag>`` inside text recalled into that block.

  Recalled text — model output above all — could hold a literal closing tag
  that ends the block early and lets the rest read as prose outside it. The
  match tolerates case and stray whitespace (``</ Side_Questions >``) because
  a model reading the prompt would. Only the ``<`` of a match changes, so
  every other character — code, markup, markdown — reaches the agent intact.
  """
  pattern = re.compile(rf"<\s*/?\s*{re.escape(tag)}\b", re.IGNORECASE)
  return pattern.sub(lambda match: "&lt;" + match.group(0)[1:], text)
