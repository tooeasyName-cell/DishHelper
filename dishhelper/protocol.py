"""Strictly validate the only fields a model may write.

This checks structure, never the truth of a model's statements. Invalid output is
reported without partially applying state; the service controls bounded retries.
"""

import json

REFERENCE_KINDS = frozenset(
    {"candidate_set", "selected_plan", "recipe", "shopping_reference"}
)


class ProtocolError(ValueError):
    """The response cannot be atomically applied under the output contract."""


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ProtocolError(f"JSON 包含重复字段：{key}")
        result[key] = value
    return result


def _reject_constant(value):
    raise ProtocolError(f"JSON 不允许非标准数值：{value}")


def _keys(value, expected, label):
    if not isinstance(value, dict):
        raise ProtocolError(f"{label} 必须是对象")
    if set(value) != set(expected):
        raise ProtocolError(f"{label} 只能且必须包含字段：{', '.join(expected)}")


def _string(value, label, nonempty=False):
    if not isinstance(value, str):
        raise ProtocolError(f"{label} 必须是字符串")
    if nonempty and not value.strip():
        raise ProtocolError(f"{label} 不能为空")
    # Unpaired Unicode surrogates cannot be sent or stored as UTF-8 text.
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ProtocolError(f"{label} 包含无效 Unicode 字符") from exc
    return value


def validate_response(text: str) -> dict:
    """Parse one exact JSON response without coercion or undocumented repairs."""
    if not isinstance(text, str):
        raise ProtocolError("模型响应必须是 JSON 文本")
    try:
        output = json.loads(
            text, object_pairs_hook=_unique_object, parse_constant=_reject_constant
        )
    except (json.JSONDecodeError, RecursionError) as exc:
        raise ProtocolError("模型输出不是有效的单个 JSON 对象；未应用任何状态") from exc
    _keys(output, ("answer", "state", "reference_update"), "响应")
    answer = _string(output["answer"], "answer", nonempty=True)
    state = output["state"]
    _keys(state, ("conditions", "carry_note"), "state")
    conditions = _string(state["conditions"], "state.conditions")
    carry_note = _string(state["carry_note"], "state.carry_note")
    reference_update = output["reference_update"]
    normalized_update = None
    if reference_update is not None:
        if not isinstance(reference_update, dict):
            raise ProtocolError("reference_update 必须为 null 或对象")
        action = reference_update.get("action")
        if action == "clear":
            _keys(reference_update, ("action",), "reference_update.clear")
            normalized_update = {"action": "clear"}
        elif action == "replace":
            _keys(reference_update, ("action", "kind", "content"), "reference_update.replace")
            kind = _string(reference_update["kind"], "reference_update.kind")
            if kind not in REFERENCE_KINDS:
                raise ProtocolError("reference_update.kind 不是允许的参考类型")
            content = _string(reference_update["content"], "reference_update.content", nonempty=True)
            normalized_update = {"action": "replace", "kind": kind, "content": content}
        else:
            raise ProtocolError("reference_update.action 只允许 replace 或 clear；保留参考请返回 null")
    return {
        "answer": answer,
        "state": {"conditions": conditions, "carry_note": carry_note},
        "reference_update": normalized_update,
    }


def parse_response(text: str) -> dict:
    """Separate optional observations from the strictly validated business result.

    Invalid optional feedback never discards an otherwise valid paid answer.
    Malformed JSON, duplicate keys and invalid business fields remain fatal.
    """
    if not isinstance(text, str):
        raise ProtocolError("模型响应必须是 JSON 文本")
    try:
        raw = json.loads(text, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise ProtocolError("模型输出不是有效的单个 JSON 对象；未应用任何状态") from exc
    if not isinstance(raw, dict) or set(raw) - {"answer", "state", "reference_update", "feedback", "shared_update", "context_feedback", "shared_additions", "app_feedback", "context_display", "search_proposal", "problem_count", "persistent_impact", "long_reference"}:
        raise ProtocolError("响应只允许核心字段与声明的共享、反馈扩展。")
    # Legacy problem_count is accepted but deliberately ignored, regardless of value.
    core = {key: value for key, value in raw.items() if key in ("answer", "state", "reference_update")}
    output = validate_response(json.dumps(core, ensure_ascii=False))
    result = {"output": output, "feedback": [], "feedback_status": "absent", "feedback_error": ""}
    for key, normalizer in (("context_display", normalize_context_display),
                            ("search_proposal", normalize_search_proposal),
                            ("persistent_impact", normalize_persistent_impact),
                            ("long_reference", normalize_long_reference)):
        if key in raw:
            try:
                result[key] = normalizer(raw[key])
            except (ValueError, TypeError):
                result.setdefault("extension_errors", {})[key] = f"{key} 格式无效，已忽略；回答与状态仍独立保存。"
    if "shared_additions" in raw:
        result["shared_additions"] = normalize_shared_additions([] if raw["shared_additions"] is None else raw["shared_additions"])
    if "app_feedback" in raw:
        result.update(app_feedback=[], app_feedback_status="empty", app_feedback_error="")
        if raw["app_feedback"] is not None and raw["app_feedback"] != []:
            try:
                result["app_feedback"] = normalize_app_feedback(raw["app_feedback"])
                result["app_feedback_status"] = "reported"
            except (ValueError, TypeError):
                result.update(app_feedback_status="invalid", app_feedback_error="流程改进反馈格式无效，已忽略；回答与状态独立处理。")
    if "shared_update" in raw:
        value = raw["shared_update"]
        result["shared_update"] = None if value is None else _string(value, "shared_update")
    if "context_feedback" in raw:
        result.update(context_feedback=[], context_feedback_status="empty", context_feedback_error="")
        value = raw["context_feedback"]
        if value is not None and value != []:
            try:
                result["context_feedback"] = normalize_context_feedback(value)
                result["context_feedback_status"] = "reported"
            except (ValueError, TypeError):
                result.update(context_feedback_status="invalid", context_feedback_error="信息使用反馈格式不合规，已忽略；回答与状态独立校验。")
    if "feedback" not in raw:
        return result
    value = raw["feedback"]
    if value is None or value == []:
        result["feedback_status"] = "empty"
        return result
    try:
        if not isinstance(value, list) or len(value) > 3:
            raise ValueError("feedback 须为至多3项的数组或 null")
        normalized = []
        for item in value:
            if not isinstance(item, dict) or not {"field", "issue"} <= set(item) or set(item) - {"field", "issue", "suggestion"}:
                raise ValueError("反馈项字段无效")
            if item["field"] not in ("conditions", "reference", "carry_note", "answer"):
                raise ValueError("反馈目标字段无效")
            issue = _string(item["issue"], "feedback.issue", nonempty=True)
            suggestion = _string(item.get("suggestion", ""), "feedback.suggestion")
            if len(issue) > 300 or len(suggestion) > 300:
                raise ValueError("反馈文本超过300字")
            normalized.append({"field": item["field"], "issue": issue, "suggestion": suggestion})
        result.update(feedback=normalized, feedback_status="reported")
    except (ValueError, TypeError):
        result.update(feedback_status="invalid", feedback_error="可选反馈格式不合规，已忽略反馈，核心回答独立校验。")
    return result


def normalize_context_feedback(value):
    """Validate optional observations without giving them any state authority."""
    if not isinstance(value, list) or len(value) > 4:
        raise ValueError("context_feedback 须为至多4项的数组")
    normalized, seen = [], set()
    for item in value:
        if not isinstance(item, dict) or not {"field", "value"} <= set(item) or set(item) - {"field", "value", "note"}:
            raise ValueError("信息使用反馈项字段无效")
        field, rating = item["field"], item["value"]
        if not isinstance(field, str) or field not in ("shared", "conditions", "reference", "carry_note") or field in seen:
            raise ValueError("信息使用反馈目标无效或重复")
        if rating not in ("helpful", "unused", "uncertain"):
            raise ValueError("信息使用反馈评价无效")
        note = _string(item.get("note", ""), "context_feedback.note")
        if len(note) > 200:
            raise ValueError("信息使用反馈说明超过200字")
        seen.add(field)
        normalized.append({"field": field, "value": rating, "note": note})
    return normalized


def normalize_shared_additions(value):
    """Check the append payload, without classifying its semantic usefulness."""
    if not isinstance(value, list) or len(value) > 5:
        raise ProtocolError("shared_additions 必须是至多5项的数组")
    result = []
    for item in value:
        _keys(item, ("scope", "content"), "shared_additions 项")
        if item["scope"] not in ("day", "persistent"):
            raise ProtocolError("共享范围只允许 day 或 persistent")
        content = _string(item["content"], "shared_additions.content", nonempty=True).strip()
        if len(content) > 600:
            raise ProtocolError("每条共享信息最多600字")
        result.append({"scope": item["scope"], "content": content})
    return result


def normalize_app_feedback(value):
    if not isinstance(value, list) or len(value) > 3:
        raise ValueError("app_feedback 必须是至多3项的数组")
    result = []
    for item in value:
        _keys(item, ("area", "observation", "suggestion"), "app_feedback 项")
        if item["area"] not in ("context", "prompt", "ui", "workflow"):
            raise ValueError("未知的流程改进领域")
        observation = _string(item["observation"], "app_feedback.observation", nonempty=True)
        suggestion = _string(item["suggestion"], "app_feedback.suggestion", nonempty=True)
        if len(observation) > 300 or len(suggestion) > 300:
            raise ValueError("改进反馈每段最多300字")
        result.append({"area": item["area"], "observation": observation, "suggestion": suggestion})
    return result


def normalize_context_display(value):
    """Optional display selection, without UI code or memory authority."""
    if not isinstance(value, list) or len(value) > 12:
        raise ValueError("展示选择必须是至多12项的列表")
    result = []
    for item in value:
        if not isinstance(item, dict) or "label" not in item or set(item) - {"label", "content"}:
            raise ValueError("展示项仅含 label 和可选 content")
        label = _string(item["label"], "展示标签", nonempty=True).strip()
        if len(label) > 80:
            raise ValueError("展示标签过长")
        normalized = {"label": label}
        if "content" in item:
            content = _string(item["content"], "展示正文")
            if len(content) > 20000:
                raise ValueError("展示正文过长")
            normalized["content"] = content
        elif label not in ("C", "R", "U"):
            raise ValueError("自定义标签需要正文")
        result.append(normalized)
    return result


def normalize_search_proposal(value):
    if not isinstance(value, dict) or "query" not in value or set(value) - {"query", "sort"}:
        raise ValueError("搜索提议格式无效")
    query = _string(value["query"], "查询词", nonempty=True).strip()
    if len(query) > 300:
        raise ValueError("查询词过长")
    order = value.get("sort", "relevance")
    if order not in ("relevance", "views", "reads", "likes"):
        raise ValueError("搜索排序无效")
    return {"query": query, "sort": order}


def normalize_persistent_impact(value):
    if not isinstance(value, str) or value not in ("low", "medium", "high"):
        raise ValueError("persistent_impact 必须为 low、medium 或 high")
    return value


def normalize_long_reference(value):
    """A local navigation mark is optional, and never coerced from numbers."""
    if type(value) is not bool:
        raise ValueError("long_reference 必须是布尔值")
    return value
