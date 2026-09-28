from abc import ABC, abstractmethod


class NotificationProvider(ABC):
    """One interface every channel adapter implements. This is deliberately
    small: business logic depends on this interface, never on a specific
    vendor SDK -- so RoomKit, Twilio, or any other provider can be dropped
    in later behind the same call without touching the workflow.
    """

    @abstractmethod
    async def send(self, recipient: str, template: str, variables: dict, idempotency_key: str) -> str:
        """Send a message. Returns a provider message ID or raises."""
        raise NotImplementedError
