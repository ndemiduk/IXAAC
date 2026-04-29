"""Thin wrappers around the two xAI SDKs we use.

- `xai_sdk.Client` for collections (upload / search / list / delete)
- `openai.OpenAI` pointed at api.x.ai for chat completions w/ tool use
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator, Optional

from openai import OpenAI
from xai_sdk import Client as XAIClient

from xli.config import GlobalConfig, KeyPair


class MissingCredentials(RuntimeError):
    pass


@dataclass
class Clients:
    xai: XAIClient   # collections
    chat: OpenAI     # chat completions
    label: str = "primary"

    @classmethod
    def from_keypair(cls, kp: KeyPair, *, require_management: bool = True) -> "Clients":
        if not kp.api_key:
            raise MissingCredentials("api_key missing for key pair")
        if require_management and not kp.management_api_key:
            raise MissingCredentials(
                f"management_api_key missing for key '{kp.label or '?'}' — required for Collections."
            )
        return cls(
            xai=XAIClient(api_key=kp.api_key, management_api_key=kp.management_api_key),
            chat=OpenAI(api_key=kp.api_key, base_url="https://api.x.ai/v1"),
            label=kp.label or "primary",
        )

    @classmethod
    def from_config(cls, cfg: GlobalConfig, *, require_management: bool = True) -> "Clients":
        pairs = cfg.key_pairs()
        if not pairs:
            raise MissingCredentials(
                "XAI_API_KEY not set. Export it or write it to ~/.config/xli/config.json."
            )
        return cls.from_keypair(pairs[0], require_management=require_management)


def iter_collection_documents(xai: XAIClient, collection_id: str) -> Iterator[object]:
    """Yield every DocumentMetadata in a collection, paging until exhausted."""
    pagination_token: Optional[str] = None
    while True:
        resp = xai.collections.list_documents(
            collection_id=collection_id,
            limit=500,
            pagination_token=pagination_token,
        )
        for doc in resp.documents:
            yield doc
        pagination_token = resp.pagination_token or None
        if not pagination_token or not resp.documents:
            break
