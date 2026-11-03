"""StateGraph 构建（第 14.3 节固定流程子集）。规划循环（planner_loop）是 P3 内容，尚未实现。"""
from __future__ import annotations

from functools import lru_cache

from langgraph.graph import END, START, StateGraph

from . import nodes
from .state import AgentDeps, AgentState


@lru_cache
def build_fixed_pipeline():
    g = StateGraph(AgentState, context_schema=AgentDeps)
    for fn in (nodes.load_snapshot, nodes.load_memory, nodes.observe_attachments, nodes.prefetch,
               nodes.load_knowledge, nodes.compose_reply, nodes.validate_output, nodes.repair_output,
               nodes.persist_candidate):
        g.add_node(fn.__name__, fn)

    g.add_edge(START, "load_snapshot")
    g.add_edge("load_snapshot", "load_memory")
    # 先读附件：图片里读到的零件号/SKU 也作为预取线索
    g.add_edge("load_memory", "observe_attachments")
    g.add_edge("observe_attachments", "prefetch")
    g.add_edge("prefetch", "load_knowledge")
    g.add_edge("load_knowledge", "compose_reply")
    g.add_edge("compose_reply", "validate_output")
    g.add_conditional_edges("validate_output", lambda s: s.get("route", "persist"),
                            {"repair": "repair_output", "persist": "persist_candidate"})
    g.add_edge("repair_output", "validate_output")
    g.add_edge("persist_candidate", END)
    # P2 固定流程崩溃后按 job_id + run_generation 从头幂等重跑，不依赖 checkpoint（第 14.5 节）；
    # P3 规划循环再接 PostgresSaver（agent_checkpoint schema）。
    return g.compile()
