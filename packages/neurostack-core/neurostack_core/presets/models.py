"""Immutable public values for the preset subsystem."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, TypeAlias, cast

FrozenValue: TypeAlias = (
    "str | int | float | bool | None | tuple[FrozenValue, ...] | FrozenMap"
)


class PresetError(RuntimeError):
    """A preset could not be selected without violating its contract."""


class FrozenMap(Mapping[str, FrozenValue]):
    """A recursively immutable mapping with optional attribute-style lookup."""

    __slots__ = ("_data",)
    _data: Mapping[str, FrozenValue]

    def __init__(self, values: Mapping[str, FrozenValue]):
        object.__setattr__(self, "_data", MappingProxyType(dict(values)))

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError(f"{type(self).__name__} is immutable")

    def __getitem__(self, key: str) -> FrozenValue:
        return self._data[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def __getattr__(self, name: str) -> FrozenValue:
        try:
            return self._data[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def __repr__(self) -> str:
        return f"FrozenMap({dict(self._data)!r})"


def freeze(value: Any) -> FrozenValue:
    """Recursively freeze a parsed TOML value."""

    if isinstance(value, Mapping):
        return FrozenMap({str(key): freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(freeze(item) for item in value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return cast(FrozenValue, value)
    raise TypeError(f"unsupported preset value type: {type(value).__name__}")


@dataclass(frozen=True, slots=True)
class Preset:
    """One validated, deeply immutable four-module preset."""

    name: str
    source_id: str
    srm: FrozenMap
    flow: FrozenMap
    policy: FrozenMap
    prompt: FrozenMap


__all__ = ["FrozenMap", "Preset", "PresetError"]
