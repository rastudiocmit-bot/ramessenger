"""The application composition root: register additional verified adapters here."""

from .adapters.builtin import register_builtin
from .registry import Registry
from .scheduler import Scheduler


def build_loader(services, config):
    registry = Registry()
    register_builtin(registry, services, config)
    # Future providers register lazy factories here. Credentials and enabled
    # connections are resolved by their authorized setup/connection services.
    return Scheduler(registry)
