"""Tree-order reorder module — diff + plan + apply.

Public API:
    compute_plan       — pure function: (desired, current, src→dst) → TreeOrderPlan
    DEFAULT_MAX_CHILDREN  — threshold mặc định (50)

Stage I/O orchestration sống trong `waytoagi.stages.reorder`.
"""

from waytoagi.reorder.diff import DEFAULT_MAX_CHILDREN, compute_plan

__all__ = ["DEFAULT_MAX_CHILDREN", "compute_plan"]
