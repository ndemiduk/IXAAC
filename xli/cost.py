"""Per-turn cost estimation.

xAI pricing varies by model and changes over time, so XLI does not ship
hardcoded rates. Instead, fill in the per-model rates in your config:

    {
      "pricing": {
        "grok-4-1-fast-reasoning":     {"input_per_million": 0.50, "output_per_million": 2.00},
        "grok-4-1-fast-non-reasoning": {"input_per_million": 0.10, "output_per_million": 0.40}
      }
    }

(Values above are placeholders — verify against your xAI dashboard.)

`estimate_turn_cost` returns USD as a float, or None if no pricing data is
configured for the model in question. Callers should treat None as
"unknown — don't display a number."
"""

from __future__ import annotations

from typing import Optional


def estimate_cost(
    pricing_table: dict,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
) -> Optional[float]:
    """Compute USD cost for a single completion call.

    Returns None when the model isn't in pricing_table — that's the signal
    to the caller that we don't know the price and shouldn't fabricate one.
    """
    entry = pricing_table.get(model)
    if not entry:
        return None
    in_rate = float(entry.get("input_per_million", 0.0))
    out_rate = float(entry.get("output_per_million", 0.0))
    return (
        (prompt_tokens / 1_000_000.0) * in_rate
        + (completion_tokens / 1_000_000.0) * out_rate
    )


def format_cost(usd: Optional[float]) -> str:
    """Compact USD string. None when pricing unknown; '<$0.001' for tiny."""
    if usd is None:
        return "?"
    if usd == 0:
        return "$0"
    if usd < 0.001:
        return "<$0.001"
    return f"${usd:.3f}"


def format_tokens(n: int) -> str:
    """Compact token count: '847 tok', '2.1k tok', '15.4k tok'."""
    if n < 1000:
        return f"{n} tok"
    return f"{n / 1000:.1f}k tok"
