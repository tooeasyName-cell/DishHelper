"""Optional local feedback UI. Normal chat never waits for a rating."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from uuid import uuid4

import streamlit as st

from .variants import CANDIDATE_VERSION, CUSTOM_VERSION, VARIANT_LABELS
from .comparisons import keep_recent_history
from . import adapters

TAG_LABELS = {"state_wrong": "记忆写错 / 把建议当事实", "lost_condition": "漏记条件",
              "reference_wrong": "参考更新不合适", "too_long": "回答或重复内容太长", "other": "其他"}


def render_review_sidebar(repo, feedback):
    from .review import build_review, render_markdown, write_bundle

    with st.sidebar.expander("本地反馈与审核"):
        current = feedback.get_settings()["model_feedback_enabled"]
        enabled = st.checkbox("允许模型附带信息使用与应用改进反馈", value=current, key="collect_model_feedback",
            help="同一次回答可附带反馈，不增加调用；关闭后仍保留用户反馈与程序日志。")
        if enabled != current:
            feedback.set_settings(model_feedback_enabled=enabled)
        st.caption("反馈只用于本地审核，不进入聊天记忆。未反馈不代表满意或确定。本次不上传，也不自动改生产提示词。")
        days = st.selectbox("审核范围", [7, 30, 0], format_func=lambda value: f"最近 {value} 天" if value else "全部记录")
        examples = st.checkbox("包含问题样例及状态前后值", value=False,
            help="默认仅导出统计与版本信息；勾选后可能包含个人对话。脱敏不等于匿名，分享前应查看。")
        if st.button("生成本地审核包（无模型调用）"):
            since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat() if days else None
            st.session_state["local_review_bundle"] = build_review(repo, feedback, since=since, include_examples=examples)
        bundle = st.session_state.get("local_review_bundle")
        if bundle:
            markdown = render_markdown(bundle)
            with st.expander("查看已生成摘要"):
                st.markdown(markdown)
            st.download_button("下载审核 JSON", json.dumps(bundle, ensure_ascii=False, indent=2),
                               file_name="dishhelper-review.json", mime="application/json")
            st.download_button("下载审核摘要", markdown, file_name="dishhelper-review.md", mime="text/markdown")
            if st.button("保存这份审核包到本地待审核目录"):
                paths = write_bundle(Path(repo.path).parent / "review_outbox", bundle)
                st.success("已保存到数据库旁的 review_outbox，未上传。")
                st.json({key: str(value) for key, value in paths.items()})


def render_turn_feedback(repo, feedback, comparisons, turn, observation=None, context_observation=None, app_observation=None):
    if turn["status"] == "pending":
        return
    tid = turn["id"]
    with st.expander("反馈与版本比较", expanded=False):
        existing = feedback.get_user_feedback(tid)
        if existing:
            rating_label = {"helpful": "有帮助", "problem": "有问题", "neutral": "一般"}[existing["rating"]]
            st.caption(f"本轮已记录：{rating_label}。可以修改，不会重复计票。")
        buttons = st.columns(3)
        for column, label, rating, tags in [
            (buttons[0], "有帮助", "helpful", []),
            (buttons[1], "记忆有问题", "problem", ["state_wrong"]),
            (buttons[2], "一般", "neutral", []),
        ]:
            if column.button(label, key=f"feedback_{rating}_{tid}"):
                feedback.set_user_feedback(tid, rating, tags, existing["comment"] if existing else "")
                st.rerun()
        with st.form(f"feedback_details_{tid}"):
            labels = st.multiselect("可选问题标签", list(TAG_LABELS), default=existing["tags"] if existing else [],
                                   format_func=lambda value: TAG_LABELS[value])
            comment = st.text_area("可选说明", value=existing["comment"] if existing else "", max_chars=1000)
            if st.form_submit_button("保存问题反馈"):
                feedback.set_user_feedback(tid, "problem", labels, comment)
                st.rerun()
        call = repo.get_call(tid)
        with st.expander("本轮写入的工作记忆与模型疑点"):
            st.caption("这些是模型整理的记忆，不是经程序核实的现实状态。")
            st.json({"before": call["before_state"], "after": call.get("after_state")})
            if observation and observation["status"] == "reported":
                st.write("模型自述疑点（仅供审核，不代表可靠置信度）")
                st.json(observation["feedback"])
            elif observation:
                st.caption({"absent": "模型未提供反馈。", "empty": "模型返回空反馈；不能据此认定状态正确。",
                            "invalid": "可选反馈格式不合规，核心回答已独立处理。"}[observation["status"]])
            else:
                st.caption("本轮没有单独的模型反馈记录（可能是旧版本记录或记录缺失）。")
            render_context_feedback(context_observation)
            render_app_feedback(app_observation)

        if turn["status"] != "succeeded":
            return
        st.write("可选：回放这一轮，比较另一个版本")
        st.caption("使用当时保存的输入，额外调用一次。选择偏好只生成审核反馈，当前会话仍沿用已保存的回答和记忆。")
        rows = comparisons.list(source_turn_id=tid)
        pending = any(row["status"] == "pending" for row in rows)
        variant = st.selectbox("候选提示词版本", list(VARIANT_LABELS),
            index=list(VARIANT_LABELS).index(CANDIDATE_VERSION), key=f"comparison_variant_{tid}",
            format_func=lambda value: VARIANT_LABELS[value])
        profiles = {profile["id"]: profile for profile in repo.list_models() if profile["adapter"] != "systemone"}
        chosen_model = st.selectbox("比较使用的模型", ["__original__", *profiles], key=f"comparison_model_{tid}",
            format_func=lambda value: "与原轮相同的保存配置" if value == "__original__" else profiles[value]["name"])
        custom_body_json = None
        preview_error = False
        if variant == CUSTOM_VERSION:
            st.caption("编辑整个请求体：提示词、C/R/U、共享资料、近期问答、当前输入、model 和供应商参数均可修改。连接地址、凭证和超时使用所选模型配置；实际模型名称以 JSON 的 model 为准。")
            draft_key = f"comparison_body_{tid}"
            if draft_key not in st.session_state:
                st.session_state[draft_key] = json.dumps(comparisons.get_editable_body(tid), ensure_ascii=False, indent=2)
            controls = st.columns(3)
            if controls[0].button("恢复原请求", key=f"reset_request_{tid}"):
                st.session_state[draft_key] = json.dumps(comparisons.get_editable_body(tid), ensure_ascii=False, indent=2)
            keep = controls[1].number_input("保留最近问答轮数", min_value=0, max_value=20, value=0,
                                             key=f"keep_history_{tid}")
            if controls[2].button("应用历史轮数", key=f"trim_history_{tid}"):
                try:
                    body = adapters.parse_experiment_body(st.session_state[draft_key])
                    st.session_state[draft_key] = json.dumps(keep_recent_history(body, int(keep)), ensure_ascii=False, indent=2)
                except (ValueError, adapters.AdapterError, AttributeError, KeyError) as exc:
                    st.error(str(exc))
            custom_body_json = st.text_area("候选请求体 JSON", key=draft_key, height=400)
            st.caption("轮数设为 0 可移除近期原文，仍保留系统提示词、C/R/U、共享资料和当前输入；快捷处理不修改应用资料。")
            try:
                prepared = comparisons.preview(tid, variant,
                    None if chosen_model == "__original__" else chosen_model, custom_body_json=custom_body_json)
                with st.expander("检查候选请求与原请求差异"):
                    st.caption(f"输入保守估算：{prepared['request']['context_meta']['estimated_input_tokens']}；未调用模型。")
                    st.code(prepared["metadata"]["body_diff"] or "请求体未改动", language="diff")
                    st.json(prepared["request"]["context_meta"])
            except (ValueError, adapters.AdapterError) as exc:
                preview_error = True
                st.error(str(exc))
        if st.button("生成另一个版本（额外一次调用）", key=f"generate_comparison_{tid}", disabled=pending or preview_error):
            st.session_state["pending_comparison"] = {
                "source_turn_id": tid, "comparison_id": str(uuid4()), "variant": variant,
                "model_id": None if chosen_model == "__original__" else chosen_model,
                "custom_body_json": custom_body_json,
            }
            st.rerun()
        if not rows:
            return
        row_ids = [row["id"] for row in rows]
        chosen_id = st.selectbox("已保存的比较", row_ids, key=f"comparison_view_{tid}",
                                 format_func=lambda value: value[:8])
        compare = comparisons.get(chosen_id)
        if compare is None:
            st.info("这条比较已被删除。")
            return
        with st.expander("完整请求、差异与原始响应"):
            st.write("原版请求快照")
            st.json(compare["baseline"].get("request_snapshot", call["request_snapshot"]))
            st.write("候选请求快照")
            st.json(compare["request_snapshot"])
            st.code(compare["metadata"].get("body_diff", "旧记录未保存差异"), language="diff")
            st.write("候选原始响应（失败时也保留）")
            st.code(compare.get("raw_response", "") or "尚无响应", language="json")
            st.json({"usage": compare.get("usage", {}), "http_status": compare.get("http_status"),
                     "elapsed_ms": compare.get("elapsed_ms"), "status": compare["status"]})
        if compare["status"] == "pending":
            st.info("比较已登记，页面重跑不会再调用。若确认已中断，可标记失败后重新生成；原请求仍可能计费。")
            if st.button("将此比较标记为中断", key=f"comparison_interrupt_{chosen_id}"):
                comparisons.fail_pending(chosen_id)
                st.rerun()
            return
        if compare["status"] == "failed":
            st.error("比较失败，主会话未变：" + compare.get("error", ""))
            return
        baseline = compare["baseline"]
        candidate = compare["candidate_output"]
        new_reference = candidate.get("reference_update")
        results = {
            "baseline": {"answer": baseline["answer"], "reference": baseline.get("reference"), "state": baseline["after_state"]},
            "candidate": {"answer": candidate["answer"], "reference": new_reference if new_reference and new_reference["action"] == "replace" else None,
                          "state": compare["candidate_after_state"]},
        }
        order = compare["display_order"]
        columns = st.columns(2)
        for index, side in enumerate(order):
            with columns[index]:
                st.write("版本 " + ("A" if index == 0 else "B"))
                st.markdown(results[side]["answer"])
                if results[side]["reference"]:
                    st.markdown(results[side]["reference"]["content"])
                with st.expander("查看这版拟写入的工作记忆"):
                    st.json(results[side]["state"])
                    if side == "candidate" and compare.get("candidate_shared_additions"):
                        st.write("拟新增的共享信息（仅展示，未发布）")
                        st.json(compare["candidate_shared_additions"])
                    summary = baseline.get("shared_summary") if side == "baseline" else compare.get("candidate_shared_summary")
                    if summary is not None:
                        st.write("讨论共享摘要")
                        st.write(summary or "（空）")
        note = st.text_input("可选：偏好原因", key=f"preference_note_{chosen_id}", max_chars=1000)
        choices = [("更喜欢 A", order[0]), ("更喜欢 B", order[1]), ("差不多", "tie"), ("都不好", "neither"), ("跳过", "skip")]
        preference_columns = st.columns(5)
        for column, (label, preference) in zip(preference_columns, choices):
            if column.button(label, key=f"prefer_{preference}_{chosen_id}"):
                comparisons.choose(chosen_id, preference, comment=note)
                st.rerun()
        if compare.get("preference"):
            st.caption("偏好已记录，未替换主会话。重复选择会更新本条反馈。")
        with st.expander("版本来源、成本与比较限制"):
            st.caption("显示顺序固定随机；原回答已经看过，比较仍可能存在顺序和样本选择偏差。偏好不代表事实或安全性正确。演示模型仅用于检查流程。")
            st.json({"display_order": order, "metadata": compare["metadata"], "candidate_usage": compare["usage"],
                     "baseline_usage": baseline.get("usage", {}), "elapsed_ms": compare["elapsed_ms"],
                     "feedback_status": compare.get("feedback_status")})
            render_context_feedback({"status": compare.get("context_feedback_status", "missing"),
                                     "feedback": compare.get("context_feedback", [])})
            render_app_feedback({"status": compare.get("app_feedback_status", "missing"),
                                 "feedback": compare.get("app_feedback", [])})


def render_context_feedback(observation):
    st.caption("模型自述的信息使用情况：未用到仅指本轮，不表示这类背景没有价值。")
    if observation and observation["status"] == "reported":
        fields = {"shared": "共享信息", "conditions": "C 条件", "reference": "R 参考", "carry_note": "U 承接信息"}
        values = {"helpful": "有帮助", "unused": "本次未用到", "uncertain": "难判断"}
        for item in observation["feedback"]:
            st.write(f"{fields[item['field']]}：{values[item['value']]}" + (f" — {item['note']}" if item.get("note") else ""))
    else:
        status = observation.get("status") if observation else "missing"
        st.caption({"absent": "本轮未返回信息使用反馈。", "empty": "本轮返回空信息使用反馈。",
                    "invalid": "本轮信息使用反馈格式不合规，未计入评价。"}.get(status, "没有信息使用反馈记录（旧记录或记录缺失）。"))


def render_app_feedback(observation):
    if observation and observation.get("status") == "reported":
        st.write("模型对应用流程的改进建议（待开发者审核）")
        areas = {"context": "上下文", "prompt": "提示词", "ui": "界面", "workflow": "使用流程"}
        for item in observation["feedback"]:
            st.write(f"{areas[item['area']]}：{item['observation']} → {item['suggestion']}")
    elif observation and observation.get("status") == "invalid":
        st.caption("应用建议格式不合规，核心回答已独立处理。")
