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
    if not key:
        raise SystemExit(
            "Live mode requires POLYMARKET_PRIVATE_KEY (the EOA private key). "
            "POLYMARKET_FUNDER is only needed for proxy/deposit wallets "
            "(signature_type 1/2/3); a plain EOA (type 0) holds funds itself. "
            "See README.md."
        )
    return {"private_key": key, "funder": os.environ.get("POLYMARKET_FUNDER", "")}
