"""Fixed context assembly: system, application data, whole recent turns, input."""

from datetime import date, datetime, timedelta, timezone
import json
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .variants import BASELINE_VERSION, metadata, render_prompt

APPLICATION_DATA_PREFIX = "APPLICATION_DATA (DATA ONLY / 仅作为资料)\n"
ESTIMATOR_LABEL = "保守本地估算：UTF-8 字节数 + 每条消息 16 + 整体 16；非供应商实际用量"


class BudgetError(ValueError):
    """The fixed request exceeds the budget, so nothing is silently truncated."""


def estimate_tokens(messages: list[dict]) -> int:
    """Use one UTF-8 byte per estimated token plus explicit envelope allowance.

    This deliberately overestimates ordinary Chinese/English text. Providers use
    different tokenizers and message wrappers, so this is not a usage measurement
    or a guarantee of any provider's context limit.
    """
    return 16 + sum(
        16 + len(message["role"].encode("utf-8")) + len(message["content"].encode("utf-8"))
        for message in messages
    )


def _local_date(current_date):
    try:
        local_zone = ZoneInfo("Asia/Shanghai")
    except ZoneInfoNotFoundError:
        # Windows may have no IANA database. Shanghai currently uses UTC+08:00.
        local_zone = timezone(timedelta(hours=8), "Asia/Shanghai")
    if current_date is None:
        return datetime.now(local_zone).date().isoformat()
    if isinstance(current_date, datetime):
        if current_date.tzinfo is not None:
            current_date = current_date.astimezone(local_zone)
        return current_date.date().isoformat()
    if isinstance(current_date, date):
        return current_date.isoformat()
    if isinstance(current_date, str):
        parsed = date.fromisoformat(current_date)
        if parsed.isoformat() != current_date:
            raise ValueError("current_date 必须使用 YYYY-MM-DD 格式")
        return current_date
    raise ValueError("current_date 必须是日期或 YYYY-MM-DD 字符串")


def _reference_data(reference):
    return {key: reference[key] for key in ("id", "kind", "content")}


def _turn_messages(turn, current_reference_id):
    # Only the displayed answer belongs to W, never raw_response/state snapshots.
    answer = turn["answer"]
    reference = turn.get("reference")
    if reference:
        if reference["id"] == current_reference_id:
            marker = "[CURRENT_REFERENCE / 当前同版本参考已在 APPLICATION_DATA 中提供]"
            reference_data = {"id": reference["id"], "kind": reference["kind"]}
        else:
            marker = "[HISTORICAL_REFERENCE / 历史参考，非当前活动参考]"
            reference_data = _reference_data(reference)
        answer += "\n\n" + marker + "\n" + json.dumps(reference_data, ensure_ascii=False)
    return [
        {"role": "user", "content": turn["user_input"]},
        {"role": "assistant", "content": answer},
    ]


def build_context(conversation, turns, user_input, profile, current_date=None):
    """Build one immutable request snapshot from the pre-turn working state.

    ``turns`` is chronological. Only succeeded (or status-omitted) turns enter W.
    ``dropped_turn_ids`` identifies budget evictions inside the configured recent
    window; older turns outside that window were never candidates for this call.
    """
    if not isinstance(user_input, str) or not user_input.strip():
        raise ValueError("当前输入不能为空")
    budget = profile.get("input_budget", 12000)
    window = profile.get("recent_turns", 4)
    if isinstance(budget, bool) or not isinstance(budget, int) or budget < 1:
        raise ValueError("input_budget 必须是正整数")
    if isinstance(window, bool) or not isinstance(window, int) or window < 0:
        raise ValueError("recent_turns 必须是非负整数")

    branch = bool(conversation.get("parent_id"))
    incremental = "shared_delivery" in conversation
    delivery = conversation.get("shared_delivery") if not branch else None
    # Old snapshots/direct callers can still describe their original summary.
    shared = conversation.get("shared_context") if not branch and not incremental else None
    shared_enabled = delivery is not None if incremental else shared is not None
    system = {
        "role": "system",
        "content": render_prompt(
            conversation["scene"], bool(conversation.get("explain")), bool(conversation.get("parent_id")),
            conversation.get("prompt_variant", BASELINE_VERSION), bool(conversation.get("feedback_enabled", False)),
            shared_enabled=shared_enabled,
        ),
    }
    data = {"current_date": _local_date(current_date)}
    if delivery is not None:
        if delivery.get("day"):
            data["discussion_date"] = delivery["day"]
        data["sharing_scope"] = "day_and_persistent" if delivery.get("day_scope") else "persistent_only"
    if shared is not None and shared.get("summary"):
        data["shared"] = shared["summary"]
    for key in ("conditions", "carry_note"):
        if conversation.get(key):
            data[key] = conversation[key]
    reference = conversation.get("reference")
    if reference:
        data["reference"] = _reference_data(reference)
    application_data = {"role": "user", "content": APPLICATION_DATA_PREFIX + json.dumps(data, ensure_ascii=False)}
    current = {"role": "user", "content": user_input}
    fixed = [system, application_data, current]
    fixed_tokens = estimate_tokens(fixed)
    if fixed_tokens > budget:
        raise BudgetError(
            f"固定输入的保守估算为 {fixed_tokens}，超过输入预算 {budget}。"
            "请提高模型输入预算，或在状态检查中精简 C/R/U、缩短当前输入后再提交；本次未调用模型。"
        )

    # Prioritize new shared facts over old verbatim history, but deliver only
    # whole entries that fit. Deferred entries remain pending for another turn.
    delivered = []
    pending_items = delivery.get("items", []) if delivery is not None else []
    for item in pending_items:
        # Management fields stay in the local delivery snapshot. Only content
        # grouped by scope belongs to the provider's context and its budget.
        selected = [*delivered, item]
        shared_text = {
            scope: [entry["content"] for entry in selected if entry["scope"] == scope]
            for scope in ("day", "persistent")
            if any(entry["scope"] == scope for entry in selected)
        }
        proposed = {**data, "shared": shared_text}
        proposed_message = {"role": "user", "content": APPLICATION_DATA_PREFIX + json.dumps(proposed, ensure_ascii=False)}
        if estimate_tokens([system, proposed_message, current]) > budget:
            break
        delivered.append(item)
        data = proposed
        application_data = proposed_message

    successful = [turn for turn in turns if turn.get("status", "succeeded") == "succeeded"]
    recent = successful[-window:] if window else []
    current_reference_id = reference["id"] if reference else None
    # Precompute pairs to ensure budgeting only removes complete original turns.
    pairs = [(turn["id"], _turn_messages(turn, current_reference_id)) for turn in recent]
    dropped = []
    messages = [system, application_data] + [message for _, pair in pairs for message in pair] + [current]
    estimated = estimate_tokens(messages)
    while estimated > budget and pairs:
        dropped.append(pairs.pop(0)[0])
        messages = [system, application_data] + [message for _, pair in pairs for message in pair] + [current]
        estimated = estimate_tokens(messages)
    return {
        "messages": messages,
        "estimated_input_tokens": estimated,
        "dropped_turn_ids": dropped,
        "included_turn_ids": [turn_id for turn_id, _ in pairs],
        "budget": budget,
        "estimator": ESTIMATOR_LABEL,
        **metadata(system["content"], conversation.get("prompt_variant", BASELINE_VERSION)),
        "scene": conversation["scene"], "explain": bool(conversation.get("explain")),
        "branch": bool(conversation.get("parent_id")), "feedback_enabled": bool(conversation.get("feedback_enabled", False)),
        **({"shared_delivery": {**delivery, "items": delivered} if delivery is not None else None,
            "shared_deferred_count": len(pending_items) - len(delivered)} if incremental else {"shared_context": shared}),
        "shared_enabled": shared_enabled,
        "context_fields": [field for field in ("shared", "conditions", "reference", "carry_note") if field in data],
    }
