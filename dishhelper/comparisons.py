"""Optional retrospective comparisons, isolated from the C/R/U commit path.

The baseline is an already committed response. Each candidate consumes at most
one extra call, using its source's frozen request rather than today's state.
Preferences are review evidence only; nothing here can adopt a candidate.
"""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import json
import difflib
from pathlib import Path
import secrets
import sqlite3
from time import perf_counter

from . import adapters
from .context import APPLICATION_DATA_PREFIX, BudgetError, ESTIMATOR_LABEL, estimate_tokens
from .protocol import parse_response
from .variants import (BASELINE_VERSION, CANDIDATE_VERSION, LEGACY_BASELINE_VERSION,
                       PREVIOUS_BASELINE_VERSION, V3_BASELINE_VERSION, V4_BASELINE_VERSION, V5_BASELINE_VERSION, V6_BASELINE_VERSION, V7_BASELINE_VERSION, V8_BASELINE_VERSION, V9_BASELINE_VERSION, V10_BASELINE_VERSION, CUSTOM_VERSION, VARIANT_LABELS, metadata, render_prompt)


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


@contextmanager
def _connect(path, write=False):
    connection = sqlite3.connect(str(path), timeout=10, isolation_level=None)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        if write:
            connection.execute("BEGIN IMMEDIATE")
        yield connection
        if connection.in_transaction:
            connection.commit()
    except BaseException:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()


def initialize_comparisons(db_path):
    """Create only additive evaluation storage after Repository.initialize."""
    with _connect(db_path) as connection:
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS comparisons (
                id TEXT PRIMARY KEY,
                source_turn_id TEXT NOT NULL REFERENCES turns(id) ON DELETE CASCADE,
                conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                status TEXT NOT NULL CHECK(status IN ('pending','succeeded','failed')),
                baseline_json TEXT NOT NULL,
                request_snapshot_json TEXT NOT NULL,
                candidate_output_json TEXT,
                candidate_after_state_json TEXT,
                usage_json TEXT NOT NULL DEFAULT '{}',
                elapsed_ms INTEGER NOT NULL DEFAULT 0,
                http_status INTEGER,
                raw_response TEXT NOT NULL DEFAULT '',
                error TEXT NOT NULL DEFAULT '',
                feedback_json TEXT NOT NULL DEFAULT '[]',
                feedback_status TEXT NOT NULL DEFAULT 'absent',
                feedback_error TEXT NOT NULL DEFAULT '',
                metadata_json TEXT NOT NULL,
                preference TEXT CHECK(preference IN ('baseline','candidate','tie','neither','skip')),
                preference_comment TEXT NOT NULL DEFAULT '',
                display_order_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                completed_at TEXT,
                preference_updated_at TEXT
            );
            CREATE UNIQUE INDEX IF NOT EXISTS one_pending_comparison_per_source
                ON comparisons(source_turn_id) WHERE status='pending';
            CREATE INDEX IF NOT EXISTS comparisons_conversation
                ON comparisons(conversation_id,created_at);
        """)
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(comparisons)")}
        for name, definition in {
            "context_feedback_json": "TEXT NOT NULL DEFAULT '[]'",
            "context_feedback_status": "TEXT NOT NULL DEFAULT 'missing'",
            "context_feedback_error": "TEXT NOT NULL DEFAULT ''",
            "candidate_shared_summary": "TEXT",
            "candidate_shared_additions_json": "TEXT NOT NULL DEFAULT '[]'",
            "app_feedback_json": "TEXT NOT NULL DEFAULT '[]'",
            "app_feedback_status": "TEXT NOT NULL DEFAULT 'missing'",
            "app_feedback_error": "TEXT NOT NULL DEFAULT ''",
        }.items():
            if name not in columns:
                connection.execute(f"ALTER TABLE comparisons ADD COLUMN {name} {definition}")


def body_hash(body):
    return sha256(json.dumps(body, ensure_ascii=False, sort_keys=True, allow_nan=False,
                             separators=(",", ":")).encode("utf-8")).hexdigest()


def body_diff(original, candidate):
    return "\n".join(difflib.unified_diff(
        json.dumps(original, ensure_ascii=False, sort_keys=True, indent=2).splitlines(),
        json.dumps(candidate, ensure_ascii=False, sort_keys=True, indent=2).splitlines(),
        fromfile="original-request.json", tofile="candidate-request.json", lineterm=""))


def edited_context(messages):
    """Describe only fields demonstrably present in the edited application data."""
    candidates = [message["content"][len(APPLICATION_DATA_PREFIX):] for message in messages
                  if message["role"] == "user" and message["content"].startswith(APPLICATION_DATA_PREFIX)]
    data = None
    if len(candidates) == 1:
        try:
            data = adapters.parse_experiment_body(candidates[0])
        except adapters.AdapterError:
            pass
    known = data is not None
    meta = {"context_parse_status": "parsed" if known else "unknown",
            "context_fields": [] if known else None, "shared_enabled": False, "included_turn_ids": [],
            "history_provenance": "edited_messages_no_original_turn_mapping"}
    before = {"conditions": None, "carry_note": None, "reference": None}
    if known:
        for field in ("conditions", "carry_note"):
            before[field] = data.get(field, "")
            if not isinstance(before[field], str):
                before[field] = None
        reference = data.get("reference")
        if reference is None or (isinstance(reference, dict) and isinstance(reference.get("kind"), str)
                                 and isinstance(reference.get("content"), str)):
            before["reference"] = deepcopy(reference)
            meta["reference_known"] = True
        else:
            meta["reference_known"] = False
        meta["context_fields"] = [field for field in ("shared", "conditions", "reference", "carry_note") if data.get(field)]
        meta["shared_enabled"] = bool(data.get("shared")) or data.get("sharing_scope") in ("day_and_persistent", "persistent_only")
        meta["edited_application_data"] = deepcopy(data)
    else:
        meta["reference_known"] = False
    return before, meta


def keep_recent_history(body, count):
    """Trim only the canonical system/data/(user,assistant)* /current layout."""
    if type(count) is not int or count < 0:
        raise ValueError("保留轮数必须是非负整数。")
    messages = body.get("messages") if isinstance(body, dict) else None
    if (not isinstance(messages, list) or len(messages) < 3
        or not all(isinstance(message, dict) for message in messages)
        or messages[0].get("role") != "system"
        or messages[1].get("role") != "user" or not isinstance(messages[1].get("content"), str)
        or not messages[1]["content"].startswith(APPLICATION_DATA_PREFIX)
        or messages[-1].get("role") != "user"):
        raise ValueError("消息结构已改变，无法安全识别历史；请直接编辑 JSON。")
    try:
        adapters.parse_experiment_body(messages[1]["content"][len(APPLICATION_DATA_PREFIX):])
    except adapters.AdapterError:
        raise ValueError("应用资料无法解析，请直接编辑 JSON。") from None
    history = messages[2:-1]
    if len(history) % 2 or any(not isinstance(message, dict) or message.get("role") != ("user" if i % 2 == 0 else "assistant")
                              or not isinstance(message.get("content"), str)
                              or message["content"].startswith(APPLICATION_DATA_PREFIX)
                              for i, message in enumerate(history)):
        raise ValueError("消息结构已改变，无法安全识别历史；请直接编辑 JSON。")
    result = deepcopy(body)
    result["messages"] = messages[:2] + (history[-count * 2:] if count else []) + messages[-1:]
    return result


class ComparisonService:
    def __init__(self, repo, caller=None):
        self.repo = repo
        self.path = Path(repo.path)
        self.caller = caller or adapters.invoke

    def initialize(self):
        initialize_comparisons(self.path)

    def _safe(self, value, profile=None):
        # Include configured keys from every profile, plus the frozen profile in
        # case its credential reference has since changed in the model settings.
        from .feedback import FeedbackStore, scrub

        store = FeedbackStore(self.path)
        with store._connect() as connection:
            profiles = store._profiles(connection)
        profiles.extend(profile if isinstance(profile, list) else [profile or {}])
        # One history read per tree, and one pass per distinct credential ref.
        # Large request snapshots contain many strings, but usually only one key.
        profiles = list({item.get("credential_ref", ""): item for item in profiles}.values())

        def visit(item):
            if isinstance(item, str):
                return scrub(item, profiles)
            if isinstance(item, list):
                return [visit(child) for child in item]
            if isinstance(item, dict):
                return {visit(key): visit(child) for key, child in item.items()}
            return item

        return visit(value)

    @staticmethod
    def _row(row):
        result = dict(row)
        for key in ("baseline", "request_snapshot", "candidate_output", "candidate_after_state",
                    "usage", "feedback", "context_feedback", "metadata", "display_order",
                    "candidate_shared_additions", "app_feedback"):
            raw = result.pop(key + "_json")
            result[key] = json.loads(raw) if raw is not None else None
        return result

    def get(self, comparison_id):
        with _connect(self.path) as connection:
            row = connection.execute("SELECT * FROM comparisons WHERE id=?", (comparison_id,)).fetchone()
        if row is None:
            return None
        result = self._row(row)
        return self._safe(result, result["request_snapshot"].get("profile"))

    def list(self, source_turn_id=None, conversation_id=None):
        clauses, parameters = [], []
        if source_turn_id is not None:
            clauses.append("source_turn_id=?")
            parameters.append(source_turn_id)
        if conversation_id is not None:
            clauses.append("conversation_id=?")
            parameters.append(conversation_id)
        query = "SELECT * FROM comparisons"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        with _connect(self.path) as connection:
            rows = connection.execute(query + " ORDER BY created_at DESC,rowid DESC", parameters).fetchall()
        results = [self._row(row) for row in rows]
        return self._safe(results, [row["request_snapshot"].get("profile", {}) for row in results])

    def _source(self, source_turn_id):
        # Read immutable call and displayed turn together. Current working memory
        # is deliberately absent; conversation scene is only a legacy fallback.
        with self.repo._connect() as connection:
            connection.execute("BEGIN")
            call_row = connection.execute("SELECT * FROM calls WHERE id=?", (source_turn_id,)).fetchone()
            turn_row = connection.execute("SELECT * FROM turns WHERE id=?", (source_turn_id,)).fetchone()
            if call_row is None or turn_row is None:
                raise ValueError("比较来源必须是已经成功保存的一轮回答。")
            if call_row["status"] != "succeeded" or turn_row["status"] != "succeeded":
                raise ValueError("仅支持已成功保存的回答比较；失败或待处理请求不可比较。")
            scene = connection.execute(
                "SELECT scene FROM conversations WHERE id=?", (call_row["conversation_id"],)
            ).fetchone()["scene"]
            call = self.repo._call(call_row)
            turn = self.repo._turn(connection, turn_row)
            shared_audit = connection.execute(
                "SELECT after_summary FROM shared_context_audits WHERE source_turn_id=? "
                "ORDER BY revision DESC LIMIT 1", (source_turn_id,),
            ).fetchone()
        return call, turn, scene, shared_audit["after_summary"] if shared_audit else None

    @staticmethod
    def _settings(source_meta, system, fallback_scene):
        assumptions = []
        scene = source_meta.get("scene")
        if scene not in ("planning", "shopping", "cooking"):
            scene = fallback_scene
            assumptions.append("legacy_scene_from_source_conversation")

        def setting(key, true_phrase, false_phrase):
            value = source_meta.get(key)
            if type(value) is bool:
                return value
            if true_phrase in system:
                assumptions.append("legacy_" + key + "_from_saved_system")
                return True
            if false_phrase in system:
                assumptions.append("legacy_" + key + "_from_saved_system")
                return False
            assumptions.append("unknown_" + key + "_default_false")
            return False

        explain = setting("explain", "【解释模式】默认附上与当前操作有关的简短原理解释",
                          "【解释模式】默认直接给出实用答案")
        branch = setting("branch", "【会话范围】这是独立临时分支", "【会话范围】只维护当前会话提供的状态")
        feedback_enabled = source_meta.get("feedback_enabled")
        if type(feedback_enabled) is not bool:
            feedback_enabled = "【唯一可选协议扩展】" in system
            assumptions.append("legacy_feedback_enabled_from_saved_system")
        return scene, explain, branch, feedback_enabled, assumptions

    def get_editable_body(self, source_turn_id):
        call, _, _, _ = self._source(source_turn_id)
        return self._safe(deepcopy(call["request_snapshot"]["body"]), call["request_snapshot"].get("profile"))

    def preview(self, source_turn_id, variant=CANDIDATE_VERSION, model_id=None, custom_body_json=None):
        if variant not in VARIANT_LABELS:
            raise ValueError("未知比较提示词版本。")
        call, turn, fallback_scene, source_shared_summary = self._source(source_turn_id)
        original = call["request_snapshot"]
        try:
            source_profile = adapters.validate_profile(original["profile"])
            messages = deepcopy(original["body"]["messages"])
            if not messages or messages[0]["role"] != "system":
                raise ValueError("system")
            if sum(message["role"] == "system" for message in messages) != 1:
                raise ValueError("system")
            saved_system = messages[0]["content"]
            if not isinstance(saved_system, str):
                raise ValueError("system")
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("来源未保存完整请求与模型配置，不能可靠地重放比较。") from exc
        source_meta = original.get("context_meta") or {}
        if not isinstance(source_meta, dict):
            raise ValueError("来源请求元数据格式无效。")
        scene, explain, branch, feedback_enabled, assumptions = self._settings(
            source_meta, saved_system, fallback_scene
        )
        shared_enabled = source_meta.get("shared_enabled") is True
        shared_snapshot = source_meta.get("shared_context") if shared_enabled else None
        if source_shared_summary is None and isinstance(shared_snapshot, dict):
            source_shared_summary = shared_snapshot.get("summary")
        profile = source_profile if model_id is None else adapters.validate_profile(self.repo.get_model(model_id))
        replay_baseline = variant in (BASELINE_VERSION, V10_BASELINE_VERSION, V9_BASELINE_VERSION, V8_BASELINE_VERSION, V7_BASELINE_VERSION, V6_BASELINE_VERSION, V5_BASELINE_VERSION, V4_BASELINE_VERSION, V3_BASELINE_VERSION, PREVIOUS_BASELINE_VERSION, LEGACY_BASELINE_VERSION)
        if not replay_baseline and variant != CUSTOM_VERSION:
            messages[0]["content"] = render_prompt(scene, explain, branch, variant, feedback_enabled,
                                                 shared_enabled=shared_enabled)
        messages = self._safe(messages, profile)
        if variant == CUSTOM_VERSION:
            body = adapters.parse_experiment_body(custom_body_json) if custom_body_json is not None else deepcopy(original["body"])
            if self._safe(body, profile) != body:
                raise adapters.AdapterError("请求体中检测到已配置的凭证，请移除后再比较。")
            request = adapters.build_experiment_request(profile, body)
            profile = request["profile"]
            messages = request["body"]["messages"]
        else:
            request = adapters.build_request(profile, messages)
        estimated = estimate_tokens(messages)
        if variant == CUSTOM_VERSION:
            estimated += len(_json({key: value for key, value in request["body"].items() if key != "messages"}).encode("utf-8"))
        if estimated > profile["input_budget"]:
            raise BudgetError(
                f"冻结的比较输入保守估算为 {estimated}，超过预算 {profile['input_budget']}。"
                "本次未调用模型；比较不会裁剪原始资料或近期问答。"
            )
        # build_request uses the original complete profile unless explicitly
        # overridden; changed current model settings cannot affect a replay.
        system_text = "\n".join(message["content"] for message in messages if message["role"] in ("system", "developer"))
        prompt_meta = metadata(system_text, variant)
        if replay_baseline:
            prompt_meta["prompt_version"] = source_meta.get("prompt_version", "unknown")
            prompt_meta["protocol_version"] = source_meta.get("protocol_version", "unknown")
            prompt_meta["app_version"] = source_meta.get("app_version", "unknown")
        comparison_meta = {
            **prompt_meta, "source_turn_id": source_turn_id,
            "source_request_hash": sha256(json.dumps(original, ensure_ascii=False, sort_keys=True,
                                                     separators=(",", ":")).encode("utf-8")).hexdigest(),
            "source_prompt_sha256": sha256(saved_system.encode("utf-8")).hexdigest(),
            "source_prompt_version": source_meta.get("prompt_version", "unknown"),
            "variant": variant, "comparison_mode": "retrospective_evaluation",
            "changed_model": profile != source_profile, "requested_model_id": model_id,
            "extra_calls": 1, "scene": scene, "explain": explain, "branch": branch,
            "feedback_enabled": feedback_enabled, "shared_enabled": shared_enabled,
            "assumptions": assumptions,
            "body_sha256": body_hash(request["body"]),
            "body_diff": body_diff(original["body"], request["body"]),
            "actual_model": request["body"]["model"],
            "demo": profile["adapter"] == "demo", "baseline_demo": source_profile["adapter"] == "demo",
            "estimated_network_requests": 0 if profile["adapter"] == "demo" else 1,
        }
        candidate_before = deepcopy(call["before_state"])
        replay_meta = deepcopy(source_meta)
        if variant == CUSTOM_VERSION:
            candidate_before, edited_meta = edited_context(messages)
            # Never attribute received original fields/history to an arbitrary edit.
            replay_meta = edited_meta
            comparison_meta.update({"comparison_mode": "custom_request", "explain": None,
                                    "branch": None, "feedback_enabled": None,
                                    "shared_enabled": bool(edited_meta.get("shared_enabled")),
                                    "context_parse_status": edited_meta["context_parse_status"]})
        request["context_meta"] = {
            **replay_meta, **comparison_meta, "candidate_before_state": candidate_before,
            "estimated_input_tokens": estimated, "budget": profile["input_budget"], "estimator": ESTIMATOR_LABEL,
        }
        baseline = {
            "answer": turn["answer"], "reference": turn["reference"],
            "before_state": call["before_state"], "after_state": call["after_state"],
            "usage": call["usage"], "elapsed_ms": call["elapsed_ms"],
            "shared_context": deepcopy(shared_snapshot), "shared_summary": source_shared_summary,
            "request_snapshot": deepcopy(original),
        }
        return self._safe({"request": request, "profile": profile, "baseline": baseline,
                           "metadata": comparison_meta, "candidate_before_state": candidate_before,
                           "conversation_id": call["conversation_id"]}, source_profile)

    @staticmethod
    def _check_identity(row, source_turn_id, variant, model_id, custom_body_json=None):
        saved_meta = row["metadata"]
        if row["source_turn_id"] != source_turn_id:
            raise ValueError("比较 ID 已用于另一轮回答，不会重复调用。")
        if saved_meta["variant"] != variant or saved_meta.get("requested_model_id") != model_id:
            raise ValueError("比较 ID 已使用不同的实验配置，不会重复调用。")
        if variant == CUSTOM_VERSION:
            body = (adapters.parse_experiment_body(custom_body_json) if custom_body_json is not None
                    else row["baseline"].get("request_snapshot", {}).get("body"))
            if body_hash(body) != saved_meta.get("body_sha256"):
                raise ValueError("比较 ID 已使用不同的请求体，不会重复调用。")

    @staticmethod
    def _candidate_state(before, output):
        reference = deepcopy(before["reference"])
        update = output["reference_update"]
        if update is not None:
            reference = None if update["action"] == "clear" else {
                "kind": update["kind"], "content": update["content"]
            }
        return {**output["state"], "reference": reference}

    @staticmethod
    def _candidate_shared(prepared, parsed):
        # A candidate describes a possible result for the original snapshot.
        # It never reads or updates the discussion's current shared summary.
        if not prepared["metadata"].get("shared_enabled"):
            return None
        update = parsed.get("shared_update")
        if update is not None:
            return update
        snapshot = prepared["baseline"].get("shared_context")
        return snapshot.get("summary", "") if isinstance(snapshot, dict) else None

    def run(self, source_turn_id, comparison_id, variant=CANDIDATE_VERSION, model_id=None, custom_body_json=None):
        if not isinstance(comparison_id, str) or not comparison_id.strip() or len(comparison_id) > 200:
            raise ValueError("比较 ID 必须是有效的非空文本。")
        previous = self.get(comparison_id)
        if previous is not None:
            self._check_identity(previous, source_turn_id, variant, model_id, custom_body_json)
            return {**previous, "duplicate": True}
        prepared = self.preview(source_turn_id, variant, model_id, custom_body_json)
        profile = prepared["profile"]
        order = ["baseline", "candidate"]
        if secrets.randbelow(2):
            order.reverse()
        with _connect(self.path, write=True) as connection:
            row = connection.execute("SELECT * FROM comparisons WHERE id=?", (comparison_id,)).fetchone()
            if row is not None:
                previous = self._row(row)
                self._check_identity(previous, source_turn_id, variant, model_id, custom_body_json)
                return {**self._safe(previous, profile), "duplicate": True}
            if connection.execute(
                "SELECT 1 FROM comparisons WHERE source_turn_id=? AND status='pending'", (source_turn_id,)
            ).fetchone():
                raise RuntimeError("该回答已有待处理比较，不会并发增加模型调用。")
            connection.execute("""
                INSERT INTO comparisons(
                    id,source_turn_id,conversation_id,status,baseline_json,request_snapshot_json,
                    metadata_json,display_order_json,created_at
                ) VALUES (?,?,?,'pending',?,?,?,?,?)
                """, (comparison_id, source_turn_id, prepared["conversation_id"],
                       _json(prepared["baseline"]), _json(prepared["request"]),
                       _json(prepared["metadata"]), _json(order), _now()))
        started = perf_counter()
        raw, usage, http_status = "", {}, None
        try:
            result = self.caller(profile, prepared["request"])
            raw = self._safe(result["raw_response"], profile)
            if not isinstance(raw, str) or not isinstance(result.get("usage", {}), dict):
                raise ValueError("比较响应与用量格式无效。")
            usage = self._safe(result.get("usage", {}), profile)
            _json(usage)
            http_status = result.get("http_status")
            if http_status is not None and (type(http_status) is not int or not 100 <= http_status <= 599):
                raise ValueError("比较 HTTP 状态格式无效。")
            parsed = parse_response(self._safe(result["content"], profile))
            output = parsed["output"]
            candidate_state = self._candidate_state(prepared["candidate_before_state"], output)
            candidate_shared = self._candidate_shared(prepared, parsed)
            with _connect(self.path, write=True) as connection:
                connection.execute("""
                    UPDATE comparisons SET status='succeeded',candidate_output_json=?,candidate_after_state_json=?,
                        raw_response=?,usage_json=?,elapsed_ms=?,http_status=?,feedback_json=?,feedback_status=?,
                        feedback_error=?,context_feedback_json=?,context_feedback_status=?,
                        context_feedback_error=?,candidate_shared_summary=?,candidate_shared_additions_json=?,
                        app_feedback_json=?,app_feedback_status=?,app_feedback_error=?,completed_at=?
                        WHERE id=? AND status='pending'
                    """, (_json(output), _json(candidate_state), raw, _json(usage),
                           round((perf_counter() - started) * 1000), http_status, _json(parsed["feedback"]),
                           parsed["feedback_status"], parsed["feedback_error"],
                           _json(parsed.get("context_feedback", [])),
                           parsed.get("context_feedback_status", "absent"),
                           parsed.get("context_feedback_error", ""), candidate_shared,
                           _json(parsed.get("shared_additions", [])), _json(parsed.get("app_feedback", [])),
                           parsed.get("app_feedback_status", "absent"), parsed.get("app_feedback_error", ""),
                           _now(), comparison_id))
        except Exception as exc:
            raw = self._safe(getattr(exc, "raw_response", None) or raw, profile)
            raw = raw if isinstance(raw, str) else ""
            status = getattr(exc, "http_status", None) or http_status
            http_status = status if type(status) is int and 100 <= status <= 599 else None
            try:
                _json(usage)
            except (ValueError, TypeError):
                usage = {}
            with _connect(self.path, write=True) as connection:
                connection.execute("""
                    UPDATE comparisons SET status='failed',error=?,raw_response=?,usage_json=?,elapsed_ms=?,
                        http_status=?,completed_at=? WHERE id=? AND status='pending'
                    """, (self._safe(str(exc), profile), raw, _json(usage),
                           round((perf_counter() - started) * 1000), http_status, _now(), comparison_id))
        result = self.get(comparison_id)
        if result is None:
            raise ValueError("比较来源已被删除，结果未保存。")
        return {**result, "duplicate": False}

    def fail_pending(self, comparison_id):
        with _connect(self.path, write=True) as connection:
            row = connection.execute("SELECT status FROM comparisons WHERE id=?", (comparison_id,)).fetchone()
            if row is None:
                raise ValueError("比较不存在。")
            connection.execute(
                "UPDATE comparisons SET status='failed',error=?,completed_at=? WHERE id=? AND status='pending'",
                ("用户结束了待处理比较；不会自动重试，原请求可能已经产生用量。", _now(), comparison_id),
            )
        return self.get(comparison_id)

    def choose(self, comparison_id, preference, comment=""):
        if preference not in ("baseline", "candidate", "tie", "neither", "skip"):
            raise ValueError("请选择原回答、候选、相当、都不好或跳过。")
        if not isinstance(comment, str) or len(comment) > 1000:
            raise ValueError("偏好备注须为不超过1000字的文本。")
        existing = self.get(comparison_id)
        if existing is None:
            raise ValueError("比较不存在。")
        comment = self._safe(comment, existing["request_snapshot"].get("profile"))
        with _connect(self.path, write=True) as connection:
            row = connection.execute("SELECT * FROM comparisons WHERE id=?", (comparison_id,)).fetchone()
            if row is None:
                raise ValueError("比较不存在。")
            if row["status"] != "succeeded" or row["candidate_output_json"] is None:
                raise ValueError("只有两份有效回答均已保存的比较才能记录偏好。")
            connection.execute(
                "UPDATE comparisons SET preference=?,preference_comment=?,preference_updated_at=? WHERE id=?",
                (preference, comment, _now(), comparison_id),
            )
        return self.get(comparison_id)
