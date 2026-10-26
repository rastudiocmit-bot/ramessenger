"""Explicit lazy registration: a broken optional factory cannot break healthy ones."""

from .contracts import CONTRACT_VERSION, Manifest, Step


class Registry:
    def __init__(self):
        self._providers = {}
        self.errors = {}

    def register(self, manifest: Manifest, factory):
        if manifest.provider in self._providers:
            # Never replace the healthy registration with the duplicate.
            self.errors[manifest.provider + ".duplicate"] = "duplicate_provider"
            return False
        if manifest.version != CONTRACT_VERSION:
            self.errors[manifest.provider] = "incompatible_contract"
            return False
        if not callable(factory):
            self.errors[manifest.provider] = "invalid_factory"
            return False
        self._providers[manifest.provider] = (manifest, factory)
        return True

    def resolve(self):
        resolved = {}
        for name, (manifest, factory) in self._providers.items():
            try:
                steps = tuple(factory())
                if not steps or any(not isinstance(s, Step) or s.capability not in manifest.capabilities for s in steps):
                    raise ValueError("Invalid provider steps")
                if len({s.name for s in steps}) != len(steps):
                    raise ValueError("Duplicate provider step")
                resolved.update({f"{name}.{s.name}": s for s in steps})
                self.errors.pop(name, None)
            except Exception:
                # No exception message: third-party exceptions can contain credentials.
                self.errors[name] = "adapter_initialization_failed"
        return resolved
