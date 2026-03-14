from .codex import CodexProviderAdapter

_ADAPTERS = {
    "codex": CodexProviderAdapter(),
}


def resolve_provider_adapter(name: str):
    key = name.strip().lower()
    if key not in _ADAPTERS:
        raise ValueError(f"unsupported provider adapter: {name}")
    return _ADAPTERS[key]
