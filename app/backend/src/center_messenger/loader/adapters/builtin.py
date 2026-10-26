"""Bridges to existing durable queues. Never start their legacy thread loops."""

from ..contracts import Capability as C, Manifest, Step


def register_builtin(registry, services, config):
    if config.crm_sync_worker and services.alfa_sync:
        registry.register(Manifest("alfa_directory", frozenset({C.DIRECTORY})), lambda: [
            Step("page", C.DIRECTORY, lambda op: services.alfa_sync.run_next(),
                 lane="maintenance", active_delay=.2, idle_delay=2, budget=90)])
    native_read = bool(config.crm_sync_worker and services.alfa_max)
    native_send = bool(config.max_worker and services.messaging)
    if native_read or native_send:
        def native():
            steps = []
            if native_read:
                from center_messenger.alfa_max import AlfaMaxWorker
                worker = AlfaMaxWorker(services.alfa_max)
                steps.append(Step("history", C.RECEIVE, lambda op: worker.run_next(),
                                  active_delay=.01, idle_delay=.25, budget=300))
            if native_send:
                steps.append(Step("outgoing", C.SEND, lambda op: services.messaging.run_next("send", "alfa_max_browser", stopping=op.stopping),
                                  idle_delay=.5, budget=90))
            return steps
        capabilities = ({C.RECEIVE} if native_read else set()) | ({C.SEND} if native_send else set())
        registry.register(Manifest("alfa_max", frozenset(capabilities)), native)
    if config.max_worker and services.messaging:
        registry.register(Manifest("max_bot", frozenset({C.RECEIVE, C.SEND})), lambda: [
            Step("incoming", C.RECEIVE, lambda op: services.messaging.run_next("event", "max_bot"), idle_delay=.5),
            Step("outgoing", C.SEND, lambda op: services.messaging.run_next("send", "max_bot"), idle_delay=.5)])
    if services.accounts:
        def projection():
            from center_messenger.chat_queue import QueueProjectionWorker
            worker = QueueProjectionWorker(services.accounts)
            return [Step("batch", C.PROJECTION, lambda op: worker.run_next(), lane="maintenance", idle_delay=10)]
        registry.register(Manifest("chat_projection", frozenset({C.PROJECTION})), projection)
    if config.crm_sync_worker and services.alfa:
        def cards():
            from center_messenger.family_cache import FamilyCacheWorker
            worker = FamilyCacheWorker(services.alfa)
            return [Step("card", C.CARDS, lambda op: worker.run_next(), lane="maintenance",
                         active_delay=2, idle_delay=2, budget=90)]
        def review():
            from center_messenger.active_review import ActiveReviewWorker
            worker = ActiveReviewWorker(services.alfa)
            return [Step("page", C.DIRECTORY, lambda op: worker.run_next(), lane="maintenance",
                         active_delay=3, idle_delay=3, budget=90)]
        def identities():
            from center_messenger.customer_lookup import CustomerLookupWorker
            worker = CustomerLookupWorker(services.alfa, services.alfa_max)
            return [Step("customer", C.DIRECTORY, lambda op: worker.run_next(), lane="maintenance",
                         active_delay=.2, idle_delay=2, budget=60)]
        registry.register(Manifest("crm_cards", frozenset({C.CARDS})), cards)
        registry.register(Manifest("crm_active_review", frozenset({C.DIRECTORY})), review)
        if services.alfa_max:
            registry.register(Manifest("crm_customer_lookup", frozenset({C.DIRECTORY})), identities)
