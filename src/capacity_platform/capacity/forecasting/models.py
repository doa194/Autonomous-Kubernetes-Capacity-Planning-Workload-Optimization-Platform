"""Deterministic forecast model portfolio (S14 companion §4.1).

All models are small, statistical and fully deterministic: the same training array gives
the same prediction, with no random seeds. Hyperparameter grids are finite and fixed so
fitting cost is bounded. Training arrays use NaN for missing buckets; models skip NaN
rather than reading it as zero.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

# Fixed, versioned hyperparameter grids. Changing them is a model registry revision.
SES_ALPHAS = (0.2, 0.4, 0.6, 0.8)
HOLT_GRID = ((0.3, 0.1), (0.5, 0.1), (0.5, 0.3))
HOLT_DAMPING = 0.9
MODEL_REGISTRY_REVISION = "s14-models-v1"


@dataclass
class FittedModel:
    family: str
    params: dict[str, float | int] = field(default_factory=dict)
    state: dict[str, float] = field(default_factory=dict)

    def predict(self, steps: int) -> np.ndarray:
        return _PREDICTORS[self.family](self, steps)


def _valid(train: np.ndarray) -> np.ndarray:
    return train[~np.isnan(train)]


# ---- persistence ---------------------------------------------------------


def fit_persistence(train: np.ndarray) -> FittedModel | None:
    valid = _valid(train)
    if valid.size == 0:
        return None
    return FittedModel("persistence", {}, {"level": float(valid[-1])})


def _predict_level(model: FittedModel, steps: int) -> np.ndarray:
    return np.full(steps, model.state["level"])


# ---- rolling robust median ----------------------------------------------


def fit_rolling_median(train: np.ndarray, window: int) -> FittedModel | None:
    valid = _valid(train)
    if valid.size < max(3, window // 2):
        return None
    return FittedModel("rolling_median", {"window": window}, {"level": float(np.median(valid[-window:]))})


# ---- simple exponential smoothing (level only) --------------------------


def _ses_filter(valid: np.ndarray, alpha: float) -> tuple[float, float]:
    level = float(valid[0])
    sse = 0.0
    for value in valid[1:]:
        sse += (value - level) ** 2
        level = alpha * value + (1 - alpha) * level
    return level, sse


def fit_ses(train: np.ndarray) -> FittedModel | None:
    valid = _valid(train)
    if valid.size < 6:
        return None
    best = None
    for alpha in SES_ALPHAS:  # finite grid, selected on training one-step SSE only
        level, sse = _ses_filter(valid, alpha)
        if best is None or sse < best[2] - 1e-12:
            best = (alpha, level, sse)
    assert best is not None
    return FittedModel("ses", {"alpha": best[0]}, {"level": best[1]})


# ---- damped additive trend (Holt) ---------------------------------------


def _holt_filter(valid: np.ndarray, alpha: float, beta: float) -> tuple[float, float, float]:
    level, trend = float(valid[0]), float(valid[1] - valid[0])
    sse = 0.0
    for value in valid[2:]:
        forecast = level + HOLT_DAMPING * trend
        sse += (value - forecast) ** 2
        new_level = alpha * value + (1 - alpha) * forecast
        trend = beta * (new_level - level) + (1 - beta) * HOLT_DAMPING * trend
        level = new_level
    return level, trend, sse


def fit_holt_damped(train: np.ndarray) -> FittedModel | None:
    valid = _valid(train)
    if valid.size < 12:
        return None
    best = None
    for alpha, beta in HOLT_GRID:
        level, trend, sse = _holt_filter(valid, alpha, beta)
        if best is None or sse < best[4] - 1e-12:
            best = (alpha, beta, level, trend, sse)
    assert best is not None
    return FittedModel(
        "holt_damped",
        {"alpha": best[0], "beta": best[1], "phi": HOLT_DAMPING},
        {"level": best[2], "trend": best[3]},
    )


def _predict_holt(model: FittedModel, steps: int) -> np.ndarray:
    phi = HOLT_DAMPING
    level, trend = model.state["level"], model.state["trend"]
    out = np.empty(steps)
    damp = 0.0
    for h in range(steps):
        damp += phi ** (h + 1)
        out[h] = level + damp * trend
    # CPU/memory demand cannot be negative; truncation is part of the model definition.
    return np.maximum(out, 0.0)


# ---- seasonal naive ------------------------------------------------------


def fit_seasonal_naive(train: np.ndarray, period: int) -> FittedModel | None:
    """Repeats the corresponding bucket from the previous real cycle. Only offered when
    at least two full observed cycles exist (caller enforces real, non-compressed time)."""
    if train.size < 2 * period:
        return None
    last_cycle = train[-period:]
    if np.isnan(last_cycle).mean() > 0.2:
        return None
    filled = np.where(np.isnan(last_cycle), np.nanmedian(last_cycle), last_cycle)
    return FittedModel("seasonal_naive", {"period": period}, {f"s{i}": float(v) for i, v in enumerate(filled)})


def _predict_seasonal(model: FittedModel, steps: int) -> np.ndarray:
    period = int(model.params["period"])
    cycle = np.array([model.state[f"s{i}"] for i in range(period)])
    return np.array([cycle[h % period] for h in range(steps)])


_PREDICTORS = {
    "persistence": _predict_level,
    "rolling_median": _predict_level,
    "ses": _predict_level,
    "holt_damped": _predict_holt,
    "seasonal_naive": _predict_seasonal,
}

# Simplicity order for deterministic tie-breaking (simpler wins).
SIMPLICITY = {"persistence": 0, "rolling_median": 1, "seasonal_naive": 2, "ses": 3, "holt_damped": 4}


def fit_candidates(train: np.ndarray, *, rolling_window: int, season_period: int | None) -> list[FittedModel]:
    """Fit the full portfolio on a training array. Order is stable for reproducibility."""
    fitted = [
        fit_persistence(train),
        fit_rolling_median(train, rolling_window),
        fit_ses(train),
        fit_holt_damped(train),
    ]
    if season_period:
        fitted.append(fit_seasonal_naive(train, season_period))
    return [m for m in fitted if m is not None]


def fit_family(train: np.ndarray, family: str, *, rolling_window: int, season_period: int | None) -> FittedModel | None:
    for model in fit_candidates(train, rolling_window=rolling_window, season_period=season_period):
        if model.family == family:
            return model
    return None
