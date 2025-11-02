from .data_types import XEMMPerpetualExecutorConfig

__all__ = [
    "XEMMPerpetualExecutorConfig",
    "XEMMPerpetualExecutor",
]


def __getattr__(name):  # pragma: no cover - module level convenience import
    if name == "XEMMPerpetualExecutor":
        from .xemm_perpetual_executor import XEMMPerpetualExecutor

        return XEMMPerpetualExecutor
    raise AttributeError(f"module {__name__} has no attribute {name}")
