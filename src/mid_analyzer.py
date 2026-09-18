"""Mid + LastPrice historical anomaly analysis.

This module deliberately keeps the detector independent from the existing tick
detector pipeline.  The existing pipeline merges equal time keys and derives
Turnover based fields; this analysis needs the original row order and only
uses the first-level quote, LastPrice, Volume, and time.
"""

from __future__ import annotations

import json
import math
import re
import zipfile
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, BinaryIO, Callable, Iterable, Iterator, Sequence

import numpy as np
import pandas as pd

from src.tick_detector.tick_io import CONTINUOUS_KEYWORDS, CONTRACT_RE, normalize_contract_code, parse_contract_file


MODEL_VERSION = "mid-last-v1"
DEFAULT_THRESHOLDS = (0.0025, 0.005, 0.0075, 0.01, 0.0125, 0.015, 0.02, 0.025, 0.03)
DEFAULT_HORIZONS = (1, 3, 5, 10, 30, 60)
REQUIRED_COLUMNS = (
    "TradingDay",
    "InstrumentID",
    "UpdateTime",
    "LastPrice",
    "Volume",
    "BidPrice1",
    "AskPrice1",
)
OPTIONAL_COLUMNS = ("UpdateMillisec", "BidVolume1", "AskVolume1")
PRICE_MAX = 1_000_000_000.0
FILE_DATE_RE = re.compile(r"_(\d{8})\.csv$", re.IGNORECASE)


@dataclass(frozen=True)
class Config:
    thresholds: tuple[float, ...] = DEFAULT_THRESHOLDS
    horizons: tuple[int, ...] = DEFAULT_HORIZONS
    event_merge_seconds: float = 5.0
    session_gap_seconds: float = 300.0
    sample_delay_tolerance: float = 1.0
    mfe_mae_horizon: int = 60
    window_before_seconds: float = 30.0


@dataclass(frozen=True)
class Source:
    """A CSV, or one CSV member inside a ZIP archive."""

    path: Path
    member: str | None
    file_name: str
    trade_date: str | None
    contract_hint: str | None
    priority: int
    source_type: str

    @property
    def label(self) -> str:
        return f"{self.path}::{self.member}" if self.member else str(self.path)


@dataclass
class SourceDiscovery:
    selected: list[Source] = field(default_factory=list)
    duplicates: list[dict[str, str]] = field(default_factory=list)


@dataclass
class AnalysisResult:
    summary: pd.DataFrame
    summary_by_day: pd.DataFrame
    events: pd.DataFrame
    distributions: pd.DataFrame
    quality: pd.DataFrame
    windows: dict[str, list[dict[str, Any]]]
    manifest: dict[str, Any]


def _file_date_and_contract(file_name: str) -> tuple[str | None, str | None]:
    base = Path(file_name).name
    date_match = FILE_DATE_RE.search(base)
    trade_date = date_match.group(1) if date_match else None
    stem = Path(base).stem
    name_part = stem.rsplit("_", 1)[0] if "_" in stem else stem
    match = CONTRACT_RE.fullmatch(name_part.strip())
    return trade_date, match.group(0).upper() if match else None


def _infer_source_date(path: Path, file_name: str, parsed_date: str | None) -> str | None:
    if parsed_date:
        return parsed_date
    # A few historical exports have a suffix such as ``_202603031.csv``.
    # The containing day directory/archive is the reliable date for those rows.
    parts = (*path.parts, *Path(file_name).parts)
    for part in reversed(parts):
        if re.fullmatch(r"\d{8}", part):
            return part
    if re.fullmatch(r"\d{8}", path.stem):
        return path.stem
    return None


def _contract_forms(value: object) -> set[str]:
    text = str(value or "").strip().upper()
    if not text:
        return set()
    forms = {text}
    try:
        forms.add(normalize_contract_code(text))
    except (TypeError, ValueError):
        pass
    return forms


def _symbol(value: object) -> str:
    text = str(value or "").strip().upper()
    match = CONTRACT_RE.fullmatch(text)
    if match:
        return match.group(1).upper()
    prefix = re.match(r"^[A-Z]+", text)
    return prefix.group(0) if prefix else text


def _is_continuous_name(file_name: str) -> bool:
    return any(keyword in Path(file_name).stem for keyword in CONTINUOUS_KEYWORDS)


def _source_candidates(path: Path) -> Iterator[Source]:
    if path.is_file() and path.suffix.lower() == ".csv":
        if _is_continuous_name(path.name):
            return
        date, contract = _file_date_and_contract(path.name)
        date = _infer_source_date(path, path.name, date)
        yield Source(path, None, path.name, date, contract, 0, "csv")
        return
    if path.is_file() and path.suffix.lower() == ".zip":
        try:
            with zipfile.ZipFile(path) as archive:
                for info in archive.infolist():
                    if info.is_dir() or not info.filename.lower().endswith(".csv"):
                        continue
                    date, contract = _file_date_and_contract(info.filename)
                    date = _infer_source_date(path, info.filename, date)
                    if _is_continuous_name(info.filename):
                        continue
                    priority = 1 if path.stem == (date or "") else 2
                    yield Source(path, info.filename, Path(info.filename).name, date, contract, priority, "zip")
        except (OSError, zipfile.BadZipFile):
            date = _infer_source_date(path, path.name, None)
            yield Source(path, None, path.name, date, None, 2, "zip")


def discover_sources(input_expr: str | Path) -> SourceDiscovery:
    """Find sources and de-duplicate contract/date pairs.

    Direct CSVs win over daily ZIP members; daily ZIPs win over other archives.
    The selected list is deterministic so a rerun over the same tree is stable.
    """
    raw_paths: list[Path] = []
    path = Path(input_expr)
    if path.is_file():
        raw_paths = [path]
    elif path.is_dir():
        raw_paths = sorted(
            candidate
            for candidate in path.rglob("*")
            if candidate.is_file() and candidate.suffix.lower() in {".csv", ".zip"}
        )
    else:
        parent = path.parent if str(path.parent) not in ("", ".") else Path(".")
        raw_paths = sorted(parent.glob(path.name))

    candidates: list[Source] = []
    for raw_path in raw_paths:
        candidates.extend(_source_candidates(raw_path))
    candidates.sort(key=lambda s: (s.priority, s.trade_date or "", s.contract_hint or "", s.label))

    selected: list[Source] = []
    duplicates: list[dict[str, str]] = []
    seen: dict[tuple[str, str], Source] = {}
    for source in candidates:
        if source.contract_hint and source.trade_date:
            key = (normalize_contract_code(source.contract_hint), source.trade_date)
            previous = seen.get(key)
            if previous is not None:
                duplicates.append(
                    {
                        "source_label": source.label,
                        "trade_date": source.trade_date,
                        "contract": source.contract_hint,
                        "status": "duplicate",
                        "message": f"已选用 {previous.label}",
                    }
                )
                continue
            seen[key] = source
        selected.append(source)
    return SourceDiscovery(selected, duplicates)


def _open_source(source: Source) -> tuple[Callable[[], BinaryIO], Callable[[], None]]:
    if source.member is None:
        return lambda: source.path.open("rb"), lambda: None

    archive = zipfile.ZipFile(source.path)

    def opener() -> BinaryIO:
        return archive.open(source.member, "r")

    def closer() -> None:
        archive.close()

    return opener, closer


def read_source(source: Source) -> pd.DataFrame:
    """Read only fields required by the model and its replay table."""
    use_columns = set(REQUIRED_COLUMNS) | set(OPTIONAL_COLUMNS)
    opener, archive_close = _open_source(source)
    try:
        try:
            handle = opener()
            try:
                df = pd.read_csv(handle, usecols=lambda c: c in use_columns, encoding="utf-8-sig", low_memory=False)
            finally:
                handle.close()
        except UnicodeDecodeError:
            handle = opener()
            try:
                df = pd.read_csv(handle, usecols=lambda c: c in use_columns, encoding="gb18030", low_memory=False)
            finally:
                handle.close()
    finally:
        archive_close()

    missing = [column for column in REQUIRED_COLUMNS if column not in df.columns]
    if missing:
        raise ValueError(f"缺少字段: {', '.join(missing)}")
    for column in OPTIONAL_COLUMNS:
        if column not in df.columns:
            df[column] = 0
    return df


def _parse_clock(value: object) -> tuple[float, bool]:
    try:
        if pd.isna(value):
            return math.nan, False
    except (TypeError, ValueError):
        pass
    text = str(value).strip()
    match = re.fullmatch(r"(\d{1,2}):(\d{2}):(\d{2})(?:\.(\d+))?", text)
    if not match:
        return math.nan, False
    hour, minute, second = (int(match.group(i)) for i in range(1, 4))
    if hour > 23 or minute > 59 or second > 59:
        return math.nan, False
    fraction = (match.group(4) or "")[:3].ljust(3, "0")
    return float((hour * 3600 + minute * 60 + second) * 1000 + int(fraction or 0)), True


def _time_axis(values: Sequence[object], millis: Sequence[object]) -> tuple[np.ndarray, np.ndarray]:
    """Return absolute milliseconds and a validity mask, preserving row order."""
    out = np.full(len(values), np.nan, dtype=float)
    valid = np.zeros(len(values), dtype=bool)
    offset = 0.0
    previous_clock: float | None = None
    for index, (value, ms_value) in enumerate(zip(values, millis, strict=True)):
        clock, ok = _parse_clock(value)
        if not ok:
            previous_clock = None
            continue
        missing_ms = ms_value is None or (isinstance(ms_value, float) and np.isnan(ms_value)) or str(ms_value).strip() == ""
        if missing_ms:
            ms = 0.0
        else:
            try:
                ms = float(ms_value)
            except (TypeError, ValueError):
                previous_clock = None
                continue
            if not np.isfinite(ms) or ms < 0 or ms >= 1000:
                previous_clock = None
                continue
        clock += float(ms)
        if previous_clock is not None and clock < previous_clock and previous_clock >= 18 * 3600 * 1000 and clock < 6 * 3600 * 1000:
            offset += 86_400_000.0
        out[index] = clock + offset
        valid[index] = True
        previous_clock = clock
    return out, valid


def _finite_price(series: pd.Series) -> pd.Series:
    numeric = pd.to_numeric(series, errors="coerce")
    return numeric.notna() & np.isfinite(numeric) & numeric.gt(0) & numeric.lt(PRICE_MAX)


def prepare_group(raw: pd.DataFrame, source: Source, cfg: Config) -> pd.DataFrame:
    """Prepare one instrument while retaining every original row."""
    x = raw.copy()
    x["_row_order"] = np.arange(len(x), dtype=np.int64)
    x["InstrumentID"] = x["InstrumentID"].astype(str).str.strip()
    for column in OPTIONAL_COLUMNS:
        if column not in x.columns:
            x[column] = 0
    for column in ("LastPrice", "Volume", "BidPrice1", "AskPrice1", "BidVolume1", "AskVolume1", "UpdateMillisec"):
        x[column] = pd.to_numeric(x[column], errors="coerce")

    x["ts_ms"], x["valid_time"] = _time_axis(x["UpdateTime"].tolist(), x["UpdateMillisec"].tolist())
    x["valid_volume"] = x["Volume"].notna() & np.isfinite(x["Volume"]) & x["Volume"].ge(0) & np.isclose(x["Volume"], x["Volume"].round())
    x["valid_bbo"] = _finite_price(x["BidPrice1"]) & _finite_price(x["AskPrice1"]) & x["BidPrice1"].le(x["AskPrice1"])
    x["valid_last"] = _finite_price(x["LastPrice"])
    x["mid"] = np.where(x["valid_bbo"], (x["BidPrice1"] + x["AskPrice1"]) / 2.0, np.nan)
    x["spread"] = np.where(x["valid_bbo"], x["AskPrice1"] - x["BidPrice1"], np.nan)
    x["spread_pct"] = np.where(x["mid"].gt(0), x["spread"] / x["mid"], np.nan)

    ts = x["ts_ms"].to_numpy(dtype=float)
    volume = x["Volume"].to_numpy(dtype=float)
    valid_time = x["valid_time"].to_numpy(dtype=bool)
    valid_volume = x["valid_volume"].to_numpy(dtype=bool)
    dt = np.full(len(x), np.nan, dtype=float)
    if len(x) > 1:
        dt[1:] = ts[1:] - ts[:-1]
    delta_volume = np.full(len(x), np.nan, dtype=float)
    if len(x) > 1:
        delta_volume[1:] = volume[1:] - volume[:-1]
    volume_reset = np.zeros(len(x), dtype=bool)
    if len(x) > 1:
        volume_reset[1:] = valid_volume[1:] & valid_volume[:-1] & (delta_volume[1:] < 0)

    starts = np.zeros(len(x), dtype=bool)
    if len(x):
        starts[0] = True
    if len(x) > 1:
        starts[1:] = (
            ~valid_time[1:]
            | ~valid_time[:-1]
            | ~valid_volume[1:]
            | ~valid_volume[:-1]
            | np.isnan(dt[1:])
            | (dt[1:] < 0)
            | (dt[1:] > cfg.session_gap_seconds * 1000)
            | volume_reset[1:]
        )
    segment_id = np.cumsum(starts, dtype=np.int64)
    same_segment = np.zeros(len(x), dtype=bool)
    if len(x) > 1:
        same_segment[1:] = segment_id[1:] == segment_id[:-1]
    valid_delta = same_segment & valid_volume
    delta_volume[~valid_delta] = np.nan

    x["dt_seconds"] = dt / 1000.0
    x["delta_volume"] = delta_volume
    x["volume_reset"] = volume_reset
    x["segment_id"] = segment_id
    x["has_new_trade"] = pd.Series(delta_volume, index=x.index).gt(0)
    valid_dev = x["valid_bbo"] & x["valid_last"] & x["has_new_trade"] & x["mid"].gt(0)
    x["deviation"] = np.where(valid_dev, (x["LastPrice"] - x["mid"]) / x["mid"], np.nan)
    x["abs_deviation"] = pd.Series(x["deviation"], index=x.index).abs()
    x["outside_bbo_down"] = valid_dev & x["LastPrice"].lt(x["BidPrice1"])
    x["outside_bbo_up"] = valid_dev & x["LastPrice"].gt(x["AskPrice1"])

    # Previous valid Mid is reset at every segment and is deliberately before this row.
    previous_mid = np.full(len(x), np.nan, dtype=float)
    last_mid = math.nan
    previous_segment: int | None = None
    mids = x["mid"].to_numpy(dtype=float)
    valid_bbo = x["valid_bbo"].to_numpy(dtype=bool)
    for index, current_segment in enumerate(segment_id):
        if previous_segment != int(current_segment):
            last_mid = math.nan
            previous_segment = int(current_segment)
        previous_mid[index] = last_mid
        if valid_bbo[index] and np.isfinite(mids[index]):
            last_mid = mids[index]
    x["prev_mid"] = previous_mid
    x["source_file"] = source.file_name
    x["source_label"] = source.label
    x["source_type"] = source.source_type
    x["trade_date"] = source.trade_date or ""
    return x


def _merge_candidates(candidates: pd.DataFrame, merge_seconds: float) -> list[dict[str, Any]]:
    if candidates.empty:
        return []
    c = candidates.sort_values(["segment_id", "ts_ms", "_row_order"], kind="stable").copy()
    clusters: list[list[int]] = []
    current: list[int] = []
    previous_segment: int | None = None
    previous_ts = math.nan
    for position, (_, row) in enumerate(c.iterrows()):
        segment = int(row["segment_id"])
        timestamp = float(row["ts_ms"])
        contiguous = (
            current
            and segment == previous_segment
            and np.isfinite(timestamp)
            and np.isfinite(previous_ts)
            and timestamp - previous_ts <= merge_seconds * 1000
            and timestamp - previous_ts >= 0
        )
        if not contiguous:
            if current:
                clusters.append(current)
            current = []
        current.append(position)
        previous_segment = segment
        previous_ts = timestamp
    if current:
        clusters.append(current)

    events: list[dict[str, Any]] = []
    for cluster in clusters:
        # ``itertuples`` renames columns beginning with ``_``; dicts keep the
        # explicit raw-row key used by the event/replay code.
        cluster_rows = [c.iloc[index].to_dict() for index in cluster]
        representative = min(cluster_rows, key=lambda row: (-float(row["abs_deviation"]), int(row["_row_order"])))
        first = cluster_rows[0]
        last = cluster_rows[-1]
        event = dict(representative)
        event.update(
            {
                "event_start_ts_ms": first["ts_ms"],
                "event_end_ts_ms": last["ts_ms"],
                "event_start_row_order": first["_row_order"],
                "event_end_row_order": last["_row_order"],
                "cluster_candidate_rows": len(cluster_rows),
            }
        )
        events.append(event)
    return events


def _first_mid_after(group: pd.DataFrame, position: int, target_ms: float, tolerance_seconds: float) -> tuple[float, float, float] | None:
    segment = int(group.iloc[position]["segment_id"])
    ts = group["ts_ms"].to_numpy(dtype=float)
    mids = group["mid"].to_numpy(dtype=float)
    segments = group["segment_id"].to_numpy(dtype=np.int64)
    valid = np.isfinite(ts) & np.isfinite(mids) & (segments == segment)
    valid[: position + 1] = False
    indices = np.flatnonzero(valid)
    if indices.size == 0:
        return None
    index = int(indices[np.searchsorted(ts[indices], target_ms, side="left")]) if np.searchsorted(ts[indices], target_ms, side="left") < len(indices) else -1
    if index < 0:
        return None
    delay = (ts[index] - target_ms) / 1000.0
    if delay < 0 or delay > tolerance_seconds:
        return None
    return float(mids[index]), float(ts[index]), float(delay)


def _future_path(group: pd.DataFrame, position: int, horizon_seconds: float) -> tuple[np.ndarray, bool, float]:
    event = group.iloc[position]
    segment = int(event["segment_id"])
    start = float(event["ts_ms"])
    end = start + horizon_seconds * 1000
    sub = group.iloc[position + 1 :]
    sub = sub[(sub["segment_id"] == segment) & sub["ts_ms"].notna() & (sub["ts_ms"] <= end) & sub["mid"].notna()]
    if sub.empty:
        return np.array([], dtype=float), False, 0.0
    values = sub["mid"].to_numpy(dtype=float)
    last_ts = float(sub["ts_ms"].max())
    complete = last_ts >= end
    return values, complete, max(0.0, (last_ts - start) / 1000.0)


def _display_time(update_time: object, update_millisec: object) -> str:
    try:
        text = "" if pd.isna(update_time) else str(update_time)
    except (TypeError, ValueError):
        text = str(update_time)
    ms = pd.to_numeric(pd.Series([update_millisec]), errors="coerce").iloc[0]
    if not np.isfinite(ms):
        ms = 0
    return f"{text}.{int(ms):03d}"


def _event_detail(group: pd.DataFrame, event: dict[str, Any], threshold: float, side: str, mode: str, cfg: Config, event_id: str, windows: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    position = int(group.index[group["_row_order"] == event["_row_order"]][0])
    start_position = int(group.index[group["_row_order"] == event["event_start_row_order"]][0])
    end_position = int(group.index[group["_row_order"] == event["event_end_row_order"]][0])
    event_mid = float(event["mid"])
    anchor_mid = float(event["prev_mid"]) if pd.notna(event["prev_mid"]) else math.nan
    fill = event_mid * (1 - threshold) if side == "down" else event_mid * (1 + threshold)
    detail: dict[str, Any] = {
        "event_id": event_id,
        "window_id": f"{event['InstrumentID']}_{event['trade_date']}_{event['_row_order']}",
        "source_file": event["source_file"],
        "source_label": event["source_label"],
        "trade_date": event["trade_date"],
        "TradingDay": event["TradingDay"],
        "InstrumentID": event["InstrumentID"],
        "side": side,
        "mode": mode,
        "threshold_pct": threshold * 100,
        "event_start_time": _display_time(group.iloc[start_position]["UpdateTime"], group.iloc[start_position]["UpdateMillisec"]),
        "event_end_time": _display_time(group.iloc[end_position]["UpdateTime"], group.iloc[end_position]["UpdateMillisec"]),
        "event_time": _display_time(event["UpdateTime"], event["UpdateMillisec"]),
        "event_start_row_order": event["event_start_row_order"],
        "event_end_row_order": event["event_end_row_order"],
        "row_order": event["_row_order"],
        "event_mid": event_mid,
        "anchor_mid": anchor_mid,
        "event_last": float(event["LastPrice"]),
        "bid1": float(event["BidPrice1"]),
        "ask1": float(event["AskPrice1"]),
        "spread": float(event["spread"]),
        "spread_pct": float(event["spread_pct"] * 100),
        "delta_volume": float(event["delta_volume"]),
        "deviation_pct": float(event["deviation"] * 100),
        "abs_deviation_pct": float(event["abs_deviation"] * 100),
        "mid_move_pct": ((event_mid - anchor_mid) / anchor_mid * 100) if np.isfinite(anchor_mid) and anchor_mid else math.nan,
        "sim_fill_price": fill,
        "cluster_candidate_rows": int(event["cluster_candidate_rows"]),
    }

    if np.isfinite(anchor_mid):
        denominator = anchor_mid - fill if side == "down" else fill - anchor_mid
    else:
        denominator = math.nan
    for horizon in cfg.horizons:
        sample = _first_mid_after(group, position, float(event["ts_ms"]) + horizon * 1000, cfg.sample_delay_tolerance)
        suffix = f"{horizon}s"
        if sample is None:
            future_mid, future_ts, delay, sample_time = math.nan, math.nan, math.nan, ""
            recovery = math.nan
        else:
            future_mid, future_ts, delay = sample
            sample_rows = group.loc[
                (group["segment_id"] == event["segment_id"])
                & (group["ts_ms"] == future_ts)
                & (group.index > position)
            ]
            sample_time = _display_time(sample_rows.iloc[0]["UpdateTime"], sample_rows.iloc[0]["UpdateMillisec"]) if not sample_rows.empty else ""
            recovery = ((future_mid - fill) / denominator if side == "down" else (fill - future_mid) / denominator) if denominator > 0 else math.nan
        detail[f"mid_after_{suffix}"] = future_mid
        detail[f"sample_ts_after_{suffix}"] = future_ts
        detail[f"sample_time_after_{suffix}"] = sample_time
        detail[f"sample_delay_after_{suffix}"] = delay
        detail[f"recovery_ratio_{suffix}"] = recovery
        for level in (50, 80, 100):
            detail[f"recovery{level}_{suffix}"] = bool(recovery >= level / 100) if np.isfinite(recovery) else None

    path, complete, covered = _future_path(group, position, cfg.mfe_mae_horizon)
    if path.size and fill > 0:
        if side == "down":
            mfe = max(0.0, (float(path.max()) - fill) / fill)
            mae = max(0.0, (fill - float(path.min())) / fill)
        else:
            mfe = max(0.0, (fill - float(path.min())) / fill)
            mae = max(0.0, (float(path.max()) - fill) / fill)
    else:
        mfe = mae = math.nan
    detail[f"mfe_{cfg.mfe_mae_horizon}s_pct"] = mfe * 100 if np.isfinite(mfe) else math.nan
    detail[f"mae_{cfg.mfe_mae_horizon}s_pct"] = mae * 100 if np.isfinite(mae) else math.nan
    detail["mfe_mae_window_complete"] = bool(complete)
    detail["mfe_mae_covered_seconds"] = covered

    window_id = str(detail["window_id"])
    if window_id not in windows:
        start = float(event["ts_ms"]) - cfg.window_before_seconds * 1000
        end = float(event["ts_ms"]) + cfg.mfe_mae_horizon * 1000
        window = group[(group["segment_id"] == event["segment_id"]) & group["ts_ms"].between(start, end)].copy()
        rows = []
        for _, row in window.iterrows():
            rows.append(
                {
                    "row_order": int(row["_row_order"]),
                    "time": _display_time(row["UpdateTime"], row["UpdateMillisec"]),
                    "ts_seconds_from_event": (float(row["ts_ms"]) - float(event["ts_ms"])) / 1000.0,
                    "last_price": row["LastPrice"],
                    "mid": row["mid"],
                    "bid1": row["BidPrice1"],
                    "ask1": row["AskPrice1"],
                    "volume": row["Volume"],
                    "is_representative": int(row["_row_order"]) == int(event["_row_order"]),
                }
            )
        windows[window_id] = rows
    return detail


def _metric_summary(events: pd.DataFrame, cfg: Config) -> dict[str, Any]:
    result: dict[str, Any] = {
        "independent_events": int(len(events)),
        "mfe_mae_complete_n": int(events["mfe_mae_window_complete"].fillna(False).astype(bool).sum()) if not events.empty else 0,
    }
    for horizon in cfg.horizons:
        suffix = f"{horizon}s"
        values = pd.to_numeric(events.get(f"recovery_ratio_{suffix}"), errors="coerce") if not events.empty else pd.Series(dtype=float)
        valid = values.dropna()
        result[f"recovery_valid_n_{suffix}"] = int(len(valid))
        result[f"recovery_missing_n_{suffix}"] = int(len(events) - len(valid))
        for level in (50, 80, 100):
            flags = events.get(f"recovery{level}_{suffix}", pd.Series(dtype=object))
            flags = pd.Series(flags).dropna()
            result[f"recovery{level}_{suffix}_rate"] = float(flags.astype(bool).mean()) if not flags.empty else math.nan
            result[f"recovery{level}_{suffix}_n"] = int(len(flags))
        result[f"recovery_ratio_{suffix}_median"] = float(valid.median()) if not valid.empty else math.nan

    complete = events.loc[events["mfe_mae_window_complete"].fillna(False).astype(bool)] if not events.empty else events
    mfe = pd.to_numeric(complete.get(f"mfe_{cfg.mfe_mae_horizon}s_pct"), errors="coerce").dropna() if not complete.empty else pd.Series(dtype=float)
    mae = pd.to_numeric(complete.get(f"mae_{cfg.mfe_mae_horizon}s_pct"), errors="coerce").dropna() if not complete.empty else pd.Series(dtype=float)
    result["mfe_valid_n"] = int(len(mfe))
    result["mae_valid_n"] = int(len(mae))
    result["mfe_mean_pct"] = float(mfe.mean()) if not mfe.empty else math.nan
    result["mfe_median_pct"] = float(mfe.median()) if not mfe.empty else math.nan
    result["mae_mean_pct"] = float(mae.mean()) if not mae.empty else math.nan
    result["mae_median_pct"] = float(mae.median()) if not mae.empty else math.nan
    result["mae_p95_pct"] = float(mae.quantile(0.95)) if not mae.empty else math.nan
    return result


def _distribution(values: Sequence[float], scope: str, contract: str, trade_date: str = "") -> dict[str, Any]:
    numeric = np.asarray(values, dtype=float)
    numeric = numeric[np.isfinite(numeric)]
    row: dict[str, Any] = {"scope": scope, "InstrumentID": contract, "trade_date": trade_date, "new_trade_records": int(numeric.size)}
    if numeric.size:
        for quantile, label in ((0.9, "q90_pct"), (0.95, "q95_pct"), (0.99, "q99_pct"), (0.995, "q99_5_pct"), (0.999, "q99_9_pct"), (0.9999, "q99_99_pct")):
            row[label] = float(np.quantile(numeric, quantile) * 100)
        row["max_pct"] = float(numeric.max() * 100)
    else:
        for label in ("q90_pct", "q95_pct", "q99_pct", "q99_5_pct", "q99_9_pct", "q99_99_pct", "max_pct"):
            row[label] = math.nan
    return row


def _matches_filters(instrument: str, symbols: set[str], contracts: set[str]) -> bool:
    if symbols and _symbol(instrument) not in symbols:
        return False
    if contracts and not (_contract_forms(instrument) & contracts):
        return False
    return True


def analyze_sources(discovery: SourceDiscovery, cfg: Config, symbols: Iterable[str] = (), contracts: Iterable[str] = (), start_date: str | None = None, end_date: str | None = None) -> AnalysisResult:
    symbol_filter = {str(value).strip().upper() for value in symbols if str(value).strip()}
    contract_filter = set().union(*(_contract_forms(value) for value in contracts if str(value).strip())) if contracts else set()
    all_events: list[dict[str, Any]] = []
    day_rows: list[dict[str, Any]] = []
    quality_rows: list[dict[str, Any]] = list(discovery.duplicates)
    windows: dict[str, list[dict[str, Any]]] = {}
    distributions: list[dict[str, Any]] = []
    # ponytail: retain only deviation scalars for exact contract-wide quantiles;
    # use a streaming quantile sketch if a multi-year full-market run outgrows memory.
    values_by_contract: dict[str, list[np.ndarray]] = defaultdict(list)
    processed_sources: list[str] = []
    error_count = 0
    valid_days: set[tuple[str, str]] = set()
    event_counter = 0

    for source in discovery.selected:
        if source.trade_date and (start_date and source.trade_date < start_date or end_date and source.trade_date > end_date):
            continue
        if source.contract_hint and not _matches_filters(source.contract_hint, symbol_filter, contract_filter):
            continue
        try:
            raw = read_source(source)
        except Exception as exc:  # one bad file must not erase the other results
            error_count += 1
            quality_rows.append({"source_label": source.label, "source_file": source.file_name, "trade_date": source.trade_date or "", "status": "error", "message": str(exc)})
            continue
        processed_sources.append(source.label)
        if raw.empty:
            quality_rows.append(
                {
                    "source_label": source.label,
                    "source_file": source.file_name,
                    "source_type": source.source_type,
                    "trade_date": source.trade_date or "",
                    "status": "empty",
                    "message": "文件没有数据行",
                }
            )
            continue
        for instrument, raw_group in raw.groupby("InstrumentID", sort=False, dropna=False):
            instrument = str(instrument).strip()
            contract_info = parse_contract_file(source.file_name, instrument)
            if contract_info.parse_status != "ok":
                quality_rows.append(
                    {
                        "source_label": source.label,
                        "source_file": source.file_name,
                        "source_type": source.source_type,
                        "trade_date": source.trade_date or "",
                        "InstrumentID": instrument,
                        "status": contract_info.parse_status,
                        "message": "沿用现有合约规则跳过",
                    }
                )
                continue
            if not instrument or not _matches_filters(instrument, symbol_filter, contract_filter):
                continue
            group = prepare_group(raw_group.reset_index(drop=True), source, cfg)
            date = source.trade_date or str(group["TradingDay"].iloc[0])
            if start_date and date < start_date or end_date and date > end_date:
                continue
            group["trade_date"] = date
            deviation_values = pd.to_numeric(group.loc[group["deviation"].notna(), "abs_deviation"], errors="coerce").dropna().to_numpy(dtype=float)
            if deviation_values.size:
                valid_days.add((instrument, date))
            distributions.append(_distribution(deviation_values, "contract_day", instrument, date))
            values_by_contract[instrument].append(deviation_values)

            quality_rows.append(
                {
                    "source_label": source.label,
                    "source_file": source.file_name,
                    "source_type": source.source_type,
                    "trade_date": date,
                    "InstrumentID": instrument,
                    "rows": len(group),
                    "valid_time_rows": int(group["valid_time"].sum()),
                    "valid_volume_rows": int(group["valid_volume"].sum()),
                    "valid_bbo_rows": int(group["valid_bbo"].sum()),
                    "invalid_time_rows": int((~group["valid_time"]).sum()),
                    "invalid_volume_rows": int((~group["valid_volume"]).sum()),
                    "invalid_bbo_rows": int((~group["valid_bbo"]).sum()),
                    "invalid_last_rows": int((~group["valid_last"]).sum()),
                    "new_trade_records": int(group["has_new_trade"].sum()),
                    "deviation_records": int(group["deviation"].notna().sum()),
                    "volume_reset_rows": int(group["volume_reset"].sum()),
                    "time_break_rows": int((group["segment_id"].diff().fillna(0) > 0).sum()),
                    "status": "ok",
                    "message": "",
                }
            )

            for threshold in cfg.thresholds:
                for side in ("down", "up"):
                    for mode in ("raw", "strict"):
                        if side == "down":
                            mask = group["deviation"].notna() & group["deviation"].le(-threshold)
                            if mode == "strict":
                                mask &= group["outside_bbo_down"]
                        else:
                            mask = group["deviation"].notna() & group["deviation"].ge(threshold)
                            if mode == "strict":
                                mask &= group["outside_bbo_up"]
                        candidates = group.loc[mask]
                        event_dicts = _merge_candidates(candidates, cfg.event_merge_seconds)
                        details = []
                        for event in event_dicts:
                            event_counter += 1
                            event_id = f"E{event_counter:07d}"
                            details.append(_event_detail(group, event, threshold, side, mode, cfg, event_id, windows))
                        all_events.extend(details)
                        metrics = _metric_summary(pd.DataFrame(details), cfg)
                        day_rows.append(
                            {
                                "trade_date": date,
                                "InstrumentID": instrument,
                                "side": side,
                                "mode": mode,
                                "threshold_pct": threshold * 100,
                                "effective_day": int(deviation_values.size > 0),
                                "deviation_records": int(deviation_values.size),
                                "candidate_rows": int(len(candidates)),
                                **metrics,
                            }
                        )

    for instrument, arrays in values_by_contract.items():
        nonempty = [array for array in arrays if array.size]
        combined = np.concatenate(nonempty) if nonempty else np.array([], dtype=float)
        distributions.append(_distribution(combined, "contract_all", instrument))

    events_df = pd.DataFrame(all_events)
    day_df = pd.DataFrame(day_rows)
    summary_df = _aggregate_summary(day_df, events_df, cfg)
    distributions_df = pd.DataFrame(distributions)
    quality_df = pd.DataFrame(quality_rows)
    manifest = {
        "model_version": MODEL_VERSION,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "selected_source_count": len(processed_sources),
        "discovered_source_count": len(discovery.selected),
        "duplicate_source_count": len(discovery.duplicates),
        "error_source_count": error_count,
        "valid_contract_days": len(valid_days),
        "event_count": len(events_df),
        "processed_sources": processed_sources,
        "duplicate_sources": discovery.duplicates,
        "status": "failed" if not discovery.selected or (error_count and not processed_sources) else "incomplete" if error_count else "ok",
        "parameters": {
            "thresholds_pct": [value * 100 for value in cfg.thresholds],
            "horizons_seconds": list(cfg.horizons),
            "event_merge_seconds": cfg.event_merge_seconds,
            "session_gap_seconds": cfg.session_gap_seconds,
            "sample_delay_tolerance_seconds": cfg.sample_delay_tolerance,
            "mfe_mae_horizon_seconds": cfg.mfe_mae_horizon,
        },
    }
    return AnalysisResult(summary_df, day_df, events_df, distributions_df, quality_df, windows, manifest)


def _aggregate_summary(day_df: pd.DataFrame, events_df: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    keys = ["InstrumentID", "side", "mode", "threshold_pct"]
    if day_df.empty:
        return pd.DataFrame(columns=keys + ["active_days", "candidate_rows", "independent_events", "events_per_day"])
    rows: list[dict[str, Any]] = []
    for key, days in day_df.groupby(keys, dropna=False, sort=True):
        key_values = key if isinstance(key, tuple) else (key,)
        row = dict(zip(keys, key_values, strict=True))
        effective_days = days.loc[days["effective_day"].astype(bool), "trade_date"].astype(str).nunique()
        row.update(
            {
                "active_days": int(effective_days),
                "candidate_rows": int(days["candidate_rows"].sum()),
                "deviation_records": int(days["deviation_records"].sum()) if len(days) else 0,
            }
        )
        matching = events_df
        if not events_df.empty:
            matching = events_df.loc[
                (events_df["InstrumentID"] == key_values[0])
                & (events_df["side"] == key_values[1])
                & (events_df["mode"] == key_values[2])
                & (events_df["threshold_pct"] == key_values[3])
            ]
        row.update(_metric_summary(matching, cfg))
        row["events_per_day"] = row["independent_events"] / effective_days if effective_days else math.nan
        rows.append(row)
    return pd.DataFrame(rows)


def csv_columns(cfg: Config) -> dict[str, list[str]]:
    summary = ["InstrumentID", "side", "mode", "threshold_pct", "active_days", "candidate_rows", "deviation_records", "independent_events", "events_per_day", "mfe_mae_complete_n", "mfe_valid_n", "mae_valid_n", "mfe_mean_pct", "mfe_median_pct", "mae_mean_pct", "mae_median_pct", "mae_p95_pct"]
    for horizon in cfg.horizons:
        suffix = f"{horizon}s"
        summary.extend([f"recovery_valid_n_{suffix}", f"recovery_missing_n_{suffix}", f"recovery_ratio_{suffix}_median"])
        summary.extend(f"recovery{level}_{suffix}_{part}" for level in (50, 80, 100) for part in ("rate", "n"))
    event = ["event_id", "window_id", "source_file", "source_label", "trade_date", "TradingDay", "InstrumentID", "side", "mode", "threshold_pct", "event_start_time", "event_end_time", "event_time", "event_start_row_order", "event_end_row_order", "row_order", "event_mid", "anchor_mid", "event_last", "bid1", "ask1", "spread", "spread_pct", "delta_volume", "deviation_pct", "abs_deviation_pct", "mid_move_pct", "sim_fill_price", "cluster_candidate_rows", "mfe_mae_window_complete", "mfe_mae_covered_seconds", f"mfe_{cfg.mfe_mae_horizon}s_pct", f"mae_{cfg.mfe_mae_horizon}s_pct"]
    for horizon in cfg.horizons:
        suffix = f"{horizon}s"
        event.extend([f"mid_after_{suffix}", f"sample_ts_after_{suffix}", f"sample_time_after_{suffix}", f"sample_delay_after_{suffix}", f"recovery_ratio_{suffix}"])
        event.extend(f"recovery{level}_{suffix}" for level in (50, 80, 100))
    day = ["trade_date", "InstrumentID", "side", "mode", "threshold_pct", "effective_day", "deviation_records", "candidate_rows", "independent_events", "mfe_mae_complete_n", "mfe_valid_n", "mae_valid_n"]
    for horizon in cfg.horizons:
        suffix = f"{horizon}s"
        day.extend([f"recovery_valid_n_{suffix}", f"recovery_missing_n_{suffix}", f"recovery_ratio_{suffix}_median"])
        day.extend(f"recovery{level}_{suffix}_{part}" for level in (50, 80, 100) for part in ("rate", "n"))
    day.extend(["mfe_mean_pct", "mfe_median_pct", "mae_mean_pct", "mae_median_pct", "mae_p95_pct"])
    distribution = ["scope", "InstrumentID", "trade_date", "new_trade_records", "q90_pct", "q95_pct", "q99_pct", "q99_5_pct", "q99_9_pct", "q99_99_pct", "max_pct"]
    quality = ["source_label", "source_file", "source_type", "trade_date", "InstrumentID", "rows", "valid_time_rows", "invalid_time_rows", "valid_volume_rows", "invalid_volume_rows", "valid_bbo_rows", "invalid_bbo_rows", "invalid_last_rows", "new_trade_records", "deviation_records", "volume_reset_rows", "time_break_rows", "status", "message"]
    return {"summary": summary, "event": event, "day": day, "distribution": distribution, "quality": quality}


def write_outputs(result: AnalysisResult, output_dir: str | Path, cfg: Config, run_args: dict[str, Any] | None = None) -> Path:
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    columns = csv_columns(cfg)
    for name, frame, frame_columns in (
        ("threshold_summary.csv", result.summary, columns["summary"]),
        ("threshold_summary_by_day.csv", result.summary_by_day, columns["day"]),
        ("event_details.csv", result.events, columns["event"]),
        ("deviation_distribution.csv", result.distributions, columns["distribution"]),
        ("data_quality.csv", result.quality, columns["quality"]),
    ):
        frame.reindex(columns=frame_columns).to_csv(output / name, index=False, encoding="utf-8-sig")
    manifest = dict(result.manifest)
    if run_args:
        manifest["arguments"] = run_args
    (output / "run_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    from src.mid_analyzer_report import render_report_html

    (output / "analysis_report.html").write_text(
        render_report_html(result.summary, result.events, result.quality, result.windows, manifest),
        encoding="utf-8",
    )
    return output
