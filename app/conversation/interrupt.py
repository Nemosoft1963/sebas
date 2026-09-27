import asyncio


class InterruptController:
    def __init__(self) -> None:
        self.event = asyncio.Event()

    def request(self) -> None:
        self.event.set()

    def reset(self) -> None:
        self.event.clear()

    def raise_if_requested(self) -> None:
        if self.event.is_set():
            raise asyncio.CancelledError("barge-in")
