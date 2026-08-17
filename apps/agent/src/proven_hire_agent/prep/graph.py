"""Compiled LangGraph ``StateGraph`` for the prep pipeline.

Topology (fan-out, join, second fan-out into the three rounds, join again)::

    START ─┬─> fetch_cv ─> cv_analysis ─┐
           ├─> jd_analysis ─────────────┼─> gap_matching ─┬─> general_round     ─┐
           └─> company_research ────────┘                 ├─> coding_round      ─┼─> assemble_plan ─> END
                                                            └─> behavioral_round  ─┘

``cv_analysis``, ``jd_analysis`` and ``company_research`` run concurrently. The
join into ``gap_matching`` uses a list ``start_key`` so LangGraph waits for all
three branches before running it. ``gap_matching`` then fans out into the three
round-generation nodes (general/coding/behavioral) — each makes its own focused
LLM call (see ``docs/QUESTION_GENERATION_SPEC.md``) — which run concurrently and
join into ``assemble_plan``, the trivial node that stitches their question lists
into the final ``QuestionPlan``.

Deps are bound into each node with :func:`functools.partial` so the compiled node
presents the ``(state)`` signature LangGraph invokes.
"""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING

from langgraph.graph import END, START, StateGraph

from . import nodes
from .state import PrepState

if TYPE_CHECKING:
    from langgraph.graph.state import CompiledStateGraph

    from ..core.deps import Deps


def build_prep_graph(deps: Deps) -> CompiledStateGraph:
    """Build and compile the prep ``StateGraph`` with ``deps`` bound into nodes."""
    graph: StateGraph = StateGraph(PrepState)

    graph.add_node("fetch_cv", partial(nodes.fetch_cv, deps=deps))
    graph.add_node("cv_analysis", partial(nodes.cv_analysis, deps=deps))
    graph.add_node("jd_analysis", partial(nodes.jd_analysis, deps=deps))
    graph.add_node("company_research", partial(nodes.company_research, deps=deps))
    graph.add_node("gap_matching", partial(nodes.gap_matching, deps=deps))
    graph.add_node("general_round", partial(nodes.general_round, deps=deps))
    graph.add_node("coding_round", partial(nodes.coding_round, deps=deps))
    graph.add_node("behavioral_round", partial(nodes.behavioral_round, deps=deps))
    graph.add_node("assemble_plan", partial(nodes.assemble_plan, deps=deps))

    # Fan-out: three independent branches start from START concurrently.
    graph.add_edge(START, "fetch_cv")
    graph.add_edge("fetch_cv", "cv_analysis")
    graph.add_edge(START, "jd_analysis")
    graph.add_edge(START, "company_research")

    # Join: gap_matching waits for candidate (cv) + job (jd) + company research.
    graph.add_edge(["cv_analysis", "jd_analysis", "company_research"], "gap_matching")

    # Second fan-out: the three round-generation nodes run concurrently off
    # gap_matching, each making its own focused LLM call.
    graph.add_edge("gap_matching", "general_round")
    graph.add_edge("gap_matching", "coding_round")
    graph.add_edge("gap_matching", "behavioral_round")

    # Join: assemble_plan waits for all three rounds, then finishes.
    graph.add_edge(["general_round", "coding_round", "behavioral_round"], "assemble_plan")
    graph.add_edge("assemble_plan", END)

    return graph.compile()
