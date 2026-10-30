"""Fixed controls for persistent-memory cleanup; model calls require confirmation."""
from uuid import uuid4

import streamlit as st

from .maintenance import MaintenanceService


def render_maintenance(repo, conversation):
    if conversation["parent_id"]:
        return
    cid = conversation["id"]
    service = MaintenanceService(repo)
    pending = service.pending()
    notice = service.notice(cid)
    confirm_key = f"maintenance_confirmation_{cid}"
    with st.expander("持久共享整理", expanded=bool(notice or pending or st.session_state.get(confirm_key))):
        state = service.get_impact()
        level_label = {None: "待评估", "low": "低", "medium": "中", "high": "高"}[state["level"]]
        st.caption(f"当前整理迫切程度：{level_label}（跨会话最高粗估）。确认后才调用模型整理，成功后重新评估。")
        if pending:
            st.warning("有一项持久共享整理尚未结束。若原请求已中断，可标记结束后重新发起；远端请求可能已产生费用。")
            if st.button("已确认整理中断，标记结束", key=f"interrupt_maintenance_{pending['id']}"):
                service.cancel_pending(pending["id"])
                st.rerun()
        if notice:
            if notice["level"] == "high":
                st.warning("持久共享的问题影响粗估为高，建议优先整理；确认后才执行。")
            else:
                st.info("持久共享的问题影响粗估为中，建议整理；确认后才执行。")
            controls = st.columns(2)
            if controls[0].button("整理这些持久信息", key=f"offer_maintenance_{cid}", disabled=pending is not None):
                st.session_state[confirm_key] = {"task_id": str(uuid4()), "signal_key": notice["signal_key"]}
            if controls[1].button("忽略本次整理提示", key=f"dismiss_maintenance_{cid}"):
                service.dismiss_notice(cid, notice["signal_key"])
                st.rerun()
        if st.button("手动整理持久共享", key=f"manual_maintenance_{cid}", disabled=pending is not None):
            st.session_state[confirm_key] = {"task_id": str(uuid4()), "signal_key": notice["signal_key"] if notice else None}
        confirmation = st.session_state.get(confirm_key)
        if confirmation:
            st.warning("将使用本会话模型，对全部有效持久共享进行一次整理调用，自动保存合并、修改或删除结果；不会加载全部聊天历史。")
            controls = st.columns(2)
            if controls[0].button("确认并开始整理", key=f"confirm_maintenance_{cid}", disabled=pending is not None):
                try:
                    with st.spinner("整理持久共享…"):
                        result = service.run(cid, confirmation["task_id"], confirmed=True,
                                             signal_key=confirmation["signal_key"])
                    if result["status"] == "succeeded":
                        st.session_state["flash"] = "持久共享整理已完成；可在整理记录中查看变更或回退。"
                    else:
                        st.session_state["flash"] = "整理未应用：" + (result.get("error") or result["status"])
                except (ValueError, RuntimeError) as exc:
                    st.session_state["flash"] = str(exc)
                finally:
                    st.session_state.pop(confirm_key, None)
                st.rerun()
            if controls[1].button("取消本次整理", key=f"cancel_maintenance_{cid}"):
                st.session_state.pop(confirm_key, None)
                st.rerun()
        tasks = service.list()
        if tasks:
            by_id = {task["id"]: task for task in tasks}
            selected = st.selectbox("整理记录", list(by_id), key="maintenance_history",
                format_func=lambda key: f"{by_id[key]['created_at']} · {by_id[key]['status']} · {key[:8]}")
            task = by_id[selected]
            if task["error"]:
                st.error(task["error"])
            st.json({"status": task["status"], "changes": task["changes"], "usage": task["usage"],
                     "elapsed_ms": task["elapsed_ms"]})
            if task["status"] == "succeeded" and (task["changes"] or task["impact_before"].get("level")):
                if st.button("回退本次整理", key=f"rollback_maintenance_{selected}", disabled=pending is not None):
                    try:
                        service.rollback(selected)
                        st.session_state["flash"] = "已回退这次共享整理；已有会话记忆保持原样。"
                        st.rerun()
                    except (ValueError, RuntimeError) as exc:
                        st.error(str(exc))
            with st.expander("整理请求与原始响应"):
                st.json(task["request"])
                st.code(task["raw_response"] or "（尚无响应）", language="json")
