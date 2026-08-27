WAKEUP_EXCHANGE_NAME = "qa_platform_learning.runtime.wakeup"
WAKEUP_QUEUE_NAME = "qa_platform_learning.automation.wakeup"
WAKEUP_ROUTING_KEY = "automation.task.available"
WAKEUP_MESSAGE_TYPE = "qa.automation.task_available"

# This is intentionally constant. The database remains the source of truth and
# the broker never transports task payloads, commands, credentials, or IDs.
WAKEUP_HINT_BODY = b'{"kind":"automation_task_available","version":1}'


__all__ = [
    "WAKEUP_EXCHANGE_NAME",
    "WAKEUP_HINT_BODY",
    "WAKEUP_MESSAGE_TYPE",
    "WAKEUP_QUEUE_NAME",
    "WAKEUP_ROUTING_KEY",
]
