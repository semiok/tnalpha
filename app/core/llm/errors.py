"""Shared LLM error types."""


class ModelRateLimited(RuntimeError):
    """The selected model/provider is temporarily rate limited."""
