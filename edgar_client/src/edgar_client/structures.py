"""
Utility data structures for edgar_client.

CaseInsensitiveDict mirrors the one in requests/structures.py: it lets HTTP
header lookups succeed regardless of capitalisation ("content-type" vs
"Content-Type"), while preserving the original casing for serialisation.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, MutableMapping
from typing import TypeVar

_VT = TypeVar("_VT")


class CaseInsensitiveDict(MutableMapping[str, _VT]):
    """A dict subclass whose keys are compared case-insensitively.

    Internally stores ``{lowercase_key: (original_key, value)}``.  Iteration
    yields keys in their original capitalisation, preserving insertion order.
    """

    def __init__(
        self,
        data: Mapping[str, _VT] | None = None,
        **kwargs: _VT,
    ) -> None:
        self._store: dict[str, tuple[str, _VT]] = {}
        if data:
            self.update(data)
        if kwargs:
            self.update(kwargs)  # type: ignore[arg-type]

    def __setitem__(self, key: str, value: _VT) -> None:
        self._store[key.lower()] = (key, value)

    def __getitem__(self, key: str) -> _VT:
        return self._store[key.lower()][1]

    def __delitem__(self, key: str) -> None:
        del self._store[key.lower()]

    def __iter__(self) -> Iterator[str]:
        return (original for original, _ in self._store.values())

    def __len__(self) -> int:
        return len(self._store)

    def __contains__(self, key: object) -> bool:
        if not isinstance(key, str):
            return False
        return key.lower() in self._store

    def lower_items(self) -> Iterator[tuple[str, _VT]]:
        """Yield ``(lowercase_key, value)`` pairs."""
        return ((k, v) for k, (_, v) in self._store.items())

    def copy(self) -> "CaseInsensitiveDict[_VT]":
        new = CaseInsensitiveDict[_VT]()
        new._store = self._store.copy()
        return new

    def __repr__(self) -> str:
        return f"{type(self).__name__}({dict(self.items())!r})"

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Mapping):
            other = CaseInsensitiveDict(other)  # type: ignore[assignment]
        else:
            return NotImplemented
        return dict(self.lower_items()) == dict(other.lower_items())  # type: ignore[attr-defined]
