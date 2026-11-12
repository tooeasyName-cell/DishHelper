"""Deterministic, local review packages; no model calls, upload or self-update."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
from uuid import uuid4

from .feedback import APP_AREAS, CONTEXT_FIELDS, CONTEXT_VALUES, FeedbackStore, FEEDBACK_STATUSES, FIELDS, RATINGS, TAGS, scrub
from .protocol import normalize_app_feedback
from .cache_usage import reported_cache_token_fields, summarize_cache_usage

STATUSES = ("succeeded", "failed", "pending")
PREFERENCES = ("baseline", "candidate", "tie", "neither", "skip")
EVENTS = ("conditions_cleared", "carry_note_cleared", "reference_replaced", "reference_cleared",
          "selected_plan_publication", "recent_window_dropped")
FAILURES = ("protocol", "network_or_http", "truncated", "credentials", "other")
TOKEN_FIELDS = ("prompt_tokens", "completion_tokens", "total_tokens", "input_tokens", "output_tokens",
                "cached_tokens", "reasoning_tokens", "cache_read_input_tokens", "cache_creation_input_tokens",
                "prompt_cache_hit_tokens", "prompt_cache_miss_tokens", "cache_write_tokens")
PROMPT_VERSIONS = ("baseline-v1", "compact-v1", "baseline-v2", "compact-v2",
                   "baseline-v3", "compact-v3", "baseline-v4", "compact-v4", "baseline-v5", "compact-v5", "baseline-v6", "compact-v6", "baseline-v7", "compact-v7", "baseline-v8", "compact-v8", "baseline-v9", "compact-v9", "baseline-v10", "compact-v10", "baseline-v11", "compact-v11", "scene-detail-v11", "custom-request-v1")
CAVEATS = [
    "没有返回模型反馈不代表模型确定；空反馈也不证明状态正确。",
    "没有用户评分不代表满意；每轮只计当前评分，修改不会重复计数。",
    "清空 C/U、更换 R、发布选定方案和丢弃近期轮次是结构性审核信号，不是已确认的语义错误。",
    "人工修正、用户评价、模型自报分别统计，不能相加解释为独立错误总数。",
    "时间窗口按调用完成时间（待处理按创建时间）、反馈最后修改时间及人工修正时间计算；旧轮次的新反馈仍被纳入。",
    "历史记录缺少的提示词版本、哈希、场景元数据标为 unknown，不根据当前配置反推。",
    "信息使用反馈是模型对本轮输入的自报；unused 只表示本轮未用到，不能据此判定该信息长期无价值。",
    "信息使用统计仅计入请求元数据确认已传入的非空块；未反馈单列，缺少传入记录的历史调用单列为 unknown。",
    "信息使用统计包含窗口内调用及旧调用在窗口内更新的反馈，每个调用只计一次；普通调用和比较调用分别汇总。",
    "比较是用户主动挑选轮次后的回溯评价，存在样本选择、时间和顺序偏差；偏好计数不是科学 A/B 胜率。",
    "比较调用单独计量，不参与主对话状态；缺少供应商用量不等于零成本，未配置价格不能推算金额。",
    "应用改进反馈是模型提出的待审核建议，条目数不代表独立问题数或已确认的收益；普通与比较分别计数，不自动执行建议。",
    "审核包只在本地生成；变更需开发者审查、验证和明确发布，不会自动更新生产提示词。",
    "缓存统计仅依据已报告的用量；调用用量可能已合并多次尝试，无法据此补齐未报告字段。原始字段可能是同义别名，不能彼此相加；命中率仅用分子分母均已报告的同批记录。",
]
CHECKLIST = [
    "先核对调用与反馈覆盖率、失败和中断数量，区分演示及真实外部调用。",
    "结合用户原意审核 C/R/U 的归属、完整替换、清空和参考保留是否合理。",
    "复查人工修正及模型自报，确认问题来自语义、上下文裁剪、接口还是提示词。",
    "按模型、提示词哈希及场景比较，并查看样本量、输入输出用量和额外比较成本。",
    "结合实际交互审核应用改进建议，衡量输入、操作和 token 成本与结果收益，不按建议数量自动选择改动。",
    "对拟议改动建立回归案例，审核有代表性的成对样本，明确接受标准后再发布版本。",
]


def _load(value, default=None):
    try:
        return json.loads(value) if value else default
    except (ValueError, TypeError):
        return default


def _opaque(value):
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:24]


def _timestamp(value):
    if not value:
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result.astimezone(timezone.utc)
    except (ValueError, TypeError, AttributeError):
        return None


def _since(value):
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("起始时间须为 YYYY-MM-DD 或 ISO 时间。")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        try:
            return datetime.fromisoformat(value).replace(tzinfo=timezone(timedelta(hours=8))).astimezone(timezone.utc)
        except ValueError:
            pass
    else:
        parsed = _timestamp(value)
        if parsed is not None:
            return parsed
    raise ValueError("起始时间须为有效的 YYYY-MM-DD 或 ISO 时间。")


def _inside(timestamp, since):
    parsed = _timestamp(timestamp)
    return parsed is not None and (since is None or parsed >= since)


def _counts(values, options):
    counter = Counter(values)
    return {option: counter[option] for option in options}


def _number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _usage(rows):
    totals = Counter()
    reported = 0
    usages = []
    for row in rows:
        usage = _load(row.get("usage_json"), {})
        usages.append(usage)
        if not isinstance(usage, dict):
            continue
        numeric = {key: usage[key] for key in TOKEN_FIELDS if _number(usage.get(key))}
        numeric.update(reported_cache_token_fields(usage))
        if numeric:
            reported += 1
            totals.update(numeric)
    return {"calls_with_reported_tokens": reported, "calls_without_reported_tokens": len(rows) - reported,
            "token_totals": dict(sorted(totals.items())),
            "cache_usage": summarize_cache_usage(usages),
            "elapsed_ms_total": sum(row.get("elapsed_ms", 0) for row in rows if _number(row.get("elapsed_ms", 0)))}


def _identifier(value, profiles):
    if not isinstance(value, str) or not value:
        return "unknown"
    safe = scrub(value, profiles)
    if safe != value or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,99}", safe) or "://" in safe:
        return "opaque-" + _opaque(value)
    return safe


def _metadata(call, profiles):
    request = _load(call.get("request_snapshot_json"), {})
    request = request if isinstance(request, dict) else {}
    meta = request.get("context_meta", {})
    meta = meta if isinstance(meta, dict) else {}
    profile = request.get("profile", {})
    profile = profile if isinstance(profile, dict) else {}
    body = request.get("body", {})
    body = body if isinstance(body, dict) else {}
    prompt_hash = meta.get("prompt_sha256", "unknown")
    if not isinstance(prompt_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", prompt_hash):
        prompt_hash = "unknown"
    version = meta.get("prompt_version", "unknown")
    if version not in PROMPT_VERSIONS:
        version = "unknown"
    scene = meta.get("scene", "unknown")
    if scene not in ("planning", "shopping", "cooking"):
        scene = "unknown"
    return {"model": _identifier(body.get("model", profile.get("model")), profiles),
            "adapter": profile.get("adapter") if profile.get("adapter") in ("demo", "openai_chat") else "unknown",
            "prompt_version": version, "prompt_sha256": prompt_hash, "scene": scene,
            "protocol_version": meta.get("protocol_version") if meta.get("protocol_version") in
                ("core-v1+feedback-v1", "core-v1+shared-v1+feedback-v2", "core-v1+sharing-v2+feedback-v3", "core-v1+sharing-v3+feedback-v3", "core-v1+sharing-v3+feedback-v4+display-v1", "core-v1+sharing-v3+feedback-v4+display-v1+long-reference-v1", "core-v1+sharing-v3+feedback-v4+display-v1+long-reference-v1+persistent-impact-v1") else "unknown",
            "app_version": meta.get("app_version") if meta.get("app_version") in ("0.2.0", "0.3.0", "0.4.0", "0.5.0", "0.6.0", "0.6.1", "0.6.2", "0.7.0", "0.7.1", "0.7.2", "0.7.3", "0.8.0", "0.8.1", "0.9.0", "0.9.1", "0.9.2", "0.9.3") else "unknown"}


def _context_fields(call):
    """Only recorded nonempty request blocks establish the denominator."""
    request = _load(call.get("request_snapshot_json"), {})
    meta = request.get("context_meta") if isinstance(request, dict) else None
    fields = meta.get("context_fields") if isinstance(meta, dict) else None
    if not isinstance(fields, list) or any(field not in CONTEXT_FIELDS for field in fields):
        return None
    return set(fields)


def _context_observations(row, comparison=False):
    status_key = "context_feedback_status" if comparison else "status"
    json_key = "context_feedback_json" if comparison else "feedback_json"
    status = row.get(status_key, "missing")
    if status not in ("missing",) + FEEDBACK_STATUSES:
        status = "invalid"
    items = _load(row.get(json_key), [])
    if status != "reported" or not isinstance(items, list):
        return status, []
    # Fields/values remain an allowlist even when inspecting an older or edited DB.
    clean, seen = [], set()
    for item in items:
        if (isinstance(item, dict) and item.get("field") in CONTEXT_FIELDS
                and item.get("value") in CONTEXT_VALUES and item["field"] not in seen):
            clean.append(item)
            seen.add(item["field"])
    return status, clean


def _context_summary(source, records, profiles, comparison=False):
    def summarize(rows):
        fields = {field: {"provided": 0, **{value: 0 for value in CONTEXT_VALUES}, "not_reported": 0}
                  for field in CONTEXT_FIELDS}
        statuses, unknown, unattributed = [], 0, 0
        for call in rows:
            status, items = _context_observations(call if comparison else records.get(call["id"], {}), comparison)
            statuses.append(status)
            provided = _context_fields(call)
            if provided is None:
                unknown += 1
                unattributed += len(items)
                continue
            values = {item["field"]: item["value"] for item in items}
            unattributed += sum(field not in provided for field in values)
            for field in provided:
                fields[field]["provided"] += 1
                fields[field][values.get(field, "not_reported")] += 1
        return {"call_count": len(rows), "feedback_statuses": _counts(statuses, ("missing",) + FEEDBACK_STATUSES),
                "context_metadata_unknown": unknown, "unattributed_items": unattributed, "fields": fields}

    groups = {}
    for row in source:
        meta = _metadata(row, profiles)
        key = json.dumps(meta, sort_keys=True)
        groups.setdefault(key, {"metadata": meta, "rows": []})["rows"].append(row)
    return {**summarize(source), "groups": [{**group["metadata"], **summarize(group["rows"])}
                                            for _, group in sorted(groups.items())]}


def _app_observations(row, comparison=False):
    status_key = "app_feedback_status" if comparison else "status"
    json_key = "app_feedback_json" if comparison else "feedback_json"
    status = row.get(status_key, "missing")
    if status == "missing":
        return status, []
    if status not in FEEDBACK_STATUSES:
        return "invalid", []
    try:
        items = normalize_app_feedback(_load(row.get(json_key), None))
    except ValueError:
        return "invalid", []
    if (status == "reported" and not items) or (status != "reported" and items):
        return "invalid", []
    return status, items


def _app_summary(source, records, profiles, comparison=False):
    def summarize(rows):
        areas = {area: {"item_count": 0, "call_count": 0} for area in APP_AREAS}
        statuses = []
        for call in rows:
            status, items = _app_observations(call if comparison else records.get(call["id"], {}), comparison)
            statuses.append(status)
            for item in items:
                areas[item["area"]]["item_count"] += 1
            for area in {item["area"] for item in items}:
                areas[area]["call_count"] += 1
        return {"call_count": len(rows), "feedback_statuses": _counts(statuses, ("missing",) + FEEDBACK_STATUSES),
                "suggestion_count": sum(values["item_count"] for values in areas.values()), "areas": areas}

    groups = {}
    for row in source:
        meta = _metadata(row, profiles)
        key = json.dumps(meta, sort_keys=True)
        groups.setdefault(key, {"metadata": meta, "rows": []})["rows"].append(row)
    return {**summarize(source), "groups": [{**group["metadata"], **summarize(group["rows"])}
                                            for _, group in sorted(groups.items())]}


def _failure(row):
    """Classify recognizable application errors; never export their text here."""
    error = str(row.get("error", ""))
    if "截断" in error or "上限截断" in error:
        return "truncated"
    if "凭证" in error or ("环境变量" in error and "未设置" in error) or row.get("http_status") in (401, 403):
        return "credentials"
    if (type(row.get("http_status")) is int and row["http_status"] >= 400
            or "接口请求失败" in error or "接口未返回成功" in error or "HTTP" in error):
        return "network_or_http"
    if any(marker in error for marker in ("JSON", "只能且必须包含字段", "必须是字符串", "必须是对象",
                                          "reference_update", "接口返回格式无效", "无效 Unicode")):
        return "protocol"
    return "other"


def _events(before, after, conversation):
    if not isinstance(before, dict) or not isinstance(after, dict):
        return []
    events = []
    for field in ("conditions", "carry_note"):
        if before.get(field) and not after.get(field):
            events.append(field + "_cleared")
    old, new = before.get("reference"), after.get("reference")
    if old and not new:
        events.append("reference_cleared")
    if new and old != new:
        events.append("reference_replaced")
    if (isinstance(new, dict) and new.get("kind") == "selected_plan"
            and conversation.get("scene") == "planning" and conversation.get("meal_id")
            and not conversation.get("parent_id")):
        events.append("selected_plan_publication")
    return events


def build_review(repo, store, since=None, include_examples=False):
    """Build a read-consistent local report, with a strict counts-only default.

    The output is an explicit allowlist rather than a sanitized database dump.
    Raw responses, requests, endpoints and credential references are never exported.
    """
    if type(include_examples) is not bool:
        raise ValueError("示例开关必须为布尔值。")
    since_dt = _since(since)
    with store._connect() as connection:
        connection.execute("BEGIN")
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        def rows(table):
            return [dict(row) for row in connection.execute("SELECT * FROM " + table)] if table in tables else []
        calls = rows("calls")
        turns = {row["id"]: row for row in rows("turns")}
        conversations = {row["id"]: row for row in rows("conversations")}
        audits = rows("state_audits")
        models, users, comparisons = rows("model_feedback"), rows("user_feedback"), rows("comparisons")
        contexts = rows("context_feedback")
        improvements = rows("app_feedback")
        # Count layout work independently without loading its source or output.
        layouts = [dict(row) for row in connection.execute(
            "SELECT status,usage_json,elapsed_ms,created_at,completed_at FROM reference_layout_tasks"
        )] if "reference_layout_tasks" in tables else []
        profiles = store._profiles(connection)
    by_id = {row["id"]: row for row in calls}
    model_by_id = {row["turn_id"]: row for row in models}
    user_by_id = {row["turn_id"]: row for row in users}
    context_by_id = {row["turn_id"]: row for row in contexts}
    improvement_by_id = {row["turn_id"]: row for row in improvements}
    window_calls = [row for row in calls if _inside(row.get("completed_at") or row["created_at"], since_dt)]
    window_models = [row for row in models if _inside(row.get("updated_at", row["created_at"]), since_dt)]
    window_users = [row for row in users if _inside(row.get("updated_at", row["created_at"]), since_dt)]
    window_contexts = [row for row in contexts if _inside(row.get("updated_at", row["created_at"]), since_dt)]
    window_improvements = [row for row in improvements if _inside(row.get("updated_at", row["created_at"]), since_dt)]
    window_audits = [row for row in audits if _inside(row["created_at"], since_dt)]
    window_comparisons = [row for row in comparisons if _inside(row.get("completed_at") or row["created_at"], since_dt)]
    window_layouts = [row for row in layouts if _inside(row.get("completed_at") or row["created_at"], since_dt)]
    window_choices = [row for row in comparisons if row.get("preference") in PREFERENCES
                      and _inside(row.get("preference_updated_at") or row.get("completed_at") or row["created_at"], since_dt)]

    def coverage(source):
        statuses = [model_by_id.get(row["id"], {}).get("status", "missing") for row in source]
        rating = [user_by_id.get(row["id"], {}).get("rating", "unrated") for row in source]
        return {"model_feedback": _counts(statuses, ("missing",) + FEEDBACK_STATUSES),
                "user_feedback": _counts(rating, ("unrated",) + RATINGS)}

    cohort_map = {}
    for call in window_calls + [by_id[row["turn_id"]] for row in window_users + window_models if row["turn_id"] in by_id]:
        meta = _metadata(call, profiles)
        key = json.dumps(meta, sort_keys=True)
        cohort_map.setdefault(key, {"metadata": meta, "calls": {}, "user_feedback": {}, "model_feedback": {}})
    for call in window_calls:
        cohort_map[json.dumps(_metadata(call, profiles), sort_keys=True)]["calls"][call["id"]] = call
    for source, destination in ((window_users, "user_feedback"), (window_models, "model_feedback")):
        for row in source:
            if row["turn_id"] in by_id:
                cohort_map[json.dumps(_metadata(by_id[row["turn_id"]], profiles), sort_keys=True)][destination][row["turn_id"]] = row
    cohorts = []
    for key, cohort in sorted(cohort_map.items()):
        source = list(cohort["calls"].values())
        cohorts.append({**cohort["metadata"], "call_count": len(source),
                        "statuses": _counts((row["status"] for row in source), STATUSES),
                        "coverage": coverage(source), "usage": _usage(source),
                        "feedback_activity": {
                            "user_ratings": _counts((row["rating"] for row in cohort["user_feedback"].values()), RATINGS),
                            "model_statuses": _counts((row["status"] for row in cohort["model_feedback"].values()), FEEDBACK_STATUSES)}})

    event_counts = Counter()
    manual = [row for row in window_audits if row["source"] == "manual_correction"]
    for audit in window_audits:
        event_counts.update(_events(_load(audit["before_state_json"], {}), _load(audit["after_state_json"], {}),
                                    conversations.get(audit["conversation_id"], {})))
    for call in window_calls:
        request = _load(call["request_snapshot_json"], {})
        meta = request.get("context_meta", {}) if isinstance(request, dict) else {}
        dropped = meta.get("dropped_turn_ids", []) if isinstance(meta, dict) else []
        if isinstance(dropped, list) and dropped:
            event_counts["recent_window_dropped"] += 1

    feedback_fields = Counter()
    for row in window_models:
        for item in _load(row["feedback_json"], []) or []:
            if isinstance(item, dict) and item.get("field") in FIELDS:
                feedback_fields[item["field"]] += 1
    tag_counts = Counter()
    for row in window_users:
        tag_counts.update(tag for tag in set(_load(row["tags_json"], []) or []) if tag in TAGS)

    def comparison_metadata(row):
        meta = _load(row.get("metadata_json"), {}) or {}
        variant = meta.get("variant", meta.get("prompt_version", "unknown"))
        variant = variant if variant in PROMPT_VERSIONS else "unknown"
        original = _metadata(by_id.get(row.get("source_turn_id"), {}), profiles)
        candidate = _metadata(row, profiles)
        source_hash = meta.get("source_prompt_sha256", "unknown")
        if not isinstance(source_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", source_hash):
            source_hash = "unknown"
        return {"candidate_variant": variant, "changed_model": bool(meta.get("changed_model", False)),
                "baseline_model": original["model"], "candidate_model": candidate["model"],
                "baseline_adapter": original["adapter"], "candidate_adapter": candidate["adapter"],
                "source_prompt_sha256": source_hash, "candidate_prompt_sha256": candidate["prompt_sha256"],
                "baseline_prompt_version": original["prompt_version"], "candidate_prompt_version": candidate["prompt_version"],
                "baseline_app_version": original["app_version"], "candidate_app_version": candidate["app_version"],
                "baseline_protocol_version": original["protocol_version"], "candidate_protocol_version": candidate["protocol_version"],
                "scene": candidate["scene"]}

    comparison_groups = {}
    for row in window_comparisons + window_choices:
        meta = comparison_metadata(row)
        key = json.dumps(meta, sort_keys=True)
        comparison_groups.setdefault(key, {"metadata": meta, "calls": {}, "choices": {}})
    for source, destination in ((window_comparisons, "calls"), (window_choices, "choices")):
        for row in source:
            key = json.dumps(comparison_metadata(row), sort_keys=True)
            comparison_groups[key][destination][row["id"]] = row
    groups = []
    for key, group in sorted(comparison_groups.items()):
        source = list(group["calls"].values())
        groups.append({**group["metadata"],
                       "call_count": len(source), "statuses": _counts((row["status"] for row in source), STATUSES),
                       "preferences": _counts((row["preference"] for row in group["choices"].values()), PREFERENCES),
                       "failure_categories": _counts((_failure(row) for row in source if row["status"] == "failed"), FAILURES),
                       "usage": _usage(source)})

    context_calls = {row["id"]: row for row in window_calls}
    context_calls.update({row["turn_id"]: by_id[row["turn_id"]] for row in window_contexts if row["turn_id"] in by_id})
    context_main = _context_summary(list(context_calls.values()), context_by_id, profiles)
    context_main["feedback_updates_in_window"] = len(window_contexts)
    context_main["prior_window_calls_with_feedback_updates"] = len(set(context_calls) - {row["id"] for row in window_calls})
    improvement_calls = {row["id"]: row for row in window_calls}
    improvement_calls.update({row["turn_id"]: by_id[row["turn_id"]] for row in window_improvements if row["turn_id"] in by_id})
    improvement_main = _app_summary(list(improvement_calls.values()), improvement_by_id, profiles)
    improvement_main["feedback_updates_in_window"] = len(window_improvements)
    improvement_main["prior_window_calls_with_feedback_updates"] = len(set(improvement_calls) - {row["id"] for row in window_calls})
    timestamps = [row.get(field) for rows_ in (calls, models, users, contexts, improvements, audits, comparisons, layouts) for row in rows_
                  for field in ("created_at", "completed_at", "updated_at", "preference_updated_at") if _timestamp(row.get(field))]
    bundle = {
        "format_version": 1,
        "window": {"since_utc": since_dt.isoformat() if since_dt else None,
                   "data_through_utc": max((_timestamp(value) for value in timestamps), default=None)},
        "privacy": {"include_examples": include_examples, "example_limit": 30,
                    "raw_provider_data_included": False, "upload_performed": False},
        "main_calls": {"total": len(window_calls), "statuses": _counts((row["status"] for row in window_calls), STATUSES),
                       "adapters": _counts((_metadata(row, profiles)["adapter"] for row in window_calls), ("demo", "openai_chat", "unknown")),
                       "failure_categories": _counts((_failure(row) for row in window_calls if row["status"] == "failed"), FAILURES),
                       "coverage": coverage(window_calls), "usage": _usage(window_calls)},
        "feedback_activity": {
            "model_records": len(window_models),
            "model_statuses": _counts((row["status"] for row in window_models), FEEDBACK_STATUSES),
            "model_issue_fields": {field: feedback_fields[field] for field in FIELDS},
            "user_records": len(window_users), "user_ratings": _counts((row["rating"] for row in window_users), RATINGS),
            "user_tags": {tag: tag_counts[tag] for tag in TAGS},
            "manual_corrections": len({row["id"] for row in manual}),
            "feedback_on_prior_window_calls": len({row["turn_id"] for row in window_users + window_models
                                                   if row["turn_id"] not in {call["id"] for call in window_calls}})},
        "structural_signals": {event: event_counts[event] for event in EVENTS},
        "cohorts": cohorts,
        "context_usefulness": {"main_calls": context_main,
                               "comparisons": _context_summary(window_comparisons, {}, profiles, comparison=True)},
        "app_improvements": {"main_calls": improvement_main,
                             "comparisons": _app_summary(window_comparisons, {}, profiles, comparison=True)},
        "comparisons": {"extra_calls": len(window_comparisons),
                        "statuses": _counts((row["status"] for row in window_comparisons), STATUSES),
                        "adapters": _counts((_metadata(row, profiles)["adapter"] for row in window_comparisons), ("demo", "openai_chat", "unknown")),
                        "failure_categories": _counts((_failure(row) for row in window_comparisons if row["status"] == "failed"), FAILURES),
                        "feedback_statuses": _counts((row.get("feedback_status", "absent") for row in window_comparisons), FEEDBACK_STATUSES),
                        "preferences_updated": len(window_choices),
                        "preferences": _counts((row["preference"] for row in window_choices), PREFERENCES),
                        "unrated_in_call_window": sum(row.get("preference") not in PREFERENCES for row in window_comparisons),
                        "usage": _usage(window_comparisons), "groups": groups},
        "reference_layout": {"task_count": len(window_layouts),
                             "statuses": _counts((row["status"] for row in window_layouts),
                                                 ("succeeded", "failed", "pending", "cancelled")),
                             "usage": _usage(window_layouts)},
        "caveats": CAVEATS.copy(), "developer_review_checklist": CHECKLIST.copy(),
    }
    for section, source in (("main_calls", window_calls), ("comparisons", window_comparisons)):
        bundle[section]["by_adapter"] = {}
        for adapter in ("demo", "openai_chat", "unknown"):
            subset = [row for row in source if _metadata(row, profiles)["adapter"] == adapter]
            bundle[section]["by_adapter"][adapter] = {
                "call_count": len(subset), "statuses": _counts((row["status"] for row in subset), STATUSES),
                "usage": _usage(subset),
                "failure_categories": _counts((_failure(row) for row in subset if row["status"] == "failed"), FAILURES)}
    end = bundle["window"]["data_through_utc"]
    bundle["window"]["data_through_utc"] = end.isoformat() if end else None
    if include_examples:
        def safe_text(value):
            # URL stripping is deliberate even in the explicitly opted-in examples.
            text = scrub(value, profiles)
            for profile in profiles:
                ref = profile.get("credential_ref")
                if isinstance(ref, str) and ref:
                    text = text.replace(ref, "[CREDENTIAL_REF]")
            text = re.sub(r"(?i)\b(?:https?|ftp|file|local)://[^\s<>\"']+", "[URL]", text)
            return text[:1200]
        def safe_state(raw):
            state = raw if isinstance(raw, dict) else _load(raw, {}) or {}
            state = state if isinstance(state, dict) else {}
            ref = state.get("reference")
            return {"conditions": safe_text(state.get("conditions", "")),
                    "carry_note": safe_text(state.get("carry_note", "")),
                    "reference": {"kind": ref.get("kind") if ref.get("kind") in
                                  ("candidate_set", "selected_plan", "recipe", "shopping_reference") else "unknown",
                                  "content": safe_text(ref.get("content", ""))} if isinstance(ref, dict) else None}
        candidates = []
        for row in window_users:
            turn = turns.get(row["turn_id"], {})
            call = by_id.get(row["turn_id"], {})
            candidates.append((row.get("updated_at", row["created_at"]), "user_feedback", row["turn_id"], {
                "rating": row["rating"], "tags": [tag for tag in _load(row["tags_json"], []) if tag in TAGS],
                "comment": safe_text(row["comment"]), "user_input": safe_text(turn.get("user_input", "")),
                "answer": safe_text(turn.get("answer", "")),
                "before_state": safe_state(call.get("before_state_json")),
                "after_state": safe_state(call.get("after_state_json"))}))
        for row in window_models:
            if row["status"] == "reported":
                turn = turns.get(row["turn_id"], {})
                call = by_id.get(row["turn_id"], {})
                items = [{"field": item["field"], "issue": safe_text(item.get("issue", "")),
                          "suggestion": safe_text(item.get("suggestion", ""))}
                         for item in _load(row["feedback_json"], []) if isinstance(item, dict) and item.get("field") in FIELDS]
                candidates.append((row.get("updated_at", row["created_at"]), "model_feedback", row["turn_id"], {
                    "feedback": items, "user_input": safe_text(turn.get("user_input", "")),
                    "answer": safe_text(turn.get("answer", "")),
                    "before_state": safe_state(call.get("before_state_json")),
                    "after_state": safe_state(call.get("after_state_json"))}))
        for source, comparison in ((window_contexts, False), (window_comparisons, True)):
            for row in source:
                status, items = _context_observations(row, comparison)
                if status != "reported":
                    continue
                call = row if comparison else by_id.get(row["turn_id"], {})
                identifier = row["id"] if comparison else row["turn_id"]
                provided = _context_fields(call)
                candidates.append((row.get("updated_at") or row.get("completed_at") or row["created_at"],
                                   "comparison_context_feedback" if comparison else "context_feedback", identifier, {
                    "feedback": [{"field": item["field"], "value": item["value"],
                                  "note": safe_text(item.get("note", ""))} for item in items],
                    "provided_fields": sorted(provided) if provided is not None else None,
                    "before_state": safe_state(call.get("before_state_json")),
                    "metadata": _metadata(call, profiles)}))
        for source, comparison in ((window_improvements, False), (window_comparisons, True)):
            for row in source:
                status, items = _app_observations(row, comparison)
                if status != "reported":
                    continue
                call = row if comparison else by_id.get(row["turn_id"], {})
                identifier = row["id"] if comparison else row["turn_id"]
                candidates.append((row.get("updated_at") or row.get("completed_at") or row["created_at"],
                                   "comparison_app_feedback" if comparison else "app_feedback", identifier, {
                    "feedback": [{"area": item["area"], "observation": safe_text(item["observation"])[:300],
                                  "suggestion": safe_text(item["suggestion"])[:300]} for item in items],
                    "metadata": _metadata(call, profiles)}))
        for call in window_calls:
            if call["status"] == "failed":
                turn = turns.get(call["id"], {})
                candidates.append((call.get("completed_at") or call["created_at"], "failed_call", call["id"], {
                    "category": _failure(call), "error": safe_text(call.get("error", "")),
                    "user_input": safe_text(turn.get("user_input", "")),
                    "before_state": safe_state(call.get("before_state_json"))}))
        for row in manual:
            candidates.append((row["created_at"], "manual_correction", row["id"], {
                "before_state": safe_state(row["before_state_json"]), "after_state": safe_state(row["after_state_json"])}))
        for row in window_choices:
            baseline = _load(row.get("baseline_json"), {}) or {}
            candidate = _load(row.get("candidate_output_json"), {}) or {}
            source_turn = turns.get(row.get("source_turn_id"), {})
            candidates.append((row.get("preference_updated_at") or row["created_at"], "comparison_preference", row["id"], {
                "preference": row["preference"], "comment": safe_text(row.get("preference_comment", "")),
                "user_input": safe_text(source_turn.get("user_input", "")),
                "baseline_answer": safe_text(baseline.get("answer", "")),
                "candidate_answer": safe_text(candidate.get("answer", "")),
                "before_state": safe_state(baseline.get("before_state")),
                "baseline_after_state": safe_state(baseline.get("after_state")),
                "candidate_after_state": safe_state(row.get("candidate_after_state_json")),
                "comparison_metadata": comparison_metadata(row)}))
        bundle["examples"] = [{"timestamp": _timestamp(timestamp).isoformat(), "source": source,
                                "record_id": _opaque(identifier), **value}
                               for timestamp, source, identifier, value in sorted(candidates, key=lambda item: item[:3], reverse=True)[:30]]
        bundle["privacy"]["examples_omitted"] = max(0, len(candidates) - 30)
    canonical = json.dumps(bundle, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    bundle["bundle_id"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:20]
    return bundle


def render_markdown(bundle):
    main, activity, comparisons = bundle["main_calls"], bundle["feedback_activity"], bundle["comparisons"]
    lines = ["# DishHelper 本地审核包", "", f"包 ID：`{bundle['bundle_id']}`", "",
             "数据范围：" + (bundle["window"]["since_utc"] or "全部历史") + " 起；统计按完成/反馈修改时间归属。", "",
             "隐私模式：" + ("含经脱敏的有限示例；分享前需人工复核。" if bundle["privacy"]["include_examples"] else "仅结构化计数，不含对话、状态、评论或报错正文。"), "",
             "## 使用与覆盖", "", f"主调用 {main['total']} 次；成功 {main['statuses']['succeeded']}，失败 {main['statuses']['failed']}，待处理 {main['statuses']['pending']}。", "",
             "模型反馈覆盖：`" + json.dumps(main["coverage"]["model_feedback"], ensure_ascii=False) + "`", "",
             "当前评分覆盖：`" + json.dumps(main["coverage"]["user_feedback"], ensure_ascii=False) + "`", "",
             f"窗口内更新：用户反馈 {activity['user_records']} 条，模型反馈 {activity['model_records']} 条，人工修正 {activity['manual_corrections']} 次。", "",
             "供应商用量：`" + json.dumps(main["usage"], ensure_ascii=False) + "`", "",
             "## 结构性审核信号", "", "```json", json.dumps(bundle["structural_signals"], ensure_ascii=False, indent=2), "```", "",
             "## 模型与提示词分组", "", "| 模型 | 提示词版本 | 场景 | 主调用 | 成功 | 失败 | 待处理 |", "| --- | --- | --- | ---: | ---: | ---: | ---: |"]
    for row in bundle["cohorts"]:
        lines.append(f"| {row['model']} | {row['prompt_version']} | {row['scene']} | {row['call_count']} | {row['statuses']['succeeded']} | {row['statuses']['failed']} | {row['statuses']['pending']} |")
    lines += ["", "完整哈希、分组反馈分母及用量见同包 JSON。", "", "## 回溯比较", "",
              f"额外调用 {comparisons['extra_calls']} 次；本窗口更新偏好 {comparisons['preferences_updated']} 条。", "",
              "偏好（按真实版本身份，不按 A/B 显示位置）：`" + json.dumps(comparisons["preferences"], ensure_ascii=False) + "`", "",
              "额外用量：`" + json.dumps(comparisons["usage"], ensure_ascii=False) + "`", ""]
    if "reference_layout" in bundle:
        layout = bundle["reference_layout"]
        lines += ["## 持续参考排版", "",
                  f"排版任务 {layout['task_count']} 项；状态：`" + json.dumps(layout["statuses"], ensure_ascii=False) + "`。", "",
                  "排版供应商用量（独立统计，不并入主调用）：`" + json.dumps(layout["usage"], ensure_ascii=False) + "`。未返回用量不代表零费用。", ""]
    if "context_usefulness" in bundle:
        lines += ["## 信息使用反馈", "", "unused 表示本轮未用到；未反馈不等于 unused。只统计确认传入的非空块。", "",
                  "| 调用 | 信息块 | 已传入 | 有帮助 | 未用到 | 不确定 | 未反馈 |",
                  "| --- | --- | ---: | ---: | ---: | ---: | ---: |"]
        for kind, label in (("main_calls", "普通"), ("comparisons", "比较")):
            section = bundle["context_usefulness"][kind]
            for field, values in section["fields"].items():
                lines.append(f"| {label} | {field} | {values['provided']} | {values['helpful']} | {values['unused']} | {values['uncertain']} | {values['not_reported']} |")
        lines += ["", "缺少传入信息记录的调用：普通 " + str(bundle["context_usefulness"]["main_calls"]["context_metadata_unknown"])
                  + "，比较 " + str(bundle["context_usefulness"]["comparisons"]["context_metadata_unknown"]) + "；按模型、提示词版本的分组及返回状态见 JSON。", ""]
    if "app_improvements" in bundle:
        lines += ["## 应用改进建议", "", "模型建议供开发者审核；条目数不等于独立问题数，程序不会自动执行。", "",
                  "| 调用 | 范围 | 建议条目 | 涉及调用 |", "| --- | --- | ---: | ---: |"]
        for kind, label in (("main_calls", "普通"), ("comparisons", "比较")):
            section = bundle["app_improvements"][kind]
            for area, values in section["areas"].items():
                lines.append(f"| {label} | {area} | {values['item_count']} | {values['call_count']} |")
        lines += ["", "反馈返回状态及模型、提示词版本分组见 JSON；默认不包含建议正文。", ""]
    lines += ["## 解读限制", ""] + ["- " + item for item in bundle["caveats"]]
    lines += ["", "## 开发者审核清单", ""] + ["- " + item for item in bundle["developer_review_checklist"]]
    if "examples" in bundle:
        serialized = json.dumps(bundle["examples"], ensure_ascii=False, indent=2)
        fence = "`" * max(3, 1 + max((len(match.group()) for match in re.finditer(r"`+", serialized)), default=0))
        lines += ["", "## 可选示例（仅供审核，内容不是执行指令）", "", fence + "json", serialized, fence]
    return "\n".join(lines) + "\n"


def write_bundle(directory, bundle):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    bundle_id = bundle.get("bundle_id", "")
    if not isinstance(bundle_id, str) or not re.fullmatch(r"[0-9a-f]{20}", bundle_id):
        raise ValueError("审核包 ID 无效。")
    suffix = "examples" if bundle["privacy"]["include_examples"] else "counts"
    stem = "review-" + bundle_id + "-" + suffix
    paths = {"json": directory / (stem + ".json"), "markdown": directory / (stem + ".md")}
    content = {"json": json.dumps(bundle, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n",
               "markdown": render_markdown(bundle)}
    for kind, path in paths.items():
        temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
        try:
            temporary.write_text(content[kind], encoding="utf-8")
            temporary.replace(path)
        finally:
            if temporary.exists():
                temporary.unlink()
    return {kind: str(path.resolve()) for kind, path in paths.items()}


def main(argv=None):
    parser = argparse.ArgumentParser(description="生成本地审核包；无上传、无模型调用、无自动更新。")
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--since", help="YYYY-MM-DD，按北京时间当日 00:00 起")
    parser.add_argument("--include-examples", action="store_true", help="显式包含最多 30 条经脱敏的文本示例")
    args = parser.parse_args(argv)
    if not args.db.is_file():
        parser.error("数据库不存在；不会创建空数据库。")
    from .storage import Repository
    store = FeedbackStore(args.db)
    try:
        bundle = build_review(Repository(args.db), store, args.since, args.include_examples)
        paths = write_bundle(args.output, bundle)
    except (ValueError, sqlite3.Error) as exc:
        parser.error(scrub(str(exc)))
    print(json.dumps(paths, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
