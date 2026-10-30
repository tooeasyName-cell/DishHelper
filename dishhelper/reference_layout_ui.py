"""Read-only presentation of a saved reference layout and its original reply."""
import streamlit as st

from .reference_layout import ReferenceLayoutService


def _render_original(turn):
    st.markdown(turn["answer"])
    if turn.get("reference"):
        st.markdown(turn["reference"]["content"])


def _render_layout(turn_id, layout):
    st.subheader(layout["title"])
    if layout.get("overview"):
        st.markdown(layout["overview"])
    sections = layout["sections"]
    if len(sections) == 1:
        st.caption(sections[0]["label"])
        st.markdown(sections[0]["content"])
        return
    # Tab changes stay in the browser. They never trigger another layout call.
    panels = st.tabs([section["label"] for section in sections],
                     key=f"reference_layout_tabs_{turn_id}")
    for panel, section in zip(panels, sections):
        with panel:
            st.markdown(section["content"])


def render_reference_reply(repo, turn, *, formatted_default=True):
    """Render only persisted results; the submission path owns model calls.

    The original is always available. A failed, missing or interrupted layout
    cannot hide a successful answer or change its working state.
    """
    service = ReferenceLayoutService(repo)
    task = service.get(turn["id"])
    if task and task["status"] == "succeeded" and task.get("layout"):
        if formatted_default:
            _render_layout(turn["id"], task["layout"])
            with st.expander("查看原文"):
                _render_original(turn)
        else:
            _render_original(turn)
            with st.expander("查看排版版"):
                _render_layout(turn["id"], task["layout"])
    else:
        if task:
            if task["status"] == "pending":
                st.info("排版尚未完成，先显示已保存的原文。页面刷新不会重复调用。")
                if st.button("确认排版已中断，标记结束", key=f"interrupt_layout_{turn['id']}"):
                    try:
                        service.cancel_pending(turn["id"])
                        st.rerun()
                    except (ValueError, RuntimeError) as exc:
                        st.error(str(exc))
            elif task["status"] == "failed":
                message = "排版未完成，以下保留原文。"
                if task.get("error"):
                    message += " " + task["error"]
                st.warning(message)
            elif task["status"] == "cancelled":
                st.caption("排版已标记结束，以下保留原文。")
        _render_original(turn)
    if task:
        with st.expander("排版请求与记录"):
            st.json({key: task.get(key) for key in ("status", "usage", "elapsed_ms", "http_status", "error")})
            st.json(task.get("request", {}))
            st.code(task.get("raw_response") or "（尚无响应）", language="json")
