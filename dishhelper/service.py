"""At most two main-answer attempts, then one core state commit.

A marked reply subsequently gets one isolated presentation call.

The persisted claim precedes the network call. An interrupted pending call is
never automatically resumed: it may already have reached the provider.
"""
from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
from time import perf_counter, sleep
from typing import Callable
from uuid import uuid4

from . import adapters
from .context import build_context, estimate_tokens
from .protocol import ProtocolError, parse_response
from .attempts import AttemptStore
from .feedback import FeedbackStore


def state_of(conversation: dict) -> dict:
    return {key: conversation.get(key) for key in ("conditions", "carry_note", "reference")}


def load_local_env(path: str | Path) -> None:
    """Read optional local .env without expansion, execution, or overriding env.

    Only NAME=value (optionally quoted) lines are supported. Never print values.
    """
    path = Path(path)
    if not path.exists():
        return
    import re
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, sep, value = line.partition("=")
        name, value = name.strip(), value.strip()
        if not sep or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(name, value)


def retry_delay(error):
    """None means stop; a finite delay is the one permitted retry."""
    if isinstance(error, ProtocolError):
        return 0.0
    if not isinstance(error, adapters.AdapterError):
        return None
    if error.category == "invalid_response":
        return 0.0
    if error.category == "transient_server":
        return 1.0
    if error.category == "rate_limited":
        delay = error.retry_after_seconds if error.retry_after_seconds is not None else 2.0
        return delay if 0 <= delay <= 10 else None
    return None


def merge_usage(total, current):
    """Sum known numeric usage without inventing tokens absent from responses."""
    result = deepcopy(total)
    for key, value in current.items():
        if isinstance(value, dict):
            result[key] = merge_usage(result.get(key, {}) if isinstance(result.get(key, {}), dict) else {}, value)
        elif type(value) in (int, float) and type(result.get(key, 0)) in (int, float):
            result[key] = result.get(key, 0) + value
        else:
            result[key] = deepcopy(value)
    return result


def retry_request(original, profile, error):
    request = deepcopy(original)
    if isinstance(error, ProtocolError) or getattr(error, "category", None) == "invalid_response":
        correction = "\n\n[格式校验提示] 上次响应未通过格式校验：" + adapters.redact(str(error), profile)[:200]
        correction += "。请重新回答原问题，只返回符合约定的单个 JSON 对象。"
        messages = deepcopy(request["body"]["messages"])
        messages[-1]["content"] += correction
        # A retry must not silently exceed the user's frozen input budget.
        if estimate_tokens(messages) <= profile["input_budget"]:
            request["body"]["messages"] = messages
    request.setdefault("context_meta", {})["retry_attempt"] = 2
    request["context_meta"]["estimated_input_tokens"] = estimate_tokens(request["body"]["messages"])
    return request


class ChatService:
    def __init__(self, repository, caller: Callable | None = None, *, sleeper: Callable | None = None, layout_caller: Callable | None = None):
        self.repo = repository
        self.caller = caller or adapters.invoke
        self.feedback = FeedbackStore(repository.path)
        self.attempts = AttemptStore(repository)
        self.sleeper = sleeper or sleep
        self.layout_caller = layout_caller

    def preview(self, conversation_id: str, user_input: str) -> dict:
        # Freeze the generation before reading any source data. A cleanup that
        # commits during assembly must not attach old data to the new generation.
        from .persistent_impact import read_impact
        with self.repo._connect() as connection:
            generation = read_impact(connection)["generation"]
        conversation = self.repo.get_conversation(conversation_id)
        conversation["shared_delivery"] = self.repo.get_shared_delivery(conversation_id)
        conversation["feedback_enabled"] = self.feedback.get_settings()["model_feedback_enabled"]
        profile = adapters.validate_profile(self.repo.get_model(conversation["model_id"]))
        turns = self.repo.recent_turns(conversation_id, profile["recent_turns"])
        context = build_context(conversation, turns, user_input, profile)
        request = adapters.build_request(profile, context["messages"])
        # The snapshot IS the request used for invocation, including the selected
        # model and numeric limits. No after-the-fact reconstruction.
        request["context_meta"] = {k: v for k, v in context.items() if k != "messages"}
        request["context_meta"]["persistent_generation"] = generation
        request = json.loads(adapters.redact(json.dumps(request, ensure_ascii=False), profile))
        return {"conversation": conversation, "profile": profile, "request": request}

    def submit(self, conversation_id: str, user_input: str, turn_id: str | None = None) -> dict:
        turn_id = turn_id or str(uuid4())
        previous = self.repo.get_call(turn_id)
        if previous:
            if previous["conversation_id"] != conversation_id:
                raise ValueError("提交 ID 已被另一个会话使用。")
            return {"turn_id": turn_id, "status": previous["status"], "duplicate": True}
        if not user_input.strip():
            raise ValueError("请输入本轮问题。")
        prepared = self.preview(conversation_id, user_input)
        conversation, profile = prepared["conversation"], prepared["profile"]
        safe_input = adapters.redact(user_input, profile)
        claimed = self.repo.claim_turn(
            turn_id, conversation_id, safe_input, prepared["request"],
            state_of(conversation), conversation["revision"],
        )
        if not claimed:
            return {"turn_id": turn_id, "status": self.repo.get_call(turn_id)["status"], "duplicate": True}
        started = perf_counter()
        raw, usage, http_status = "", {}, None
        attempt_number = 0
        request = prepared["request"]
        try:
            for attempt_number in (1, 2):
                # Persist each attempt before network IO. A storage error must
                # not be mistaken for a reason to invoke the provider again.
                self.attempts.begin(turn_id, attempt_number, request)
                attempt_started = perf_counter()
                attempt_raw, attempt_usage, attempt_status = "", {}, None
                try:
                    result = self.caller(profile, request)
                    attempt_raw = adapters.redact(result["raw_response"], profile)
                    attempt_usage = json.loads(adapters.redact(json.dumps(result.get("usage", {})), profile))
                    attempt_status = result.get("http_status")
                    parsed = parse_response(adapters.redact(result["content"], profile))
                except Exception as exc:
                    attempt_raw = adapters.redact(getattr(exc, "raw_response", None) or attempt_raw, profile)
                    attempt_usage = json.loads(adapters.redact(json.dumps(getattr(exc, "usage", None) or attempt_usage), profile))
                    attempt_status = getattr(exc, "http_status", None) or attempt_status
                    delay = retry_delay(exc) if attempt_number == 1 else None
                    self.attempts.finish(turn_id, attempt_number, status="failed", raw_response=attempt_raw,
                        usage=attempt_usage, elapsed_ms=round((perf_counter() - attempt_started) * 1000),
                        http_status=attempt_status, error=adapters.redact(str(exc), profile),
                        error_category="protocol" if isinstance(exc, ProtocolError) else getattr(exc, "category", "unexpected"),
                        retry_delay_seconds=delay)
                    raw, http_status = attempt_raw, attempt_status
                    usage = merge_usage(usage, attempt_usage)
                    if delay is None:
                        raise
                    request = retry_request(prepared["request"], profile, exc)
                    if delay:
                        self.sleeper(delay)
                    continue
                self.attempts.finish(turn_id, attempt_number, status="succeeded", raw_response=attempt_raw,
                    usage=attempt_usage, elapsed_ms=round((perf_counter() - attempt_started) * 1000),
                    http_status=attempt_status)
                raw, http_status = attempt_raw, attempt_status
                usage = merge_usage(usage, attempt_usage)
                break
            saved = self.repo.complete_turn(
                turn_id, parsed["output"], raw, usage,
                round((perf_counter() - started) * 1000), http_status,
                shared_additions=parsed.get("shared_additions", []), extensions=parsed,
            )
            # Observations have no state authority. A local observation-write
            # problem must never relabel an already committed answer as failed.
            feedback_warning = "可选展示或反馈部分格式无效，已忽略对应内容；回答和工作记忆已保存。" if parsed.get("extension_errors") else ""
            try:
                self.feedback.record_model_feedback(turn_id, parsed["feedback"], parsed["feedback_status"], parsed["feedback_error"])
            except Exception:
                feedback_warning = "回答和工作记忆已保存，但本轮反馈记录失败；审核中会显示缺失记录。"
            try:
                self.feedback.record_context_feedback(turn_id, parsed.get("context_feedback", []),
                    parsed.get("context_feedback_status", "absent"), parsed.get("context_feedback_error", ""))
            except Exception:
                feedback_warning = "回答和工作记忆已保存，但本轮反馈记录失败；审核中会显示缺失记录。"
            try:
                self.feedback.record_app_feedback(turn_id, parsed.get("app_feedback", []),
                    parsed.get("app_feedback_status", "absent"), parsed.get("app_feedback_error", ""))
            except Exception:
                feedback_warning = "回答和工作记忆已保存，但本轮反馈记录失败；审核中会显示缺失记录。"
            layout_status, layout_warning = None, ""
            if parsed.get("long_reference") is True:
                # Core state is already committed. Presentation failure never
                # retries or relabels the successful cooking answer.
                try:
                    from .reference_layout import ReferenceLayoutService
                    task = ReferenceLayoutService(self.repo, caller=self.layout_caller).run(turn_id)
                    layout_status = task["status"]
                    if layout_status != "succeeded":
                        layout_warning = "回答已保存，参考排版未完成；已保留原文供阅读。"
                except Exception:
                    layout_status = "failed"
                    layout_warning = "回答已保存，但参考排版未完成；可继续阅读原文。"
            return {"turn_id": turn_id, "status": "succeeded", "duplicate": False, "result": saved,
                    "feedback_status": parsed["feedback_status"], "feedback_warning": feedback_warning, "attempt_count": attempt_number,
                    "layout_status": layout_status, "layout_warning": layout_warning}
        except Exception as exc:
            raw = adapters.redact(getattr(exc, "raw_response", None) or raw, profile)
            http_status = getattr(exc, "http_status", None) or http_status
            error = adapters.redact(str(exc), profile)
            self.repo.fail_turn(turn_id, error, raw, usage, round((perf_counter() - started) * 1000), http_status)
            return {"turn_id": turn_id, "status": "failed", "duplicate": False, "error": error, "attempt_count": attempt_number}
