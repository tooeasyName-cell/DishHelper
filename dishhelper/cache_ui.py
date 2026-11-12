"""Read-only cache usage display; never estimates or fills absent token counts."""
import streamlit as st

from .cache_usage import summarize_cache_usage


def render_cache_usage(usages, label="缓存命中统计"):
    report = summarize_cache_usage(usages)
    ratio = report.get("cache_hit_ratio")
    if report.get("input_tokens") == 0 and report.get("cache_hit_tokens") == 0:
        st.caption(label + "：已报告输入0、命中0 tokens；无输入，命中率不适用。")
    elif ratio is None:
        st.caption(label + "：服务方未报告足够用量或数据不一致，命中率未知。")
    else:
        st.caption(label + f"（已报告范围）：命中 {report['rate_cache_hit_tokens']} / 输入 {report['rate_input_tokens']} tokens，命中率 {ratio:.1%}；可计算 {report['calls_with_cache_ratio']}/{report['calls']} 次。")
    with st.expander(label + " · 明细", expanded=False):
        st.json(report)
        st.caption("只统计服务方已报告用量；未知不当作0。未据此推算费用或保证缓存收益。")
    return report
