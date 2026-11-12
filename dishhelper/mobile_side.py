"""One isolated miscellaneous history, without dietary memory or sharing."""
from copy import deepcopy
import json
from time import perf_counter, sleep

from . import adapters
from .context import BudgetError, estimate_tokens
from .service import retry_delay, merge_usage

SIDE_VERSION = "mobile-side-v1"
SIDE_PROMPT = "你是用户的日常问答助手，直接回答当前问题。这是与饮食讨论隔离的杂谈，仅依据本次提供的问答；不维护饮食记忆、共享或进度。输入可能来自语音转文字，结合上下文理解明显偏差，不确定且影响回答时简短询问。没有联网或工具，不声称已经查询或执行操作。只输出给用户的回答正文。"


class SideChatService:
    def __init__(self, repo, caller=None, sleeper=None):
        self.repo = repo
        self.caller = caller or adapters.invoke
        self.sleeper = sleeper or sleep

    def _save(self, job_id, data, profile):
        safe = adapters.redact(json.dumps(data, ensure_ascii=False, allow_nan=False), profile)
        with self.repo._connect(write=True) as connection:
            if connection.execute("UPDATE mobile_jobs SET side_json=? WHERE id=? AND status='running'", (safe, job_id)).rowcount != 1:
                raise RuntimeError("手机任务已中断，未继续杂谈调用或保存。")

    def _messages(self, text, profile):
        window = profile["recent_turns"]
        with self.repo._connect() as connection:
            rows = connection.execute("SELECT business_text,side_json FROM mobile_jobs WHERE status='succeeded' AND side_json != '{}' AND superseded_by IS NULL ORDER BY created_at DESC,rowid DESC LIMIT ?", (window,)).fetchall()
        pairs = []
        for row in reversed(rows):
            answer = json.loads(row["side_json"]).get("answer")
            if answer:
                pairs.append([{"role": "user", "content": row["business_text"]}, {"role": "assistant", "content": answer}])
        system = {"role": "system", "content": SIDE_PROMPT}
        current = {"role": "user", "content": text}
        if estimate_tokens([system, current]) > profile["input_budget"]:
            raise BudgetError("杂谈固定输入超过模型预算，未调用模型。")
        while True:
            messages = [system] + [item for pair in pairs for item in pair] + [current]
            if estimate_tokens(messages) <= profile["input_budget"]:
                return messages
            pairs.pop(0)

    def run(self, job_id, text, profile):
        profile = adapters.validate_profile({**profile, "json_mode": False})
        if profile["adapter"] == "systemone":
            raise ValueError("杂谈回答须使用聊天型模型。")
        request = adapters.build_request(profile, self._messages(text, profile))
        request["context_meta"] = {"task": "mobile_side", "prompt_version": SIDE_VERSION}
        data = {"request": request, "attempts": [], "usage": {}, "answer": ""}
        self._save(job_id, data, profile)
        for attempt in (1, 2):
            entry = {"number": attempt, "status": "pending", "request": deepcopy(request)}
            data["attempts"].append(entry)
            self._save(job_id, data, profile)
            started = perf_counter()
            try:
                result = self.caller(profile, request)
                content = result.get("content", "")
                if profile["adapter"] == "demo":
                    content = json.loads(content).get("answer", "本地演示：杂谈正文。")
                if not isinstance(content, str) or not content.strip():
                    raise adapters.AdapterError("杂谈返回空回答。", result.get("raw_response", ""), category="invalid_response", usage=result.get("usage", {}))
                entry.update(status="succeeded", raw_response=result.get("raw_response", ""), usage=result.get("usage", {}))
                data["usage"] = merge_usage(data["usage"], entry["usage"])
                data["answer"] = adapters.redact(content, profile)
                entry["elapsed_ms"] = round((perf_counter() - started) * 1000)
                self._save(job_id, data, profile)
                return data["answer"]
            except Exception as exc:
                entry.update(status="failed", error=adapters.redact(str(exc), profile),
                             raw_response=getattr(exc, "raw_response", "") or "", usage=getattr(exc, "usage", {}) or {},
                             elapsed_ms=round((perf_counter() - started) * 1000))
                data["usage"] = merge_usage(data["usage"], entry["usage"])
                delay = retry_delay(exc) if attempt == 1 else None
                entry["retry_delay_seconds"] = delay
                self._save(job_id, data, profile)
                if delay is None:
                    raise RuntimeError(adapters.redact(str(exc), profile)) from None
                if delay:
                    self.sleeper(delay)
        raise RuntimeError("杂谈回答未完成。")
