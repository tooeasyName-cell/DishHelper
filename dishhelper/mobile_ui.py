"""Desktop-only settings, draft preservation and mobile navigation polling."""
from secrets import token_urlsafe
from uuid import uuid4

import streamlit as st

from .mobile import MobileService
from .mobile_server import ensure_runtime


def stash_active_draft():
    cid = st.session_state.get("active_id")
    if cid and f"draft_{cid}" in st.session_state:
        st.session_state.setdefault("draft_store", {})[cid] = st.session_state[f"draft_{cid}"]


def remember_draft(cid):
    st.session_state.setdefault("draft_store", {})[cid] = st.session_state.get(f"draft_{cid}", "")


def submit_draft(cid):
    text = st.session_state.get(f"draft_{cid}", "")
    if not text.strip() or st.session_state.get("pending_submission"):
        return
    st.session_state["pending_submission"] = {"conversation_id": cid,
        "user_input": text, "turn_id": str(uuid4())}
    st.session_state.setdefault("draft_store", {})[cid] = ""
    st.session_state[f"draft_{cid}"] = ""


def consume_mobile_navigation(repo):
    nav = MobileService(repo).latest_navigation()
    if not nav or nav["id"] <= st.session_state.get("mobile_nav_seen", 0):
        return False
    st.session_state["mobile_nav_seen"] = nav["id"]
    try:
        target = repo.get_conversation(nav["conversation_id"])
    except ValueError:
        return False
    if target["parent_id"]:
        return False
    stash_active_draft()
    st.session_state["nav_target"] = target["id"]
    return True


def observe_desktop(repo, conversation):
    if conversation["parent_id"] or st.session_state.get("mobile_desktop_observed") == conversation["id"]:
        return
    service = MobileService(repo)
    if not service.get_settings()["enabled"]:
        return
    service.observe_desktop(conversation["id"])
    st.session_state["mobile_desktop_observed"] = conversation["id"]


def render_signature(repo):
    cid = st.session_state.get("active_id")
    if not cid:
        return None
    try:
        conversation = repo.get_conversation(cid)
        latest = next(iter(repo.list_calls(cid)), {})
    except ValueError:
        return None
    service = MobileService(repo)
    nav = service.latest_navigation()
    job = service.get_job(nav["job_id"]) if nav else None
    newest = next(iter(service.list_jobs(limit=1)), {})
    return (cid, conversation["revision"], latest.get("id"), latest.get("status"),
            (job or {}).get("status"), newest.get("id"), newest.get("status"))


@st.fragment(run_every="2s")
def poll_mobile(repo):
    if not MobileService(repo).get_settings()["enabled"]:
        return
    if consume_mobile_navigation(repo):
        st.rerun(scope="app")
    signature = render_signature(repo)
    previous = st.session_state.get("mobile_render_signature")
    st.session_state["mobile_render_signature"] = signature
    if previous is not None and previous != signature:
        st.rerun(scope="app")


def arm_mobile_poll(repo):
    # A full run has already rendered the newest view. Only future fragment
    # ticks should trigger refresh; do not cut off the composer mid-render.
    if MobileService(repo).get_settings()["enabled"]:
        st.session_state["mobile_render_signature"] = render_signature(repo)
        poll_mobile(repo)


def render_reroute_note(repo, turn_id):
    note = MobileService(repo).get_turn_note(turn_id)
    if note:
        st.caption(note)


def render_mobile_settings(repo):
    from .mobile_debug_ui import render_mobile_debug
    render_mobile_debug(repo)
    service = MobileService(repo)
    settings = service.get_settings()
    with st.sidebar.expander("手机接入 · AstrBot", expanded=False):
        st.caption("AstrBot 负责平台监听；手机消息由本机 DishHelper 路由和回答。接入默认关闭。")
        token_key = "mobile_connection_token"
        st.session_state.setdefault(token_key, settings.get("token", ""))
        if st.button("生成新的连接密钥", key="mobile_generate_token"):
            st.session_state[token_key] = token_urlsafe(32)
        profiles = {p["id"]: p for p in repo.list_models() if p["adapter"] != "demo"}
        choices = ["", *profiles]
        selected = settings.get("model_id", "")
        if selected not in choices:
            selected = ""
        with st.form("mobile_settings"):
            enabled = st.checkbox("启用手机接入", value=settings["enabled"])
            model_id = st.selectbox("手机决策模型", choices, index=choices.index(selected),
                format_func=lambda value: profiles[value]["name"] if value else "请选择已有真实模型档案")
            origin = st.text_input("绑定的 AstrBot 会话 UMO", value=settings.get("origin", ""),
                help="在 AstrBot 插件中查询并复制私聊来源标识；只处理这一个绑定会话。")
            token = st.text_input("本机连接密钥", type="password", key=token_key)
            port = st.number_input("本机桥接端口", min_value=1024, max_value=65535,
                                   value=int(settings.get("port", 8765)))
            threshold = st.number_input("自动切换场景的评分门槛", min_value=0.0, max_value=1.0,
                value=float(settings.get("switch_threshold", 0.85)), step=0.01,
                help="仅约束普通提问自动转入其他饮食场景。默认0.85，可根据模型试用调整；明确导航指令不受此门槛限制。")
            saved = st.form_submit_button("保存手机接入设置")
        if saved:
            try:
                service.save_settings({"enabled": enabled, "model_id": model_id,
                    "origin": origin.strip(), "token": token.strip(), "port": int(port),
                    "switch_threshold": float(threshold)})
                st.session_state.pop("mobile_desktop_observed", None)
                state = ensure_runtime(repo)
                if state.get("error"):
                    st.error(state["error"])
                else:
                    st.session_state["flash"] = "手机接入设置已保存。"
                    st.rerun()
            except (ValueError, RuntimeError) as exc:
                st.error(str(exc))
        state = ensure_runtime(repo)
        if state.get("running"):
            st.caption(f"本机桥接运行中：127.0.0.1:{state['port']}；每2秒检查手机导航和回答。")
        elif state.get("error"):
            st.error(state["error"])
        else:
            st.caption("本机桥接未启用。")
        jobs = service.list_jobs(limit=20)
        if jobs:
            with st.expander("手机任务记录", expanded=False):
                by_id = {job["id"]: job for job in jobs}
                chosen = st.selectbox("手机任务", list(by_id), key="mobile_job_history",
                    format_func=lambda key: f"{by_id[key].get('created_at', '')} · {by_id[key]['status']} · {key[:8]}")
                job = by_id[chosen]
                st.json({key: job.get(key) for key in ("status", "route", "usage", "error")})
                metadata = job.get("request", {}).get("context_meta", {})
                source = metadata.get("score_source")
                if source:
                    st.caption("路由评分来源：" + {"native": "原生返回", "self_reported": "聊天模型自报估计"}.get(source, str(source)))
                st.json(job.get("request", {}))
                st.code(job.get("raw_response") or "（暂无路由原始响应）", language="json")
                side = job.get("side") or {}
                if side:
                    st.caption("独立杂谈调用记录")
                    st.json(side)
                    if side.get("raw_response"):
                        st.code(side["raw_response"], language="text")
                    for attempt in side.get("attempts", []):
                        if isinstance(attempt, dict) and attempt.get("raw_response"):
                            st.code(attempt["raw_response"], language="text")
                if job["status"] == "queued":
                    if st.button("取消排队手机任务", key=f"mobile_cancel_{chosen}"):
                        try:
                            service.interrupt_job(chosen)
                            st.rerun()
                        except (ValueError, RuntimeError) as exc:
                            st.error(str(exc))
                elif job["status"] == "running" and not state.get("running"):
                    confirmed = st.checkbox("已确认手机任务不再运行", key=f"mobile_interrupt_confirm_{chosen}")
                    if st.button("标记手机任务中断", key=f"mobile_interrupt_{chosen}", disabled=not confirmed):
                        try:
                            service.interrupt_job(chosen, confirmed_stopped=True)
                            st.rerun()
                        except (ValueError, RuntimeError) as exc:
                            st.error(str(exc))

    side_jobs = service.list_side_jobs(limit=20)
    if side_jobs:
        with st.expander("手机独立杂谈", expanded=False):
            st.caption("与饮食会话隔离；这里查看不会切换当前饮食场景。")
            for job in reversed(side_jobs):
                st.caption(f"{job.get('created_at', '')} · {job['status']}")
                st.markdown(job.get("business_text") or job.get("text") or "（无文字）")
                answer = (job.get("side") or {}).get("answer") or job.get("reply")
                if answer:
                    st.markdown(answer)
                elif job.get("error"):
                    st.error(job["error"])
