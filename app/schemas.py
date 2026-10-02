"""Pydantic request/response models for the microplate arrangement API."""
from __future__ import annotations

import math
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Categories reported in 422 responses when no layout can exist.
# They identify the class of constraint that makes the instance infeasible.
VIOLATION_SAMPLE_IDS_DUPLICATED = "SAMPLE_IDS_DUPLICATED"
VIOLATION_GROUP_BATCH_INVALID = "GROUP_BATCH_INVALID"
VIOLATION_PLATE_DIMENSION_INVALID = "PLATE_DIMENSION_INVALID"
VIOLATION_WELL_REFERENCE_INVALID = "WELL_REFERENCE_INVALID"
VIOLATION_FIXED_CONFLICT = "FIXED_CONFLICT"
VIOLATION_FIXED_ON_BLOCKED = "FIXED_ON_BLOCKED"
VIOLATION_CAPACITY_EXCEEDED = "CAPACITY_EXCEEDED"
VIOLATION_SOLVER_INFEASIBLE = "SOLVER_INFEASIBLE"

# Categories reported in 422 responses of the reconcile endpoint when the
# target layout and the scanned plate state are inconsistent.
VIOLATION_TARGET_WELLS_DUPLICATED = "TARGET_WELLS_DUPLICATED"
VIOLATION_TARGET_ON_BLOCKED = "TARGET_ON_BLOCKED"
VIOLATION_SCAN_WELLS_DUPLICATED = "SCAN_WELLS_DUPLICATED"
VIOLATION_SCAN_SAMPLES_DUPLICATED = "SCAN_SAMPLES_DUPLICATED"
VIOLATION_SAMPLES_MISSING = "SAMPLES_MISSING"
VIOLATION_UNPLANNED_SAMPLES = "UNPLANNED_SAMPLES"
VIOLATION_BLOCKED_WELL_OCCUPIED = "BLOCKED_WELL_OCCUPIED"

# Categories reported in 422 responses of the spatial QC endpoint.
VIOLATION_SPATIAL_SAMPLE_DUPLICATED = "SPATIAL_SAMPLE_DUPLICATED"
VIOLATION_SPATIAL_SAMPLE_UNKNOWN = "SPATIAL_SAMPLE_UNKNOWN"
VIOLATION_SPATIAL_LAYOUT_CONFLICT = "SPATIAL_LAYOUT_CONFLICT"
VIOLATION_SPATIAL_READING_INVALID = "SPATIAL_READING_INVALID"
VIOLATION_SPATIAL_READING_DUPLICATED = "SPATIAL_READING_DUPLICATED"
VIOLATION_SPATIAL_GROUP_INSUFFICIENT = "SPATIAL_GROUP_INSUFFICIENT"
VIOLATION_SPATIAL_GRID_INSUFFICIENT = "SPATIAL_GRID_INSUFFICIENT"
VIOLATION_SPATIAL_NOT_CONVERGED = "SPATIAL_NOT_CONVERGED"

# Categories reported in 422 responses of the batch release endpoint when the
# scanned plate does not agree with the planned layout (stage 1 of release).
VIOLATION_CLOSED_LOOP_MISPLACEMENT = "CLOSED_LOOP_MISPLACEMENT"
VIOLATION_SCAN_LAYOUT_MISMATCH = "SCAN_LAYOUT_MISMATCH"

# Reasons reported (in ``reasons``) when the scan matched but the spatial QC
# release gates are not all satisfied; the batch is then NOT_RELEASED.
RELEASE_REASON_SPATIAL_EFFECT_EXCEEDED = "SPATIAL_EFFECT_EXCEEDED"
RELEASE_REASON_P_VALUE_BELOW_THRESHOLD = "P_VALUE_BELOW_THRESHOLD"
RELEASE_REASON_P_VALUE_UNAVAILABLE = "P_VALUE_UNAVAILABLE"

# Categories reported in 422 responses of the blinding endpoints.
VIOLATION_BLIND_WELLS_DUPLICATED = "BLIND_WELLS_DUPLICATED"
VIOLATION_BLIND_SAMPLE_WELL_MISMATCH = "BLIND_SAMPLE_WELL_MISMATCH"
VIOLATION_BLIND_VIEW_INVALID = "BLIND_VIEW_INVALID"
VIOLATION_BLIND_VIEW_INTEGRITY_FAILED = "BLIND_VIEW_INTEGRITY_FAILED"
VIOLATION_BLIND_SHARE_INVALID = "BLIND_SHARE_INVALID"
VIOLATION_BLIND_PACKAGE_INVALID = "BLIND_PACKAGE_INVALID"
VIOLATION_BLIND_INTEGRITY_FAILED = "BLIND_INTEGRITY_FAILED"

# Categories reported in 422 responses of the dilution-plan endpoint.
VIOLATION_DILUTION_SOURCE_DUPLICATED = "DILUTION_SOURCE_DUPLICATED"
VIOLATION_DILUTION_SOURCE_UNKNOWN = "DILUTION_SOURCE_UNKNOWN"
VIOLATION_DILUTION_TARGET_WELL_DUPLICATED = "DILUTION_TARGET_WELL_DUPLICATED"
VIOLATION_DILUTION_TARGET_WELL_NOT_IN_LAYOUT = "DILUTION_TARGET_WELL_NOT_IN_LAYOUT"
VIOLATION_DILUTION_TARGET_CONCENTRATION_INVALID = "DILUTION_TARGET_CONCENTRATION_INVALID"
VIOLATION_DILUTION_VOLUME_INVALID = "DILUTION_VOLUME_INVALID"
VIOLATION_DILUTION_WELL_CAPACITY_EXCEEDED = "DILUTION_WELL_CAPACITY_EXCEEDED"
VIOLATION_DILUTION_SOURCE_INSUFFICIENT = "DILUTION_SOURCE_INSUFFICIENT"
VIOLATION_DILUTION_MIN_VOLUME_VIOLATION = "DILUTION_MIN_VOLUME_VIOLATION"
VIOLATION_DILUTION_TOLERANCE_UNREACHABLE = "DILUTION_TOLERANCE_UNREACHABLE"


class PlateSpec(BaseModel):
    """Plate geometry.

    ``type`` selects a standard footprint ("24"/"48"/"96"/"384") or
    "custom", in which case ``rows`` and ``columns`` must be supplied.
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal["24", "48", "96", "384", "custom"] = "96"
    rows: Optional[int] = Field(default=None, ge=1, le=26)
    columns: Optional[int] = Field(default=None, ge=1, le=50)


class Sample(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    group: str = Field(min_length=1, description="实验组标识")
    batch: str = Field(min_length=1, description="批次标识")


class FixedAssignment(BaseModel):
    """A sample pinned to a specific well; the solver may never move it."""

    model_config = ConfigDict(extra="forbid")

    sample_id: str = Field(min_length=1)
    well: str = Field(min_length=1, description="孔位,如 'C5'")


class ArrangeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plate: PlateSpec = Field(default_factory=PlateSpec)
    samples: list[Sample]
    fixed: list[FixedAssignment] = Field(default_factory=list)
    blocked_wells: list[str] = Field(default_factory=list)
    seed: int = Field(default=0, ge=0)

    @field_validator("samples")
    @classmethod
    def _nonempty_samples(cls, v: list[Sample]) -> list[Sample]:
        if not v:
            raise ValueError("samples must contain at least one sample")
        return v


class ReconcileRequest(BaseModel):
    """Reconcile a physically scanned plate against a planned layout.

    ``target`` is the sample → well mapping exactly as returned in the
    ``layout`` field of ``POST /api/v1/arrangements``. ``scan`` is the
    observed well → sample mapping from a full plate scan; empty wells are
    simply omitted. ``staging_well`` optionally names a free, non-target,
    non-blocked well that may be borrowed to break closed-loop
    misplacements; it is restored to empty by the final instruction.
    """

    model_config = ConfigDict(extra="forbid")

    plate: PlateSpec = Field(default_factory=PlateSpec)
    target: dict[str, str] = Field(
        min_length=1, description="目标映射:样本 id → 目标孔位(原编排接口的 layout)"
    )
    scan: dict[str, str] = Field(
        default_factory=dict, description="扫描映射:孔位 → 样本 id(空孔不列出)"
    )
    blocked_wells: list[str] = Field(default_factory=list)
    staging_well: Optional[str] = Field(
        default=None, description="空闲且非目标的暂存孔,用于解开闭环错位"
    )


class SpatialSample(BaseModel):
    """A sample carrying an experiment group label for spatial QC."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    group: str = Field(min_length=1, description="实验组标识;中位数中心化与置换均在组内进行")


class SpatialQCRequest(BaseModel):
    """Spatial QC request.

    ``layout`` is the sample → well mapping exactly as returned in the
    ``layout`` field of ``POST /api/v1/arrangements``. ``readings`` maps each
    well to its numeric assay value; wells may be omitted or mapped to null
    to mark missing readings. Group medians are removed first, then row and
    column medians are stripped and accumulated by alternating median polish
    sweeps. An optional seed-controlled within-group permutation test yields
    an empirical, plus-one-corrected p-value for the maximal row/column
    effect.
    """

    model_config = ConfigDict(extra="forbid")

    plate: PlateSpec = Field(default_factory=PlateSpec)
    layout: dict[str, str] = Field(
        min_length=1, description="布局映射:样本 id → 孔位(编排接口响应中的 layout)"
    )
    samples: list[SpatialSample] = Field(min_length=1)
    readings: dict[str, Optional[float]] = Field(
        default_factory=dict,
        description="孔位 → 数值读数;省略孔位或显式 null 表示读数缺失",
    )
    seed: int = Field(default=0, ge=0, description="随机种子(≥0),控制组内置换")
    permutations: int = Field(
        default=1000, ge=0, le=1_000_000, description="组内置换次数(≥0,0 表示跳过置换)"
    )
    tolerance: float = Field(
        default=1e-6, gt=0, description="收敛阈值:本轮最大行列校正量 ≤ tolerance 即收敛"
    )
    max_iterations: int = Field(
        default=1000, ge=1, description="行列交替剥离的最大轮数"
    )

    @field_validator("tolerance")
    @classmethod
    def _tolerance_finite(cls, v: float) -> float:
        if not math.isfinite(v):
            raise ValueError("tolerance must be a finite number")
        return v

    @field_validator("readings")
    @classmethod
    def _readings_finite(cls, v: dict[str, Optional[float]]) -> dict[str, Optional[float]]:
        for well, value in v.items():
            if value is not None and not math.isfinite(value):
                raise ValueError(f"reading at well '{well}' must be a finite number or null")
        return v


class BatchReleaseRequest(BaseModel):
    """Batch release verification request.

    Stage 1 reuses the reconciliation rules exactly: the complete ``scan``
    must agree well-by-well with the planned ``target`` layout — no missing
    planned sample, no unplanned sample, no closed-loop (or open-chain)
    misplacement and no blocked-well occupancy. Any disagreement yields 422
    with the problem categories and the misplaced-well list; no release
    conclusion is produced.

    Stage 2 runs only after the scan matches, reusing the spatial QC
    pipeline. The batch is released only when the polish converged, the
    maximal row/column effect is at most ``max_spatial_effect`` and the
    permutation p-value reaches ``min_p_value``. Any failed gate returns a
    NOT_RELEASED verdict with the failing reason(s); no partial conclusion is
    emitted.
    """

    model_config = ConfigDict(extra="forbid")

    plate: PlateSpec = Field(default_factory=PlateSpec)
    target: dict[str, str] = Field(
        min_length=1, description="计划映射:样本 id → 计划孔位(原编排接口的 layout)"
    )
    scan: dict[str, str] = Field(
        default_factory=dict, description="完整扫描:孔位 → 样本 id(空孔不列出)"
    )
    blocked_wells: list[str] = Field(default_factory=list)
    samples: list[SpatialSample] = Field(
        min_length=1, description="样本分组,每项 {id, group};应与计划样本一一对应"
    )
    readings: dict[str, Optional[float]] = Field(
        default_factory=dict,
        description="孔位 → 数值读数;省略孔位或显式 null 表示读数缺失",
    )
    seed: int = Field(default=0, ge=0, description="随机种子(≥0),控制组内置换")
    max_spatial_effect: float = Field(
        gt=0, description="空间效应上限:最大行列效应绝对值必须 ≤ 该值"
    )
    min_p_value: float = Field(
        ge=0.0,
        le=1.0,
        description="最低置换 p 值门槛(0–1);permutations=0 时无 p 值,该门无法通过",
    )
    permutations: int = Field(
        default=1000, ge=0, le=1_000_000, description="组内置换次数(≥0,0 表示跳过置换)"
    )
    tolerance: float = Field(
        default=1e-6, gt=0, description="收敛阈值:本轮最大行列校正量 ≤ tolerance 即收敛"
    )
    max_iterations: int = Field(
        default=1000, ge=1, description="行列交替剥离的最大轮数"
    )

    @field_validator("max_spatial_effect")
    @classmethod
    def _max_effect_finite(cls, v: float) -> float:
        if not math.isfinite(v):
            raise ValueError("max_spatial_effect must be a finite number")
        return v

    @field_validator("tolerance")
    @classmethod
    def _tolerance_finite(cls, v: float) -> float:
        if not math.isfinite(v):
            raise ValueError("tolerance must be a finite number")
        return v

    @field_validator("readings")
    @classmethod
    def _readings_finite(cls, v: dict[str, Optional[float]]) -> dict[str, Optional[float]]:
        for well, value in v.items():
            if value is not None and not math.isfinite(value):
                raise ValueError(f"reading at well '{well}' must be a finite number or null")
        return v


class BlindRequest(BaseModel):
    """Two-party collaborative blinding of an existing arrangement result.

    ``batch_id`` names the production batch; ``layout`` is the sample → well
    mapping exactly as returned in the ``layout`` field of
    ``POST /api/v1/arrangements``. Every sample must map to exactly one legal
    well and every well to at most one sample, otherwise the request is
    rejected with 422 and no blind codes, mapping package or key shares are
    produced.
    """

    model_config = ConfigDict(extra="forbid")

    plate: PlateSpec = Field(default_factory=PlateSpec)
    batch_id: str = Field(min_length=1, description="批次标识")
    layout: dict[str, str] = Field(
        min_length=1, description="样本 id → 孔位(编排接口响应中的 layout)"
    )


class BlindViewEntry(BaseModel):
    """One row of the blinded operational view: well only, no sample id."""

    model_config = ConfigDict(extra="forbid")

    well: str = Field(min_length=1)
    blind_id: str = Field(min_length=1)


class UnblindRequest(BaseModel):
    """Unblinding requires all four artifacts at once.

    The mapping package binds ``batch_id``, the plate and the operational
    view; a single key share reveals nothing. Any integrity, share or batch
    mismatch yields 422 with no partial correspondence leaked.
    """

    model_config = ConfigDict(extra="forbid")

    batch_id: str = Field(min_length=1, description="批次标识,必须与盲化时一致")
    plate: PlateSpec = Field(default_factory=PlateSpec)
    view: list[BlindViewEntry] = Field(
        min_length=1, description="盲化时返回的完整操作视图(well + blind_id),顺序不敏感"
    )
    mapping_package: str = Field(min_length=1, description="盲化时返回的经认证加密映射包")
    key_shares: list[str] = Field(
        min_length=2,
        max_length=2,
        description="两份不同的密钥份额(顺序不敏感);缺少、相同或多余均拒绝",
    )


class DilutionSource(BaseModel):
    """A source sample stock with known concentration and usable volume.

    Volumes are in microlitres; concentration units are arbitrary but must be
    identical across source stocks and target wells (e.g. ng/uL). The same
    source may feed several target wells; its available volume is shared.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, description="来源样本 id,必须出现在 layout 中")
    concentration: float = Field(gt=0, description="来源液浓度(>0)")
    available_volume: float = Field(gt=0, description="该来源的可用体积(µL,>0),多目标共享")


class DilutionTarget(BaseModel):
    """One target well requiring a specified final concentration/volume."""

    model_config = ConfigDict(extra="forbid")

    well: str = Field(min_length=1, description="目标孔位,如 'C5',必须是 layout 中的已占孔")
    target_concentration: float = Field(ge=0, description="目标浓度(≥0;0 表示只用稀释液)")
    final_volume: float = Field(gt=0, description="目标终体积(µL,>0),必须是移液分辨率的整数倍")


class DilutionPlanRequest(BaseModel):
    """Dilution-gradient preparation plan for an existing arrangement.

    ``layout`` is the sample → well mapping exactly as returned in the
    ``layout`` field of ``POST /api/v1/arrangements``; each target well must
    name the well occupied in that layout by the source sample it should be
    filled from. The service computes, per target well, how much source stock
    and how much diluent to pipette so that the realized concentration (after
    rounding every transfer to ``pipette_resolution``) stays within
    ``relative_tolerance`` (or ``absolute_tolerance``) of the requested
    concentration, while the shared source inventories cover all draws. Any
    single failure (short stock, well capacity, minimum aspirate, rounding
    outside tolerance) aborts the whole request with 422 and itemized reasons;
    no partial plan is returned. Identical inputs yield an identical plan.
    """

    model_config = ConfigDict(extra="forbid")

    plate: PlateSpec = Field(default_factory=PlateSpec)
    layout: dict[str, str] = Field(
        min_length=1, description="布局映射:样本 id → 孔位(编排接口响应中的 layout)"
    )
    sources: list[DilutionSource] = Field(min_length=1)
    targets: list[DilutionTarget] = Field(min_length=1)
    well_capacity: float = Field(gt=0, description="每个目标孔的容量上限(µL,>0),终体积不得超过")
    diluent: str = Field(min_length=1, description="稀释液标识,如 'PBS'")
    pipette_resolution: float = Field(gt=0, description="移液分辨率(µL,>0);每次移液体积均为其整数倍")
    min_pipette_volume: float = Field(
        gt=0, description="最小吸取量(µL,>0);每次移液体积为 0 或 ≥ 该值"
    )
    relative_tolerance: float = Field(
        gt=0, le=1, description="相对浓度误差上限(分数,如 0.01 表示 1%),>0"
    )
    absolute_tolerance: float = Field(
        default=0.0,
        ge=0,
        description="绝对浓度误差上限(≥0,默认 0);实际误差 ≤ 相对或绝对上限即达标",
    )

    @field_validator(
        "pipette_resolution",
        "min_pipette_volume",
        "well_capacity",
        "relative_tolerance",
        "absolute_tolerance",
    )
    @classmethod
    def _finite_positive(cls, v: float) -> float:
        if not math.isfinite(v):
            raise ValueError("must be a finite number")
        return v

    @field_validator("sources")
    @classmethod
    def _sources_finite(cls, v: list[DilutionSource]) -> list[DilutionSource]:
        for s in v:
            if not math.isfinite(s.concentration) or not math.isfinite(s.available_volume):
                raise ValueError(f"source '{s.id}' values must be finite")
        return v

    @field_validator("targets")
    @classmethod
    def _targets_finite(cls, v: list[DilutionTarget]) -> list[DilutionTarget]:
        for t in v:
            if not math.isfinite(t.target_concentration) or not math.isfinite(t.final_volume):
                raise ValueError(f"target well '{t.well}' values must be finite")
        return v
