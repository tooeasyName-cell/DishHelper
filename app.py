"""Minimal local UI. All model/state behavior lives in dishhelper modules."""
from __future__ import annotations

from datetime import date
import json
import os
from pathlib import Path
from uuid import uuid4

import streamlit as st

from dishhelper.adapters import validate_profile
from dishhelper.context import BudgetError
from dishhelper.attempts import AttemptStore
from dishhelper.prompts import SCENE_LABELS
from dishhelper.service import ChatService, load_local_env, state_of
from dishhelper.runtime_paths import data_directory, local_env_path
from dishhelper.storage import Repository
from dishhelper.feedback import FeedbackStore
from dishhelper.comparisons import ComparisonService
from dishhelper.feedback_ui import render_review_sidebar, render_turn_feedback
from dishhelper.variants import APP_VERSION
from dishhelper.maintenance_ui import render_maintenance
from dishhelper.reference_layout_ui import render_reference_reply
from dishhelper.display_ui import reference_anchor, latest_anchor, navigation_link

ROOT = Path(__file__).resolve().parent
load_local_env(local_env_path())
st.set_page_config(page_title="DishHelperNext", page_icon="🍲", layout="centered", initial_sidebar_state="expanded")
# This sidebar slot precedes navigation and scrolls independently of the chat.
background_slot = st.sidebar.container()


@st.cache_resource
def get_repository(path: str, schema_version: str = APP_VERSION) -> Repository:
    repository = Repository(path)
    defaults_path = ROOT / "default-config.json"
    if not defaults_path.is_file():
        defaults_path = ROOT / "distribution" / "default-config.json"
    # Explicit database overrides are used by isolated tests and custom setups.
    defaults = json.loads(defaults_path.read_text("utf-8")) if "DISHHELPER_DB" not in os.environ and defaults_path.is_file() else None
    repository.initialize(initial_defaults=defaults)
    return repository


repo = get_repository(os.environ.get("DISHHELPER_DB", str(data_directory() / "dishhelper.sqlite3")), APP_VERSION)
service = ChatService(repo)
feedback = FeedbackStore(repo.path)
comparisons = ComparisonService(repo)


def navigate(conversation_id: str) -> None:
    st.session_state["nav_target"] = conversation_id
    st.rerun()


def flash(message: str) -> None:
    st.session_state["flash"] = message



def confirm_usage_reset() -> None:
    """Run before widgets render so no old navigation/draft cache survives."""
    if st.session_state.get("pending_submission") or st.session_state.get("pending_comparison"):
        st.session_state["usage_reset_error"] = "有尚未完成的提交，请先等待或处理该请求，再清空使用数据。"
        return
    try:
        repo.reset_usage_data()
    except (ValueError, RuntimeError) as exc:
        st.session_state["usage_reset_error"] = str(exc)
        return
    st.session_state.clear()
    flash("使用数据已清空，已回到新的空白会话。模型档案、场景默认模型和应用设置已保留。")


def cancel_usage_reset() -> None:
    st.session_state.pop("confirm_usage_reset", None)
    st.session_state.pop("usage_reset_error", None)


def usage_data_settings() -> None:
    with st.sidebar.expander("使用数据管理"):
        st.caption("清除会话、记忆、共享、调用日志、反馈及版本比较；保留模型档案、场景默认模型和应用设置。")
        pending = (repo.has_pending_usage_requests() or bool(st.session_state.get("pending_submission"))
                   or bool(st.session_state.get("pending_comparison")))
        if pending:
            st.info("有未结束的普通请求、版本比较或工具任务。请先等待完成；若已中断，在对应会话、比较或任务中标记中断后再清空。")
        if st.button("清空使用数据", key="open_usage_reset", disabled=pending):
            st.session_state["confirm_usage_reset"] = True
        if st.session_state.get("confirm_usage_reset"):
            st.warning("确认清空全部使用数据？此操作会删除全部会话及分支、C/R/U、问答、共享、日志、反馈、比较和偏好。不会删除 .env、代码、Git 历史或已经导出的文件。")
            controls = st.columns(2)
            controls[0].button("确认清空", key="confirm_usage_reset_button", disabled=pending,
                               on_click=confirm_usage_reset)
            controls[1].button("取消", key="cancel_usage_reset", on_click=cancel_usage_reset)
        if error := st.session_state.get("usage_reset_error"):
            st.error(error)


def model_settings() -> None:
    with st.sidebar.expander("模型初始设置"):
        models = repo.list_models()
        by_id = {m["id"]: m for m in models}
        selection = st.selectbox("编辑模型档案", ["__new__", *by_id],
                                 format_func=lambda value: "新建档案" if value == "__new__" else by_id[value]["name"])
        original = by_id.get(selection, {})
        with st.form(f"model_config_{selection}"):
            name = st.text_input("显示名称", original.get("name", "我的模型"))
            adapter = st.selectbox("协议适配器", ["openai_chat", "demo"],
                                   index=1 if original.get("adapter") == "demo" else 0)
            base_url = st.text_input("Base URL（通常以 /v1 结尾）", original.get("base_url", "https://api.openai.com/v1"))
            model = st.text_input("服务端模型名", original.get("model", ""))
            credential_ref = st.text_input("API Key 环境变量名", original.get("credential_ref", "DISHHELPER_API_KEY"))
            credential = st.text_input("本次运行的 API Key（可留空，且不写入数据库）", type="password")
            input_budget = st.number_input("输入预算（本地保守估算）", 256, 2000000, int(original.get("input_budget", 16000)), step=1024)
            max_output_tokens = st.number_input("最大输出 token（回答和状态共用）", 64, 131072, int(original.get("max_output_tokens", 4000)), step=128)
            recent_turns = st.number_input("近期成功问答轮数 N", 0, 20, int(original.get("recent_turns", 4)),
                help="设为 0 时不发送近期原文，仍发送 C/R/U、未接收的共享信息与本轮输入。也可先用版本实验单次比较。")
            timeout_seconds = st.number_input("请求超时（秒）", 1, 600, int(original.get("timeout_seconds", 60)))
            json_mode = st.checkbox("请求 JSON object 模式（需服务端支持）", value=original.get("json_mode", True))
            token_parameter = st.selectbox("输出上限参数", ["max_completion_tokens", "max_tokens"],
                                           index=1 if original.get("token_parameter") == "max_tokens" else 0)
            save = st.form_submit_button("保存模型配置")
        if save:
            try:
                profile = validate_profile(dict(id=original.get("id", str(uuid4())), name=name,
                    adapter=adapter, base_url=base_url, model=model, credential_ref=credential_ref,
                    input_budget=int(input_budget), max_output_tokens=int(max_output_tokens),
                    recent_turns=int(recent_turns), timeout_seconds=int(timeout_seconds),
                    json_mode=json_mode, token_parameter=token_parameter))
                repo.save_model(profile)
                if credential and credential_ref:
                    os.environ[credential_ref] = credential
                flash("模型配置已保存；填写的 Key 仅对当前进程有效。")
                st.rerun()
            except (ValueError, RuntimeError) as exc:
                st.error(str(exc))
        current_models = repo.list_models()
        model_ids = [m["id"] for m in current_models]
        names = {m["id"]: m["name"] for m in current_models}
        with st.form("scene_defaults"):
            chosen = {}
            for scene, label in SCENE_LABELS.items():
                current = repo.get_scene_model(scene)
                chosen[scene] = st.selectbox(f"{label} · 新会话默认模型", model_ids,
                    index=model_ids.index(current) if current in model_ids else 0,
                    format_func=lambda value: names[value], key=f"default_{scene}")
            if st.form_submit_button("保存场景默认值"):
                for scene, model_id in chosen.items():
                    repo.set_scene_model(scene, model_id)
                flash("新会话将使用所选默认模型。已有会话可以单独切换。")
                st.rerun()


day_meal_ids = {item["meal_id"] for item in repo.list_day_scopes()}
meal_map = {m["id"]: m for m in repo.list_meals() if m["id"] not in day_meal_ids}
target_id = st.session_state.pop("nav_target", None)
if target_id:
    target = repo.get_conversation(target_id)
    target_day = repo.get_day_date(target["meal_id"]) if target["meal_id"] else None
    st.session_state["meal_choice"] = "__date__" if target_day else target["meal_id"] or "__longterm__"
    if target_day:
        st.session_state["discussion_day"] = date.fromisoformat(target_day)
    st.session_state["scene_choice"] = target["scene"]
    st.session_state["active_id"] = target_id
    st.session_state["scope_tuple"] = (target["meal_id"], target["scene"])
    st.session_state.setdefault("last_main_by_scope", {})[(target["meal_id"], target["scene"])] = target["parent_id"] or target_id

st.title("饮食生活助手")
st.caption("工作记忆：C 当前条件 · R 当前参考 · U 后续承接信息 · 最近 N 轮原文")
if notice := st.session_state.pop("flash", None):
    st.info(notice)

scope = st.sidebar.selectbox("讨论范围", ["__date__", "__longterm__", *meal_map], key="meal_choice",
    format_func=lambda value: "按日期" if value == "__date__" else "独立长期规划" if value == "__longterm__" else "历史分组 · " + meal_map[value]["name"])
if scope == "__date__":
    st.session_state.setdefault("discussion_day", date.today())
    discussion_day = st.sidebar.date_input("讨论日期", value=None, key="discussion_day")
    day_group = repo.get_or_create_day(discussion_day.isoformat())
    meal_id = day_group["id"]
else:
    meal_id = None if scope == "__longterm__" else scope
if meal_id is None:
    st.session_state["scene_choice"] = "planning"
scene = st.sidebar.selectbox("场景", list(SCENE_LABELS), key="scene_choice",
                             format_func=lambda value: SCENE_LABELS[value], disabled=meal_id is None)
scope_tuple = (meal_id, scene)
if st.session_state.get("scope_tuple") != scope_tuple or not st.session_state.get("active_id"):
    active = None
    remembered = st.session_state.get("last_main_by_scope", {}).get(scope_tuple)
    if remembered:
        try:
            candidate = repo.get_conversation(remembered)
            if not candidate["archived"] and not candidate["parent_id"]:
                active = candidate
        except ValueError:
            pass
    if active is None:
        active = repo.find_active_conversation(meal_id, scene)
    if active is None:
        active = repo.create_conversation(scene, meal_id=meal_id)
    st.session_state["active_id"] = active["id"]
    st.session_state["scope_tuple"] = scope_tuple
try:
    conversation = repo.get_conversation(st.session_state["active_id"])
except ValueError:
    st.session_state.pop("active_id", None)
    st.rerun()
cid = conversation["id"]
if not conversation["parent_id"]:
    st.session_state.setdefault("last_main_by_scope", {})[scope_tuple] = cid

explain = st.sidebar.toggle("默认展开原理解释", value=conversation["explain"], key=f"explain_{cid}")
models = repo.list_models()
model_ids = [m["id"] for m in models]
models_by_id = {m["id"]: m for m in models}
model_id = st.sidebar.selectbox("本会话模型", model_ids,
    index=model_ids.index(conversation["model_id"]), format_func=lambda value: models_by_id[value]["name"], key=f"model_{cid}")
if explain != conversation["explain"] or model_id != conversation["model_id"]:
    repo.update_settings(cid, explain=explain, model_id=model_id)
    conversation = repo.get_conversation(cid)
if models_by_id[model_id]["adapter"] == "demo":
    st.info("当前为本地演示：只验证保存和上下文流程，不会调用真实模型，也不提供真实饮食建议。")

with st.sidebar.expander("新会话"):
    st.caption("同一天的三个场景共享新增信息；跨天背景可在不同日期的会话间传递。")
    if st.button("在当前场景新建会话"):
        navigate(repo.create_conversation(scene, meal_id=meal_id, explain=explain)["id"])

if not conversation["parent_id"]:
    if st.sidebar.button("进入独立临时分支"):
        navigate(repo.create_conversation(scene, meal_id=meal_id, parent_id=cid)["id"])
else:
    st.warning("临时分支：起始上下文为空，状态仅在分支内保存。")
    if st.sidebar.button("返回主会话"):
        navigate(conversation["parent_id"])
    if st.sidebar.button("保存分支并返回"):
        repo.archive_conversation(cid)
        navigate(conversation["parent_id"])
    if st.sidebar.button("删除此分支并返回"):
        try:
            parent_id = conversation["parent_id"]
            repo.delete_conversation(cid)
            navigate(parent_id)
        except RuntimeError as exc:
            st.error(str(exc))
    with st.expander("可选：将结论带回主会话"):
        conclusion = st.text_area("编辑要带回的结论", key=f"conclusion_{cid}",
                                  help="只填入主会话的下一条消息草稿，发送后才可能更新状态。")
        if st.button("带回为草稿", disabled=not conclusion.strip()):
            st.session_state[f"draft_{conversation['parent_id']}"] = "临时分支结论（请作为待讨论资料）：\n" + conclusion.strip()
            navigate(conversation["parent_id"])

model_settings()
delivery = repo.get_shared_delivery(cid)
if delivery is not None:
    with st.sidebar.expander("共享信息 · 当日 / 跨天"):
        st.caption("新增信息按会话送达一次；成功保存回答后标记已送达。分支和版本实验不消费共享信息。")
        st.write(f"本会话尚未接收：{len(delivery['items'])} 条")
        if delivery["items"]:
            st.json(delivery["items"])
        shared_entries = repo.list_shared_entries(cid, include_inactive=True)
        if shared_entries:
            with st.expander("全部条目与送达情况"):
                st.json(shared_entries)
                entries_by_id = {item["id"]: item for item in shared_entries}
                if next_entry := st.session_state.pop(f"next_shared_entry_{cid}", None):
                    st.session_state[f"shared_entry_{cid}"] = next_entry
                entry_id = st.selectbox("管理共享条目", list(entries_by_id), key=f"shared_entry_{cid}",
                    format_func=lambda value: f"#{value} · {'跨天' if entries_by_id[value]['scope'] == 'persistent' else '当日'}"
                        f"{' · 已退出发送' if not entries_by_id[value]['active'] else ''} · {entries_by_id[value]['content'][:40]}")
                selected_entry = entries_by_id[entry_id]
                if selected_entry["active"]:
                    with st.form(f"edit_shared_{cid}_{entry_id}"):
                        content = st.text_area("共享正文", selected_entry["content"], max_chars=600,
                                               key=f"shared_content_{cid}_{entry_id}")
                        edit = st.form_submit_button("保存共享修改")
                    if edit:
                        try:
                            replacement_id = repo.edit_shared_entry(entry_id, content)
                            st.session_state[f"next_shared_entry_{cid}"] = replacement_id
                            flash("共享信息已更新：旧条目退出发送，新条目将在其范围内重新送达各会话。")
                            st.rerun()
                        except (ValueError, RuntimeError) as exc:
                            st.error(str(exc))
                    if st.button("删除共享条目", key=f"delete_shared_{cid}_{entry_id}"):
                        repo.delete_shared_entry(entry_id)
                        flash("该共享条目已退出后续发送，已有会话记忆未修改。")
                        st.rerun()
                else:
                    st.caption("此条目已退出后续发送，保留在本地记录中供查看。")
                st.caption("编辑相当于删除旧条目并新增一条未消费的消息；删除不会改写已有会话中的 C/R/U。")
render_review_sidebar(repo, feedback)
usage_data_settings()

with st.sidebar.expander("历史会话"):
    histories = repo.list_conversations(meal_id=meal_id, include_archived=True)
    history_map = {h["id"]: h for h in histories}
    if histories:
        hid = st.selectbox("查看 / 恢复", list(history_map), format_func=lambda value:
            f"{history_map[value]['title']} · {SCENE_LABELS[history_map[value]['scene']]}"
            f"{' · 分支' if history_map[value]['parent_id'] else ''}"
            f"{' · 已保存' if history_map[value]['archived'] else ''} · {value[:6]}")
        if st.button("恢复所选会话"):
            repo.archive_conversation(hid, archived=False)
            navigate(hid)
        st.download_button("导出所选完整记录（含对话和调试数据）",
            json.dumps(repo.export_conversation(hid), ensure_ascii=False, indent=2),
            file_name=f"dishhelper-{hid[:8]}.json", mime="application/json")
        confirmed = st.checkbox("确认删除所选会话及其分支", key=f"delete_confirm_{hid}")
        if st.button("删除所选历史", disabled=not confirmed):
            try:
                repo.delete_conversation(hid)
                st.session_state.pop("active_id", None)
                st.rerun()
            except RuntimeError as exc:
                st.error(str(exc))

st.caption(f"{SCENE_LABELS[scene]} · {conversation['title']} · 会话 {cid[:8]}")
reference_marks = repo.get_long_reference_marks(cid)
marked_turn_ids = set(reference_marks["marked_turn_ids"])
with background_slot:
    with st.expander("当前任务背景", expanded=True):
        cards = repo.get_context_display(cid)
        if not cards:
            st.caption("模型选中的整体步骤、条件或注意事项会保留在这里，方便追问时查阅。")
        if reference_marks["latest_turn_id"]:
            navigation_link("查看持续参考 ↗", reference_anchor(cid, reference_marks["latest_turn_id"]))
        with st.container(height=420 if cards else "content", border=False):
            for card in cards:
                st.caption({"C": "C · 当前条件", "R": "R · 当前参考", "U": "U · 待承接信息"}.get(card["label"], card["label"]))
                st.markdown(card["content"])
        navigation_link("↓ 回到最新消息", latest_anchor(cid))
        errors = repo.latest_extensions(cid).get("extension_errors", {})
        if errors:
            st.warning("部分附带展示或反馈格式无效，已忽略；本轮回答和工作记忆已保存。")

    # Program errors have their own block; retained background is never replaced.
    latest_call = next(iter(repo.list_calls(cid)), None)
    if latest_call and latest_call["status"] == "failed":
        with st.container(border=True):
            st.caption("请求状态")
            st.error(latest_call.get("error") or "最近一次请求未完成。")
            st.caption("配置或凭据问题请在侧栏「模型初始设置」修改；处理后可在该轮回答处手动重试。")

turns = repo.list_turns(cid)
observations = {item["turn_id"]: item for item in feedback.list_model_feedback(cid)}
context_observations = {item["turn_id"]: item for item in feedback.list_context_feedback(cid)}
app_observations = {item["turn_id"]: item for item in feedback.list_app_feedback(cid)}
for turn in turns:
    with st.chat_message("user"):
        st.markdown(turn["user_input"])
    with st.chat_message("assistant"):
        if turn["status"] == "succeeded":
            if turn["id"] in marked_turn_ids:
                st.subheader("持续参考", anchor=reference_anchor(cid, turn["id"]))
            if turn["id"] in marked_turn_ids:
                render_reference_reply(repo, turn)
            else:
                st.markdown(turn["answer"])
                if turn.get("reference"):
                    st.markdown(turn["reference"]["content"])
        elif turn["status"] == "pending":
            st.info("本轮已登记，结果尚未提交。页面重跑不会重新调用。")
        else:
            call = repo.get_call(turn["id"])
            st.error("本轮失败，C / R / U 未改变：" + (call.get("error") or "未知错误"))
            if st.button("用当前配置显式重试", key=f"retry_{turn['id']}"):
                st.session_state["pending_submission"] = {"conversation_id": cid,
                    "user_input": turn["user_input"], "turn_id": str(uuid4())}
                st.rerun()

        render_turn_feedback(repo, feedback, comparisons, turn, observations.get(turn["id"]), context_observations.get(turn["id"]), app_observations.get(turn["id"]))

render_maintenance(repo, conversation)

pending_calls = [call for call in repo.list_calls(cid) if call["status"] == "pending"]
if pending_calls:
    with st.expander("处理中 / 中断请求"):
        st.caption("若已确认原请求不再运行，可标记失败后手动重试。原请求可能已产生费用；此操作不取消供应商请求。")
        for call in pending_calls:
            if st.button(f"标记中断 · {call['id'][:8]}", key=f"interrupt_{call['id']}"):
                repo.fail_turn(call["id"], "用户将中断请求标记为失败；未自动重试。")
                st.rerun()

# Capture the UUID at the explicit submit boundary. Re-runs execute at most this
# persisted ID; no request occurs merely from rendering widgets.
restore_key = f"restore_draft_{cid}"
if restore_key in st.session_state:
    st.session_state[f"draft_{cid}"] = st.session_state.pop(restore_key)
    st.warning("本轮在发送前被拦下，未调用模型。输入已自动恢复，可调整配置后发送。")
st.subheader("继续会话", anchor=latest_anchor(cid))
with st.form(f"chat_{cid}", clear_on_submit=True):
    user_text = st.text_area("本轮问题", key=f"draft_{cid}", placeholder="直接说明需求或继续追问，无需手工登记进度。", height=90)
    sent = st.form_submit_button("发送", disabled=bool(pending_calls), type="primary")
if sent and user_text.strip():
    st.session_state["pending_submission"] = {"conversation_id": cid,
        "user_input": user_text, "turn_id": str(uuid4())}
    st.rerun()

if submission := st.session_state.get("pending_submission"):
    try:
        with st.spinner("处理本轮请求…"):
            outcome = service.submit(**submission)
        if outcome["status"] == "failed":
            flash("请求未完成。已保留旧状态；请查看具体错误及尝试记录后再重试。")
        elif outcome.get("layout_warning"):
            flash(outcome["layout_warning"])
        elif outcome.get("feedback_warning"):
            flash(outcome["feedback_warning"])
    except (ValueError, RuntimeError) as exc:
        st.session_state[f"restore_draft_{submission['conversation_id']}"] = submission["user_input"]
        flash(str(exc))
    finally:
        st.session_state.pop("pending_submission", None)
    st.rerun()

if comparison_submission := st.session_state.get("pending_comparison"):
    try:
        with st.spinner("按实验请求生成比较版本…"):
            comparisons.run(**comparison_submission)
    except (ValueError, RuntimeError) as exc:
        flash("比较未完成：" + feedback.scrub(str(exc)))
    finally:
        st.session_state.pop("pending_comparison", None)
    st.rerun()

with st.expander("状态与请求检查"):
    st.caption("C/R/U 是模型整理的工作记忆，程序只校验结构和保存一致性；用户事实、建议与未知情况仍需结合原文判断。")
    st.write("当前工作状态")
    st.json(state_of(repo.get_conversation(cid)))
    calls = repo.list_calls(cid)
    if calls:
        latest = calls[0]
        st.write("上一次请求的状态前后值")
        st.json({"before": latest["before_state"], "after": latest.get("after_state")})
        st.write("上一次实际请求快照（发送时保存）")
        st.json(latest["request_snapshot"])
        st.json({"status": latest["status"], "elapsed_ms": latest["elapsed_ms"],
                 "http_status": latest.get("http_status"), "service_usage": latest.get("usage", {}),
                 "local_estimate": latest["request_snapshot"].get("context_meta", {}), "error": latest.get("error")})
        with st.expander("原始响应 / 更早调用记录"):
            call_id = st.selectbox("调用 ID", [c["id"] for c in calls])
            chosen_call = repo.get_call(call_id)
            st.code(chosen_call.get("raw_response") or "（无原始响应）", language="json")
            st.json(chosen_call)
            st.write("各次尝试与可选展示反馈")
            st.json(AttemptStore(repo).list(call_id))
            st.json(repo.get_turn_extensions(call_id))
    st.write("下一轮请求预览（不会调用模型）")
    preview_input = st.text_area("预览用输入", "继续", key=f"preview_{cid}")
    try:
        preview = service.preview(cid, preview_input)
        st.json(preview["request"])
    except (BudgetError, ValueError, RuntimeError) as exc:
        st.warning(str(exc))
    with st.expander("可选：手动纠正状态"):
        st.caption("只有明确点击保存才会改状态。纠正记录可在导出中检查；不修改旧问答。")
        current = repo.get_conversation(cid)
        reference = current.get("reference") or {}
        kinds = ["none", "candidate_set", "selected_plan", "recipe", "shopping_reference"]
        with st.form(f"correct_{cid}_{current['revision']}"):
            conditions = st.text_area("C 当前条件", current["conditions"])
            carry_note = st.text_area("U 待承接情况", current["carry_note"])
            kind = st.selectbox("R 类型（none 为清空）", kinds, index=kinds.index(reference.get("kind", "none")))
            content = st.text_area("R 正文", reference.get("content", ""))
            if st.form_submit_button("保存明确纠正", disabled=bool(pending_calls)):
                try:
                    repo.correct_state(cid, conditions, carry_note,
                        reference_kind=None if kind == "none" else kind,
                        reference_content=None if kind == "none" else content)
                    flash("工作状态已更新，下一轮将使用纠正后的内容。")
                    st.rerun()
                except (ValueError, RuntimeError) as exc:
                    st.error(str(exc))
