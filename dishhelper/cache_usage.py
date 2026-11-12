"""Normalize reported prompt-cache counters without inventing missing usage.

Providers may return several aliases for the same counter. They are reconciled,
not added. Cache writes are a subset of input, not an extra input-token total.
Only OpenAI-style full-input totals and DeepSeek counters are normalized here;
Anthropic-only usage has different input semantics and stays in raw reporting.
"""

_MISSING = object()
_INPUT = ("prompt_tokens", "input_tokens")
_HIT = ("prompt_cache_hit_tokens", "prompt_tokens_details.cached_tokens",
        "input_tokens_details.cached_tokens", "cached_tokens")
_MISS = ("prompt_cache_miss_tokens",)
_WRITE = ("prompt_tokens_details.cache_write_tokens", "input_tokens_details.cache_write_tokens",
          "cache_write_tokens")
_CACHE_FIELDS = _HIT + _MISS + _WRITE


def _at(usage, path):
    value = usage
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return _MISSING
        value = value[part]
    return value


def _token(value):
    return type(value) is int and value >= 0


def reported_cache_token_fields(usage):
    """Provider-named raw cache counters for audit tables; aliases may overlap."""
    if not isinstance(usage, dict):
        return {}
    return {path: value for path in _CACHE_FIELDS if _token(value := _at(usage, path))}


def cache_usage(usage):
    """Return known counters or None; a missing cache report never means zero."""
    result = {"input_tokens": None, "cache_hit_tokens": None, "cache_miss_tokens": None,
              "cache_write_tokens": None, "cache_hit_ratio": None, "status": "unreported",
              "source": "unknown", "issues": [], "basis": "reported_usage_only"}
    if usage is None:
        return result
    if not isinstance(usage, dict):
        result.update(status="invalid", issues=["invalid_usage_object"])
        return result
    paths = [path for path in _CACHE_FIELDS if _at(usage, path) is not _MISSING]
    if not paths and any(key in usage for key in ("cache_read_input_tokens", "cache_creation_input_tokens")):
        result.update(status="unsupported", source="anthropic_style",
                      issues=["input_total_semantics_not_normalized"])
        return result
    sources = []
    if any(path.startswith("prompt_cache_") for path in paths):
        sources.append("deepseek")
    if any(path.startswith("prompt_tokens_details.") for path in paths):
        sources.append("openai_chat")
    if any(path.startswith("input_tokens_details.") for path in paths):
        sources.append("openai_responses")
    if any(path in ("cached_tokens", "cache_write_tokens") for path in paths):
        sources.append("top_level")
    result["source"] = "+".join(sources) or "unknown"
    issues = result["issues"]

    def counter(aliases, name):
        values = [_at(usage, path) for path in aliases if _at(usage, path) is not _MISSING]
        if not values:
            return None
        if any(not _token(value) for value in values):
            issues.append("invalid_" + name)
            return None
        if len(set(values)) != 1:
            issues.append("conflicting_" + name)
            return None
        return values[0]

    total = counter(_INPUT, "input_tokens")
    hit = counter(_HIT, "cache_hit_tokens")
    miss = counter(_MISS, "cache_miss_tokens")
    write = counter(_WRITE, "cache_write_tokens")
    for key in ("prompt_tokens_details", "input_tokens_details"):
        if key in usage and usage[key] is not None and not isinstance(usage[key], dict):
            issues.append("invalid_cache_details")
    if total is not None:
        if any(value is not None and value > total for value in (hit, miss, write)):
            issues.append("cache_counter_exceeds_input")
        if hit is not None and miss is not None and hit + miss != total:
            issues.append("cache_hit_miss_total_mismatch")
        if hit is not None and write is not None and hit + write > total:
            issues.append("cache_write_exceeds_uncached_input")
    if miss is not None and write is not None and write > miss:
        issues.append("cache_write_exceeds_uncached_input")
    result["input_tokens"] = total
    if issues:
        result["status"] = "invalid"
        return result
    if miss is None and total is not None and hit is not None:
        miss = total - hit
    result.update(cache_hit_tokens=hit, cache_miss_tokens=miss, cache_write_tokens=write)
    if total is not None and hit is not None:
        result["status"] = "reported"
        result["cache_hit_ratio"] = hit / total if total else None
    elif paths:
        result["status"] = "partial"
    return result


def summarize_cache_usage(usages):
    """Weighted cache ratio over matching reported input/hit coverage only.

    A caller-supplied usage may already merge attempts. This helper cannot infer
    counters omitted by those attempts; prefer passing individual attempt usage.
    """
    rows = [cache_usage(usage) for usage in usages]
    result = {"calls": len(rows), "basis": "reported_usage_only"}
    for field in ("input_tokens", "cache_hit_tokens", "cache_miss_tokens", "cache_write_tokens"):
        values = [row[field] for row in rows if row[field] is not None]
        result[field] = sum(values) if values else None
        result["calls_with_" + field] = len(values)
    covered = [row for row in rows if row["input_tokens"] is not None and row["cache_hit_tokens"] is not None]
    denominator = sum(row["input_tokens"] for row in covered)
    numerator = sum(row["cache_hit_tokens"] for row in covered)
    result.update(calls_with_cache_ratio=len(covered), rate_input_tokens=denominator if covered else None,
                  rate_cache_hit_tokens=numerator if covered else None,
                  cache_hit_ratio=numerator / denominator if denominator else None,
                  calls_without_cache_report=sum(row["status"] == "unreported" for row in rows),
                  calls_with_invalid_cache_usage=sum(row["status"] == "invalid" for row in rows),
                  calls_with_unsupported_cache_usage=sum(row["status"] == "unsupported" for row in rows),
                  calls_with_partial_cache_usage=sum(row["status"] == "partial" for row in rows))
    return result
