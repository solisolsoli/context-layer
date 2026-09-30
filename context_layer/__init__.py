"""Packaging and entry layer for the context-layer router and evaluation harness.

This package adds a single command group (`context-layer`) and a vault scanner
that bootstraps a starting routing config. It does not reimplement the router,
the indexer or the evaluation harness; those stay in `router/` and `eval/` and
remain runnable directly with `python3`.
"""

__version__ = "0.4.0"
