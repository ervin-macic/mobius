"""Shared streaming response helpers for server-sent event routes."""

import anyio
from fastapi.responses import StreamingResponse


class ClosingStreamingResponse(StreamingResponse):
  """Close the event iterator when ASGI abandons an in-flight body send."""

  async def __call__(self, scope, receive, send):
    try:
      await super().__call__(scope, receive, send)
    finally:
      close = getattr(self.body_iterator, "aclose", None)
      if close is not None:
        # A server can cancel the outer ASGI task as well as Starlette's body
        # task. Re-enter the suspended async generator even in that state.
        with anyio.CancelScope(shield=True):
          await close()
