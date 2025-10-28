"""
Shared typing aliases for mxm-dataio.

These model JSON-shaped data and HTTP-ish containers in a precise,
Pyright-friendly way (no `Any`). Centralizing them avoids duplication
and circular imports across api.py, models.py, store.py, and adapters.
"""

from collections.abc import Mapping, Sequence
from pathlib import Path

# Python 3.12+ style aliases (PEP 695). If you need 3.11, switch to typing.TypeAlias.
type JSONScalar = str | int | float | bool | None
type JSONLike = JSONScalar | Mapping[str, "JSONLike"] | Sequence["JSONLike"]

# Query params often support multi-values per key (?k=a&k=b)
type RequestParams = Mapping[str, JSONScalar | Sequence[JSONScalar]]

# Header containers (normalized to str values)
type HeadersLike = Mapping[str, str | Sequence[str]]

# File-system-ish helper alias if you need it in adapters
type PathLike = str | Path

type AdapterMeta = Mapping[str, JSONLike]
