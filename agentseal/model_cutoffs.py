"""Model/corpus cutoff presets for temporal alignment.

These presets are convenience defaults. Treat them as approximate metadata and
review them before using a report for publication. Use ``model=none`` when no
temporal cutoff should be applied.
"""

from __future__ import annotations


# Model cutoff database — {name: (cutoff_date, description)}
# Dates are ISO 8601 (YYYY-MM-DD). Patches merged AFTER this date cannot
# be in that model's training corpus.
MODEL_CUTOFFS: dict[str, tuple[str, str]] = {
    # ── OpenAI (presets) ──
    "gpt-4": ("2023-04-27", "GPT-4 (base)"),
    "gpt-4-turbo": ("2023-12-01", "GPT-4 Turbo"),
    "gpt-4o": ("2023-10-01", "GPT-4o"),
    "gpt-4.5": ("2024-06-01", "GPT-4.5"),
    "o1": ("2023-10-01", "OpenAI o1"),
    "o3": ("2024-06-01", "OpenAI o3"),
    "o3-mini": ("2024-06-01", "OpenAI o3-mini"),
    "gpt-5": ("2024-09-30", "GPT-5 (training cutoff Sep 2024)"),
    "gpt-5.2": ("2025-08-01", "GPT-5.2 (estimated)"),

    # ── Anthropic (presets) ──
    "claude-3": ("2023-08-01", "Claude 3 family"),
    "claude-3.5": ("2024-04-01", "Claude 3.5 Sonnet/Haiku"),
    "claude-3.7": ("2024-11-01", "Claude 3.7 Sonnet"),
    "claude-4": ("2025-03-01", "Claude 4 Opus/Sonnet"),
    "claude-opus-4": ("2025-03-01", "Claude Opus 4"),
    "claude-sonnet-4": ("2025-03-01", "Claude Sonnet 4"),
    "claude-opus-4.5": ("2025-08-01", "Claude Opus 4.5"),
    "claude-sonnet-4.5": ("2025-08-01", "Claude Sonnet 4.5"),
    "claude-4.6": ("2025-08-01", "Claude 4.6 Opus"),

    # ── Google (presets) ──
    "gemini-1.5": ("2023-12-01", "Gemini 1.5 Pro/Flash"),
    "gemini-2": ("2024-06-01", "Gemini 2.0"),
    "gemini-2.5": ("2025-01-01", "Gemini 2.5 Pro"),
    "gemini-3": ("2025-06-01", "Gemini 3"),
    "gemini-3-flash": ("2025-06-01", "Gemini 3 Flash"),

    # ── Meta (presets) ──
    "llama-3": ("2023-12-01", "Llama 3 / 3.1"),
    "llama-3.2": ("2024-06-01", "Llama 3.2"),
    "llama-3.3": ("2024-12-01", "Llama 3.3"),
    "llama-4": ("2025-03-01", "Llama 4"),
    "llama-4-maverick": ("2025-03-01", "Llama 4 Maverick"),
    "llama-4-scout": ("2025-03-01", "Llama 4 Scout"),

    # ── Mistral (presets) ──
    "mistral-large": ("2024-06-01", "Mistral Large 2"),
    "mistral-large-3": ("2025-01-01", "Mistral Large 3"),
    "mistral-7b": ("2023-12-01", "Mistral 7B v0.3"),
    "mixtral": ("2024-06-01", "Mixtral 8x22B"),
    "codestral": ("2024-06-01", "Codestral"),
    "codestral-2": ("2025-01-01", "Codestral 2"),

    # ── DeepSeek (presets) ──
    "deepseek-v2": ("2024-06-01", "DeepSeek V2"),
    "deepseek-v3": ("2024-12-01", "DeepSeek V3"),
    "deepseek-r1": ("2024-12-01", "DeepSeek R1"),
    "deepseek-r2": ("2025-06-01", "DeepSeek R2 (estimated)"),

    # ── Qwen (presets) ──
    "qwen-2": ("2024-06-01", "Qwen 2 / 2.5"),
    "qwen-2.5": ("2024-09-01", "Qwen 2.5"),
    "qwen-3": ("2025-06-01", "Qwen 3"),
    "qwen-coder": ("2024-09-01", "Qwen 2.5 Coder"),

    # ── Cohere ──
    "command-r": ("2024-06-01", "Command R+"),
    "command-r-plus": ("2024-06-01", "Command R+"),
    "command-a": ("2025-01-01", "Command A"),

    # ── xAI (presets) ──
    "grok-1.5": ("2023-12-01", "Grok 1.5"),
    "grok-2": ("2024-06-01", "Grok 2"),
    "grok-3": ("2025-02-01", "Grok 3"),
    "grok-4": ("2025-08-01", "Grok 4 (estimated)"),

    # ── Other notable models ──
    "kimi-k2": ("2025-01-01", "Moonshot Kimi K2"),
    "kimi-k2.5": ("2025-06-01", "Moonshot Kimi K2.5"),
    "phi-4": ("2024-06-01", "Microsoft Phi-4"),
    "yi-large": ("2024-06-01", "01.AI Yi-Large"),
    "glm-5": ("2025-01-01", "Z.ai GLM-5"),
    "glm-5.2": ("2025-06-01", "Z.ai GLM-5.2"),

    # ── Datasets (for corpus-level alignment) ──
    "stack-v2": ("2024-03-15", "The Stack v2 (Code Llama / StarCoder training data)"),
    "stack-v1": ("2023-07-01", "The Stack v1"),
    "common-crawl": ("2024-06-01", "Common Crawl (rolling, approximate)"),

    # ── Generic ──
    "none": ("9999-12-31", "No cutoff (all patches eligible)"),
}


def get_model_cutoff(model_name: str) -> tuple[str, str] | None:
    """Look up a model's training cutoff.

    Returns (cutoff_date, description) or None if unknown.
    Case-insensitive. Strips common suffixes/prefixes.
    """
    if not model_name:
        return None
    key = model_name.lower().strip()
    # Try exact match
    if key in MODEL_CUTOFFS:
        return MODEL_CUTOFFS[key]
    # Try with common variations
    for k, v in MODEL_CUTOFFS.items():
        if key == k or key == k.replace("-", "") or key == k.replace("-", "_"):
            return v
    return None


def list_models() -> list[tuple[str, str, str]]:
    """List all available models with their cutoffs.

    Returns list of (name, cutoff_date, description) sorted by name.
    """
    return sorted(
        [(k, v[0], v[1]) for k, v in MODEL_CUTOFFS.items()],
        key=lambda x: x[0],
    )


__all__ = ["MODEL_CUTOFFS", "get_model_cutoff", "list_models"]
