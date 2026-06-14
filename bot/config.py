import os
from dataclasses import dataclass, field
from typing import Any

import yaml


@dataclass
class Config:
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str = "config.yaml") -> "Config":
        with open(path) as f:
            return cls(raw=yaml.safe_load(f))

    def __getitem__(self, key: str) -> Any:
        return self.raw[key]

    def get(self, *keys: str, default: Any = None) -> Any:
        node: Any = self.raw
        for k in keys:
            if not isinstance(node, dict) or k not in node:
                return default
            node = node[k]
        return node


def live_credentials() -> dict[str, str]:
    key = os.environ.get("POLYMARKET_PRIVATE_KEY", "")
    funder = os.environ.get("POLYMARKET_FUNDER", "")
    if not key or not funder:
        raise SystemExit(
            "Live mode requires POLYMARKET_PRIVATE_KEY and POLYMARKET_FUNDER "
            "environment variables. See README.md."
        )
    return {"private_key": key, "funder": funder}
