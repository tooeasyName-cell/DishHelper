"""Read-only mobile routing inspector; opening it never starts model work."""
from collections import Counter
from datetime import date
import json

import streamlit as st

from .mobile import MobileService
from .adapters import decision_payload
from .cache_ui import render_cache_usage
from .mobile_decisions import ROUTER_VERSION, TASK_NOTE, build_decision
from .mobile_server import get_runtime_status

_SCENES = {"planning": "饮食规划", "shopping": "买菜挑菜", "cooking": "备菜煮菜", "other": "独立杂谈"}
_STATUS = {"queued": "排队", "running": "处理中", "succeeded": "已完成", "failed": "失败", "interrupted": "已中断"}
_REASONS = {"below_switch_threshold": "目标评分未达到切换门槛，保留原去向", "unresolved_date": "日期需要澄清",
            "longterm_requires_planning": "长期讨论仅支持规划", "reference_requires_food_scene": "已有饮食参考不能从杂谈直接读取"}


def _json(value):
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return None


def decision_input(request):
    """Read historical combined and new split snapshots without rewriting them."""
    return decision_payload(request)


def decision_output(job):
    """Extract the normalized reply from either provider envelope or old raw JSON."""
    request = job.get("request") or {}
    value = _json(job.get("raw_response") or "")
    if not isinstance(value, dict):
        return None
    try:
        if request.get("adapter") == "systemone":
            for part in (request.get("profile", {}).get("response_root") or "").split("."):
                if part:
                    value = value[part]
        elif isinstance(value.get("choices"), list) and value["choices"]:
            value = _json(value["choices"][0]["message"]["content"])
    except (KeyError, TypeError, IndexError):
        return None
    return value if isinstance(value, dict) else None


def _phase(job):
    if job["status"] != "running":
        return _STATUS.get(job["status"], job["status"])
    if not job.get("request"):
        return "准备决策请求"
    if not job.get("raw_response"):
        return "等待决策模型响应"
    if not job.get("route"):
        return "已收到响应，校验决策结果"
    if job.get("side"):
        return "独立杂谈回答处理中"
    return "决策已返回，业务执行或回答处理中"


def _template(state):
    return build_decision({"current_date": date.today().isoformat(),
        "main": {key: state.get(key) for key in ("scene", "date", "reference_kind")},
        "focus": state.get("focus", "main"), "gap_hours": state.get("gap_hours"),
        "previous_message": "", "message": ""})


def _render_job(job, settings):
    request = job.get("request") or {}
    metadata = request.get("context_meta") or {}
    route = job.get("route") or {}
    details = route.get("decision_meta") or {}
    payload = decision_input(request)
    output = decision_output(job)
    st.subheader("本条手机消息")
    st.caption(f"{job.get('id', '')} · {_phase(job)} · 接收 {job.get('created_at', '')} · 完成 {job.get('completed_at') or '—'}")
    st.code(job.get("text") or "（无输入）", language="text")
    if job.get("business_text") and job["business_text"] != job.get("text"):
        st.caption("本次实际回放的原问题")
        st.code(job["business_text"], language="text")
    if job.get("error"):
        st.error(job["error"])
    source = metadata.get("score_source") or details.get("score_source")
    st.caption("评分来源：" + {"native": "原生返回", "self_reported": "聊天模型自报估计"}.get(source, "旧记录未保存")
               + "；提示词版本：" + str(metadata.get("prompt_version", "旧记录未保存")))
    if details:
        st.write("模型建议 → 实际执行：" + _SCENES.get(details.get("suggested_scene"), str(details.get("suggested_scene", "—")))
                 + " → " + _SCENES.get(details.get("applied_scene"), str(route.get("scene", "—"))))
        score = details.get("selected_probability")
        threshold = details.get("switch_threshold", metadata.get("switch_threshold"))
        st.write("选中项评分：" + (str(score) if score is not None else "评分不可用")
                 + "；当时门槛：" + (str(threshold) if threshold is not None else "旧记录未保存")
                 + "；自动切换：" + ("已拦截" if details.get("switch_blocked") else "未拦截"))
        if details.get("reason"):
            st.info(_REASONS.get(details["reason"], details["reason"]))
    elif job.get("status") == "running":
        st.info("任务处理中。以下内容随已保存的进度更新；这不是模型内部推理进度。")
    st.caption(f"当前设置门槛：{settings.get('switch_threshold', 0.85)}；历史记录以上述当时值为准。")
    st.subheader("决策模型返回（解析后）")
    if output is None:
        st.info("尚无可解析的决策回复，可在下方查看原始响应和错误。")
    else:
        st.json(output, expanded=True)
        rows = []
        answers = output.get("answers")
        for name, answer in (answers.items() if isinstance(answers, dict) else []):
            if not isinstance(answer, dict):
                continue
            values = answer.get("probabilities")
            if not isinstance(values, dict):
                continue
            for option, value in values.items():
                rows.append({"问题": name, "选项": option, "模型评分原值": str(value),
                             "选中": "✓" if option == answer.get("choice") else ""})
        if rows:
            st.table(rows)
    with st.expander("实际执行结果与用量", expanded=True):
        st.json({"route": route, "usage": job.get("usage", {}), "delivery": job.get("delivery")})
        st.caption("原生 confidence 原样展示；程序使用 probabilities 中选中项的有效评分。")
        render_cache_usage([job.get("usage", {})], "决策调用缓存命中")
    with st.expander("决策输入与实际提示词", expanded=False):
        if payload:
            st.caption("这条记录保存的输入与三题定义，不使用当前状态重建。")
            st.json(payload, expanded=True)
        else:
            st.info("旧请求或未完成请求没有统一问题定义；完整请求见下方。")
        for message in (request.get("body") or {}).get("messages", []):
            if message.get("role") == "system":
                st.caption("聊天接口 system 格式说明")
                st.code(message.get("content", ""), language="text")
    with st.expander("完整请求 JSON 与原始响应", expanded=False):
        st.json(request, expanded=True)
        st.code(job.get("raw_response") or "（暂无路由原始响应）", language="json")
    with st.expander("手机业务回复与杂谈调用", expanded=False):
        st.markdown(job.get("reply") or "（业务回复尚未完成）")
        if job.get("side"):
            st.json(job["side"])
            attempts = job["side"].get("attempts") or []
            render_cache_usage([attempt.get("usage", {}) for attempt in attempts] or [job["side"].get("usage", {})], "杂谈调用缓存命中")


@st.fragment(run_every="2s")
def _debug_contents(repo):
    auto = st.checkbox("自动刷新调试状态", value=True, key="mobile_debug_auto")
    refresh = st.button("刷新调试状态", key="mobile_debug_refresh")
    cached = st.session_state.get("mobile_debug_snapshot")
    if auto or refresh or not cached:
        service = MobileService(repo)
        cached = {"settings": service.get_settings(), "state": service.get_state(),
                  "runtime": get_runtime_status(repo), "jobs": service.list_jobs(limit=50)}
        st.session_state["mobile_debug_snapshot"] = cached
    settings, state, runtime, jobs = (cached[key] for key in ("settings", "state", "runtime", "jobs"))
    if runtime.get("error"):
        st.error(runtime["error"])
    elif runtime.get("running"):
        st.success(f"本机桥接运行中 · 127.0.0.1:{runtime.get('port')}")
    elif not settings.get("enabled"):
        st.info("手机接入未启用；仍可查看已保存的决策记录和提示词。")
    else:
        st.warning("本进程没有活动的手机工作线程，请检查侧栏手机接入状态；查看调试不会启动任务。")
    st.write("当前去向：" + ("独立杂谈" if state.get("focus") == "other" else "饮食主会话")
             + "；保留主场景：" + _SCENES.get(state.get("scene"), "—") + "；日期：" + str(state.get("date") or "长期规划"))
    st.caption("当前决策模型档案：" + (settings.get("model_id") or "未选择") + "；当前切换门槛：" + str(settings.get("switch_threshold", 0.85)))
    counts = Counter(job.get("status") for job in jobs)
    st.caption("最近50条：" + " · ".join(f"{label} {counts[key]}" for key, label in _STATUS.items()))
    with st.expander("当前路由状态与提示词模板", expanded=not bool(jobs)):
        st.json(state, expanded=True)
        st.caption(f"当前规则 {ROUTER_VERSION}；下方为空问题预览，不是某条历史请求。")
        st.write(TASK_NOTE)
        try:
            st.json(_template(state), expanded=True)
        except (ValueError, TypeError):
            st.warning("当前状态无法生成规则预览；原始状态和任务记录仍可查看。")
    if not jobs:
        st.info("暂无手机决策记录。收到手机消息后，这里会显示模型输入、返回及执行结果。")
        return
    follow = st.checkbox("跟随最新手机消息", value=True, key="mobile_debug_follow")
    by_id = {job["id"]: job for job in jobs}
    selected_key = "mobile_debug_selected_job"
    if follow or st.session_state.get(selected_key) not in by_id:
        st.session_state[selected_key] = jobs[0]["id"]
    chosen = st.selectbox("查看手机决策记录", list(by_id), key=selected_key, disabled=follow,
        format_func=lambda key: f"{by_id[key].get('created_at', '')} · {_STATUS.get(by_id[key]['status'], by_id[key]['status'])} · {by_id[key].get('text', '')[:28]}")
    _render_job(by_id[chosen], settings)


def _dismiss():
    st.session_state["mobile_debug_open"] = False
    st.session_state.pop("mobile_debug_snapshot", None)


@st.dialog("手机路由调试", width="large", on_dismiss=_dismiss)
def _debug_dialog(repo):
    st.caption("只读查看；自动或手动刷新均不会重新调用决策模型或业务模型，也不会切换场景。")
    if st.button("关闭路由调试", key="mobile_debug_close"):
        _dismiss()
        st.rerun()
    _debug_contents(repo)


def render_mobile_debug(repo):
    if st.sidebar.button("手机路由调试", key="mobile_debug_entry", use_container_width=True):
        st.session_state["mobile_debug_open"] = True
        st.session_state.pop("mobile_debug_snapshot", None)
    if st.session_state.get("mobile_debug_open"):
        _debug_dialog(repo)
