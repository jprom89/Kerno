"""
LLM client factory for Kerno.

What this module does
---------------------
Provides get_llm_client(), a factory that returns a configured Mistral SDK
client pinned to Mistral's EU server endpoint (server="eu"). All chat/mapping
LLM calls in the codebase must go through this factory so that the provider,
EU routing, and API key are configured in exactly one place.

Why this module exists
----------------------
Kerno is an EU-native compliance SaaS selling NIS2/DORA/CRA/EU AI Act coverage
to European enterprises. Using a US-incorporated AI provider (OpenAI, Anthropic,
Cohere) exposes customer compliance data to the US CLOUD Act, directly
contradicting our product promise. Mistral AI SAS (Paris, France) is the only
major frontier LLM provider incorporated in the EU with no US-jurisdiction
exposure. This factory makes the EU-provider requirement explicit and auditable:
any future provider change must go through this module and be accompanied by a
documented sovereignty assessment.

Not the same as the embedding provider
--------------------------------------
EMBEDDING_API_KEY (read elsewhere in the codebase) configures a separate
provider integration — the embedding model service used for vector similarity
search. It is intentionally distinct from this chat/mapping LLM client and its
MISTRAL_API_KEY. Do not conflate the two: they are different services, different
keys, and may resolve to different vendors. This factory governs only the
chat/mapping LLM.

How to use
----------
    from src.services.llm_client import get_llm_client

    client = get_llm_client()
    response = client.chat.complete(model=..., messages=...)

Environment variables required
------------------------------
MISTRAL_API_KEY   — API key from console.mistral.ai
KERNO_LLM_MODEL   — model to use, default "mistral-large-latest"

How to test
-----------
    pytest tests/unit/services/test_mapping_service.py -v
"""

from __future__ import annotations

import os

from mistralai.client import SERVER_EU, Mistral

from src.exceptions import ConfigurationError

__all__ = ["get_llm_client"]


def get_llm_client() -> Mistral:
    """Return a configured Mistral client pinned to the EU server, reading MISTRAL_API_KEY from the environment.

    Passes server=SERVER_EU so requests are routed to Mistral's EU endpoint, keeping
    inference inside EU jurisdiction. Raises ConfigurationError if MISTRAL_API_KEY is
    unset or empty, so a missing deployment secret fails loudly rather than reaching
    the provider as an anonymous call.
    """
    api_key = os.environ.get("MISTRAL_API_KEY")
    if not api_key:
        raise ConfigurationError(
            "MISTRAL_API_KEY environment variable is not set. "
            "Set it to your Mistral AI key (https://console.mistral.ai)."
        )
    return Mistral(api_key=api_key, server=SERVER_EU)
