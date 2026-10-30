"""Local, same-page background navigation; no provider or state updates."""
from hashlib import sha256
from html import escape

import streamlit as st


def background_anchor(conversation_id, card):
    source = card.get("source_turn_id") or "current"
    identity = f"{conversation_id}:{source}:{card['source_index']}"
    return "background-" + sha256(identity.encode("utf-8")).hexdigest()[:24]


def reference_anchor(conversation_id, turn_id):
    identity = f"{conversation_id}:{turn_id}"
    return "reference-" + sha256(identity.encode("utf-8")).hexdigest()[:24]


def latest_anchor(conversation_id):
    return "latest-" + sha256(conversation_id.encode("utf-8")).hexdigest()[:24]


def navigation_link(text, anchor):
    # Only app-owned IDs and escaped labels enter HTML. Model text stays in
    # st.markdown's normal renderer; there is no generated or executable code.
    st.html(f'<a href="#{escape(anchor, quote=True)}" target="_self" role="button" '
            'style="display:block;padding:.4rem .65rem;margin:.25rem 0 .5rem;'
            'border:1px solid currentColor;border-radius:.5rem;text-decoration:none;'
            f'text-align:center;font-size:.9rem">{escape(text)}</a>')


def render_background_updates(conversation_id, cards):
    for card in cards:
        st.subheader("背景附注 · " + card["label"], anchor=background_anchor(conversation_id, card))
        st.markdown(card["content"])
