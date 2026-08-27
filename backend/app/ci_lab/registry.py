"""Immutable CI definitions compiled into the teaching service.

Definitions contain only deterministic waits and fixed failure decisions.
There is deliberately no field for a command, executable, module, URL,
container image, or filesystem path.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from app.ci_lab.models import (
    DefinitionJobView,
    DefinitionStageView,
    DefinitionView,
)


@dataclass(frozen=True, slots=True)
class JobDefinition:
    key: str
    name: str
    duration_ms: int
    should_fail: bool = False

    def __post_init__(self) -> None:
        if not self.key or not self.name or not 0 <= self.duration_ms <= 60_000:
            raise ValueError("invalid fixed CI job definition")


@dataclass(frozen=True, slots=True)
class StageDefinition:
    key: str
    name: str
    jobs: tuple[JobDefinition, ...]

    def __post_init__(self) -> None:
        if not self.key or not self.name or not self.jobs:
            raise ValueError("invalid fixed CI stage definition")
        keys = [job.key for job in self.jobs]
        if len(keys) != len(set(keys)):
            raise ValueError("job keys must be unique inside a fixed CI stage")


@dataclass(frozen=True, slots=True)
class PipelineDefinition:
    key: str
    name: str
    revision: int
    queue_delay_ms: int
    stages: tuple[StageDefinition, ...]

    def __post_init__(self) -> None:
        if (
            not self.key
            or not self.name
            or self.revision < 1
            or not 0 <= self.queue_delay_ms <= 60_000
            or not self.stages
        ):
            raise ValueError("invalid fixed CI pipeline definition")
        stage_keys = [stage.key for stage in self.stages]
        if len(stage_keys) != len(set(stage_keys)):
            raise ValueError("stage keys must be unique inside a fixed CI definition")
        all_job_keys = [job.key for stage in self.stages for job in stage.jobs]
        if len(all_job_keys) != len(set(all_job_keys)):
            raise ValueError("job keys must be unique across a fixed CI definition")

    @property
    def jobs(self) -> tuple[JobDefinition, ...]:
        return tuple(job for stage in self.stages for job in stage.jobs)

    def to_view(self) -> DefinitionView:
        return DefinitionView(
            key=self.key,
            name=self.name,
            revision=self.revision,
            queue_delay_ms=self.queue_delay_ms,
            stages=[
                DefinitionStageView(
                    key=stage.key,
                    name=stage.name,
                    jobs=[
                        DefinitionJobView(
                            key=job.key,
                            name=job.name,
                            duration_ms=job.duration_ms,
                            should_fail=job.should_fail,
                        )
                        for job in stage.jobs
                    ],
                )
                for stage in self.stages
            ],
        )


DefinitionRegistry = Mapping[str, PipelineDefinition]


def build_default_definition_registry() -> DefinitionRegistry:
    definitions = (
        PipelineDefinition(
            key="local-quality-gate",
            name="Local QA quality gate",
            revision=1,
            queue_delay_ms=100,
            stages=(
                StageDefinition(
                    key="validate",
                    name="Validate",
                    jobs=(
                        JobDefinition(
                            key="validate-input",
                            name="Validate bounded inputs",
                            duration_ms=100,
                        ),
                    ),
                ),
                StageDefinition(
                    key="test",
                    name="Test",
                    jobs=(
                        JobDefinition(
                            key="deterministic-tests",
                            name="Run deterministic tests",
                            duration_ms=300,
                        ),
                    ),
                ),
                StageDefinition(
                    key="report",
                    name="Report",
                    jobs=(
                        JobDefinition(
                            key="quality-summary",
                            name="Build quality summary",
                            duration_ms=100,
                        ),
                    ),
                ),
            ),
        ),
        PipelineDefinition(
            key="local-failure-demo",
            name="Local deterministic failure",
            revision=1,
            queue_delay_ms=50,
            stages=(
                StageDefinition(
                    key="validate",
                    name="Validate",
                    jobs=(
                        JobDefinition(
                            key="validate-input",
                            name="Validate bounded inputs",
                            duration_ms=50,
                        ),
                    ),
                ),
                StageDefinition(
                    key="test",
                    name="Test",
                    jobs=(
                        JobDefinition(
                            key="fixed-failure",
                            name="Demonstrate deterministic failure",
                            duration_ms=100,
                            should_fail=True,
                        ),
                    ),
                ),
                StageDefinition(
                    key="package",
                    name="Package",
                    jobs=(
                        JobDefinition(
                            key="never-runs-after-failure",
                            name="Cancelled downstream work",
                            duration_ms=100,
                        ),
                    ),
                ),
            ),
        ),
    )
    return MappingProxyType({definition.key: definition for definition in definitions})


DEFAULT_DEFINITION_REGISTRY = build_default_definition_registry()


__all__ = [
    "DEFAULT_DEFINITION_REGISTRY",
    "DefinitionRegistry",
    "JobDefinition",
    "PipelineDefinition",
    "StageDefinition",
    "build_default_definition_registry",
]
