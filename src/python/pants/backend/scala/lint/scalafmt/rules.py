# Copyright 2021 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).
from __future__ import annotations

import os.path
from collections import defaultdict
from dataclasses import dataclass
from typing import cast

from pants.backend.scala.lint.scalafmt.skip_field import SkipScalafmtField
from pants.backend.scala.lint.scalafmt.subsystem import ScalafmtSubsystem
from pants.backend.scala.target_types import ScalaSourceField
from pants.core.goals.fmt import FmtResult, FmtTargetsRequest, Partitions
from pants.core.goals.resolves import ExportableTool
from pants.core.util_rules.config_files import (
    GatherConfigFilesByDirectoriesRequest,
    gather_config_files_by_workspace_dir,
)
from pants.core.util_rules.partitions import Partition
from pants.engine.fs import Digest, DigestSubset, MergeDigests, PathGlobs, Snapshot
from pants.engine.internals.selectors import concurrently
from pants.engine.intrinsics import digest_to_snapshot, merge_digests_request_to_digest
from pants.engine.process import fallible_to_exec_result_or_raise
from pants.engine.rules import collect_rules, implicitly, rule
from pants.engine.target import FieldSet, Target
from pants.engine.unions import UnionRule
from pants.jvm.goals import lockfile
from pants.jvm.jdk_rules import InternalJdk, JvmProcess
from pants.jvm.resolve.coursier_fetch import ToolClasspathRequest, materialize_classpath_for_tool
from pants.jvm.resolve.jvm_tool import GenerateJvmLockfileFromTool
from pants.util.dirutil import group_by_dir
from pants.util.frozendict import FrozenDict
from pants.util.logging import LogLevel
from pants.util.strutil import pluralize


@dataclass(frozen=True)
class ScalafmtFieldSet(FieldSet):
    required_fields = (ScalaSourceField,)

    source: ScalaSourceField

    @classmethod
    def opt_out(cls, tgt: Target) -> bool:
        return tgt.get(SkipScalafmtField).value


class ScalafmtRequest(FmtTargetsRequest):
    field_set_type = ScalafmtFieldSet
    tool_subsystem = ScalafmtSubsystem


@dataclass(frozen=True)
class GatherScalafmtConfigFilesRequest:
    filepaths: tuple[str, ...]


@dataclass(frozen=True)
class ScalafmtConfigFiles:
    snapshot: Snapshot
    source_dir_to_config_file: FrozenDict[str, str]


@dataclass(frozen=True)
class PartitionInfo:
    classpath_entries: tuple[str, ...]
    config_snapshot: Snapshot
    extra_immutable_input_digests: FrozenDict[str, Digest]

    @property
    def description(self) -> str:
        return self.config_snapshot.files[0]


@rule
async def partition_scalafmt(
    request: ScalafmtRequest.PartitionRequest, tool: ScalafmtSubsystem
) -> Partitions[PartitionInfo]:
    if tool.skip:
        return Partitions()

    toolcp_relpath = "__toolcp"

    filepaths = tuple(field_set.source.file_path for field_set in request.field_sets)
    lockfile_request = GenerateJvmLockfileFromTool.create(tool)
    tool_classpath, config_files = await concurrently(
        materialize_classpath_for_tool(ToolClasspathRequest(lockfile=lockfile_request)),
        gather_config_files_by_workspace_dir(
            GatherConfigFilesByDirectoriesRequest(
                tool_name=tool.name,
                config_filename=tool.config_file_name,
                filepaths=filepaths,
                orphan_filepath_behavior=tool.orphan_files_behavior,
            )
        ),
    )

    extra_immutable_input_digests = {
        toolcp_relpath: tool_classpath.digest,
    }

    # Partition the work by which source files share the same config file (regardless of directory).
    source_files_by_config_file: dict[str, set[str]] = defaultdict(set)
    for source_dir, files_in_source_dir in group_by_dir(filepaths).items():
        config_file = config_files.source_dir_to_config_file[source_dir]
        source_files_by_config_file[config_file].update(
            os.path.join(source_dir, name) for name in files_in_source_dir
        )

    config_file_snapshots = await concurrently(
        digest_to_snapshot(
            **implicitly(DigestSubset(config_files.snapshot.digest, PathGlobs([config_file])))
        )
        for config_file in source_files_by_config_file
    )

    return Partitions(
        Partition(
            tuple(files),
            PartitionInfo(
                classpath_entries=tuple(tool_classpath.classpath_entries(toolcp_relpath)),
                config_snapshot=config_snapshot,
                extra_immutable_input_digests=FrozenDict(extra_immutable_input_digests),
            ),
        )
        for files, config_snapshot in zip(
            source_files_by_config_file.values(), config_file_snapshots
        )
    )


@rule(desc="Format with scalafmt", level=LogLevel.DEBUG)
async def scalafmt_fmt(
    request: ScalafmtRequest.Batch, jdk: InternalJdk, tool: ScalafmtSubsystem
) -> FmtResult:
    partition_info = cast(PartitionInfo, request.partition_metadata)
    merged_digest = await merge_digests_request_to_digest(
        MergeDigests([partition_info.config_snapshot.digest, request.snapshot.digest])
    )

    result = await fallible_to_exec_result_or_raise(
        **implicitly(
            JvmProcess(
                jdk=jdk,
                argv=[
                    "org.scalafmt.cli.Cli",
                    f"--config={partition_info.config_snapshot.files[0]}",
                    "--non-interactive",
                    *request.files,
                ],
                classpath_entries=partition_info.classpath_entries,
                input_digest=merged_digest,
                output_files=request.files,
                extra_jvm_options=tool.jvm_options,
                extra_immutable_input_digests=partition_info.extra_immutable_input_digests,
                # extra_nailgun_keys=request.extra_immutable_input_digests,
                use_nailgun=False,
                description=f"Run `scalafmt` on {pluralize(len(request.files), 'file')}.",
                level=LogLevel.DEBUG,
            ),
        )
    )
    return await FmtResult.create(request, result)


def rules():
    return [
        *collect_rules(),
        *lockfile.rules(),
        *ScalafmtRequest.rules(),
        UnionRule(ExportableTool, ScalafmtSubsystem),
    ]
