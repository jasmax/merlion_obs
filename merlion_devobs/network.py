"""The pipeline the dev team is building (architecture.md), shared with the production stack:

    intake -> resolveOrder -> chat (LLM + getDeliveryHistory, getPodScan, getGpsTrail, getRate, searchPolicy) -> answer_check

See merlion_obs/agent.py. A run config picks a version for every step and a model for the chat step.
"""
from merlion_obs.agent import COMPONENTS, TOOL_PLAN, Pipeline, oracle

Network = Pipeline
__all__ = ["COMPONENTS", "TOOL_PLAN", "Network", "oracle"]
