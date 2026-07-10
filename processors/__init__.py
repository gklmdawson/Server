"""Processor registry.

A processor turns an assigned job into a running payload process and decides
when it's genuinely done. Adding a new application = add a module here, list
its class in ALL_PROCESSORS, and put its job type in that machine's agent
config capabilities. The coordinator never changes.
"""
from __future__ import annotations

from processors.base import Processor, ProcessorError, Validation
from processors.mock import MockProcessor

ALL_PROCESSORS: list[type[Processor]] = [
    MockProcessor,
    # Phase 3+: TerraPpkProcessor, TerraLidarProcessor, Pix4dMaticProcessor,
    #           CycloneClassifyProcessor
]


def build_registry(agent_cfg, capabilities: list[str]) -> dict[str, Processor]:
    """Instantiate processors covering `capabilities`; error on gaps so a
    misconfigured agent fails loudly at startup instead of at assignment."""
    registry: dict[str, Processor] = {}
    for cls in ALL_PROCESSORS:
        instance = None
        for job_type in cls.job_types:
            if job_type in capabilities:
                if instance is None:
                    instance = cls(agent_cfg)
                registry[job_type] = instance
    missing = [c for c in capabilities if c not in registry]
    if missing:
        raise ProcessorError(
            f"No processor implements configured capabilities: {', '.join(missing)}"
        )
    return registry
