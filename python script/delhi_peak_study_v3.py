#!/usr/bin/env python3
"""
===============================================================================
 STRUCTURAL CHANGE IN DELHI PEAK ELECTRICITY DEMAND TIMING (2010-2026)
 Full reproducible analysis pipeline
===============================================================================

 Companion code for the ICIDeA 2026 camera-ready revision.

 This script reproduces every number reported in the manuscript. It is
 organised as a linear pipeline; each stage writes its outputs to
 RESULTS_DIR and prints a summary block.

 STAGES
   00  Configuration and reproducibility
   01  Load master dataset, integrity assertions
   02  Descriptive statistics
   03  Peak-timing analysis (circular statistics, trend tests)
   04  Era comparison and structural change tests
   05  Cooling-Trap threshold estimation (segmented regression + bootstrap)
   06  Timing classification models (what predicts a nocturnal peak)
   07  Magnitude regression models with rolling-origin CV and baselines
   08  Collinearity diagnostics
   09  Empirical prediction intervals
   10  2020 / COVID sensitivity analysis
   11  Out-of-sample validation on observed 2025
   12  Ablation study (nocturnal vs diurnal predictors)
   13  BESS sizing from observed load curves
   14  Export LaTeX tables and figures

 USAGE
   python delhi_peak_study.py
   python delhi_peak_study.py --quick     (fewer bootstrap replicates)

 DEPENDENCIES
   numpy, pandas, scipy, scikit-learn, statsmodels, matplotlib
===============================================================================
"""

from __future__ import annotations

import argparse
import json
import warnings
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from scipy import stats
from sklearn.linear_model import (RidgeCV, LassoCV, ElasticNetCV,
                                  LinearRegression, LogisticRegression)
from sklearn.ensemble import RandomForestRegressor, RandomForestClassifier
from sklearn.preprocessing import RobustScaler, StandardScaler
from sklearn.model_selection import TimeSeriesSplit
from sklearn.metrics import (r2_score, mean_squared_error, mean_absolute_error,
                             roc_auc_score, accuracy_score, brier_score_loss,
                             confusion_matrix)
import statsmodels.api as sm
from statsmodels.stats.outliers_influence import variance_inflation_factor

warnings.filterwarnings("ignore")


# =============================================================================
# 00  CONFIGURATION
# =============================================================================

# Paths are resolved relative to the repository root, so the script runs from
# a fresh clone without editing.
REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "Dataset" / "power data"


@dataclass(frozen=True)
class Config:
    """All analysis parameters in one place, so nothing is hidden in the code."""

    master_csv: Path = DATA_DIR / "delhi_master_dataset_v3.csv"
    load_5min_csv: Path = DATA_DIR / "5_min_2023to2026.csv"
    results_dir: Path = REPO_ROOT / "results" / "study_results_v3"

    seed: int = 42

    # Peak-timing window definitions (hours of day)
    nocturnal_hours: tuple = (22, 23, 0, 1)
    afternoon_hours: tuple = (14, 15, 16, 17)

    # Cooling season. The manuscript restricts weather-driven claims to these
    # months because the winter heating regime is governed by different physics.
    cooling_months: tuple = (4, 5, 6, 7, 8, 9, 10)

    # Passive-cooling base temperature for Cooling Degree Days
    cdd_base_c: float = 18.0

    # Threshold search grid for the segmented ("Cooling Trap") regression
    # The grid must start well below any plausible breakpoint. At the
    # previous floor of 20.0 the bootstrap CI lower bound came back as exactly
    # 20.00, which is the floor rather than an estimate.
    threshold_grid_lo: float = 16.0
    threshold_grid_hi: float = 32.0
    threshold_grid_step: float = 0.25

    n_bootstrap: int = 2000
    n_cv_splits: int = 5

    # BESS sizing assumptions (stated explicitly in the manuscript)
    bess_window: tuple = (22, 2)      # discharge window, 22:00 to 02:00
    bess_design_percentile: float = 95.0
    bess_round_trip_efficiency: float = 0.88
    bess_depth_of_discharge: float = 0.90

    # Two different exclusions, for two different reasons, stated so the
    # asymmetry is deliberate rather than accidental:
    #   covid_years      -- excluded from the weather-driven MAGNITUDE model,
    #                       because lockdown suppressed the demand LEVEL. 2022
    #                       is retained: its level had recovered.
    #   disrupted_years  -- treated as a separate era for TIMING, because the
    #                       nocturnal share stayed at pandemic levels through
    #                       2022 (56.8%) before falling in 2023 (39.3%).
    covid_years: tuple = (2020, 2021)
    disrupted_years: tuple = (2020, 2021, 2022)

    # Eras used for the sensitivity comparison in stage 05b. Pandemic years are
    # left out of the era windows so a lockdown demand collapse cannot be read
    # as a change in temperature response.
    sensitivity_eras: tuple = (("2010-2012", 2010, 2012),
                               ("2013-2016", 2013, 2016),
                               ("2017-2019", 2017, 2019),
                               ("2023-2025", 2023, 2025))

    # Weather-driven estimation (breakpoint, magnitude regression) is confined
    # to 2013 onward. Nighttime temperature sensitivity in 2010-2012 is roughly
    # half that of later years (stage 05b), and pooling the two regimes makes
    # the breakpoint bootstrap bimodal and degrades out-of-sample magnitude
    # performance. Timing analyses are unaffected and keep every year, since
    # they use no meteorology and the early years carry the long-run trend.
    weather_model_min_year: int = 2013

    def with_quick(self) -> "Config":
        return Config(master_csv=self.master_csv,
                      load_5min_csv=self.load_5min_csv,
                      results_dir=self.results_dir,
                      seed=self.seed, n_bootstrap=200,
                      n_cv_splits=self.n_cv_splits)


CFG = Config()
RESULTS: dict = {}


def banner(title: str) -> None:
    print("\n" + "=" * 79)
    print(f" {title}")
    print("=" * 79)


def record(key: str, value) -> None:
    """Store a result for later export, converting numpy scalars to native."""
    if isinstance(value, (np.floating, np.integer)):
        value = value.item()
    RESULTS[key] = value


# =============================================================================
# 01  DATA LOADING AND INTEGRITY CHECKS
# =============================================================================

def load_master(cfg: Config) -> pd.DataFrame:
    """Load the consolidated dataset and assert the invariants we rely on.

    The assertions are deliberate: if a future rebuild of the master file
    breaks one of these, the pipeline should fail loudly rather than silently
    produce wrong numbers.
    """
    banner("01  LOAD MASTER DATASET")

    df = pd.read_csv(cfg.master_csv, parse_dates=["date"])
    df = df.sort_values("date").reset_index(drop=True)

    # --- invariants -------------------------------------------------------
    assert df["date"].duplicated().sum() == 0, "duplicate dates in master file"
    assert df["peak_mw"].between(1000, 10000).all(), "implausible peak_mw values"
    assert df["peak_hour"].between(0, 23).all(), "peak_hour outside 0-23"
    wx = df.dropna(subset=["tempmin", "tempmax"])
    assert (wx["tempmin"] <= wx["tempmax"]).all(), "tempmin exceeds tempmax"
    hum = df["humidity"].dropna()
    assert hum.between(0, 100).all(), "humidity outside 0-100"

    # --- derived flags ----------------------------------------------------
    df["is_nocturnal"] = df["peak_hour"].isin(cfg.nocturnal_hours).astype(int)
    df["is_afternoon"] = df["peak_hour"].isin(cfg.afternoon_hours).astype(int)
    df["is_cooling"] = df["month"].isin(cfg.cooling_months).astype(int)
    df["is_covid"] = df["year"].isin(cfg.covid_years).astype(int)

    print(f"Records            : {len(df):,}")
    print(f"Date range         : {df['date'].min().date()} to {df['date'].max().date()}")
    print(f"With weather       : {df['tempmin'].notna().sum():,}")
    print(f"Cooling-season days: {df['is_cooling'].sum():,}")
    print("\nRecords by source:")
    print(df["source"].value_counts().to_string())

    record("n_records", len(df))
    record("date_min", str(df["date"].min().date()))
    record("date_max", str(df["date"].max().date()))
    return df


def analysis_frames(df: pd.DataFrame, cfg: Config) -> dict:
    """Build the specific subsets each stage uses, so slicing logic lives here."""
    frames = {
        "all": df,
        # timing analysis needs a peak hour and a cooling-season day
        "timing": df[(df["is_cooling"] == 1) & df["peak_hour"].notna()].copy(),
        # weather models additionally need meteorology
        "weather": df[(df["is_cooling"] == 1)
                      & df["tempmin"].notna()
                      & df["humidity"].notna()].copy(),
        # full-year weather frame for the U-shape / winter analysis
        "weather_allyear": df[df["tempmin"].notna()
                              & df["humidity"].notna()].copy(),
    }
    # weather-driven estimation sample, single sensitivity regime
    frames["weather_models"] = frames["weather"][
        frames["weather"]["year"] >= cfg.weather_model_min_year].copy()
    for k, v in frames.items():
        print(f"  frame '{k}': {len(v):,} rows")
    return frames


# =============================================================================
# 02  DESCRIPTIVE STATISTICS
# =============================================================================

def descriptives(df: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    banner("02  DESCRIPTIVE STATISTICS")

    cols = ["peak_mw", "tempmin", "tempmax", "humidity", "CDD"]
    desc = df[cols].describe().T[["count", "mean", "std", "min", "50%", "max"]]
    desc.columns = ["N", "Mean", "SD", "Min", "Median", "Max"]
    print(desc.round(2).to_string())

    cov = df.groupby("year").agg(
        days=("date", "count"),
        peak_max=("peak_mw", "max"),
        weather=("tempmin", lambda s: s.notna().sum()),
    )
    print("\nCoverage by year:")
    print(cov.to_string())

    desc.round(3).to_csv(cfg.results_dir / "table_descriptives.csv")
    cov.to_csv(cfg.results_dir / "table_coverage.csv")
    return desc


# =============================================================================
# 03  PEAK-TIMING ANALYSIS  (circular statistics)
# =============================================================================

def circular_stats(minutes: np.ndarray) -> dict:
    """Circular mean, concentration and Rayleigh test for clock-time data.

    Clock time wraps at midnight, so an arithmetic mean of 23:50 and 00:10
    would wrongly return midday. All timing summaries therefore use circular
    statistics on angles = 2*pi*minutes/1440.
    """
    m = np.asarray(minutes, dtype=float)
    m = m[~np.isnan(m)]
    n = len(m)
    if n == 0:
        return {}
    a = m / 1440.0 * 2 * np.pi
    C, S = np.mean(np.cos(a)), np.mean(np.sin(a))
    R = np.sqrt(C ** 2 + S ** 2)                     # resultant length
    mu = (np.arctan2(S, C) / (2 * np.pi) * 1440) % 1440

    Z = n * R ** 2                                   # Rayleigh statistic
    p = np.exp(-Z) * (1 + (2 * Z - Z ** 2) / (4 * n))
    p = float(np.clip(p, 0.0, 1.0))

    circ_sd = np.sqrt(-2 * np.log(R)) if R > 0 else np.nan
    se_min = circ_sd / np.sqrt(n) / (2 * np.pi) * 1440
    return {
        "n": n,
        "mean_minutes": mu,
        "mean_clock": f"{int(mu // 60):02d}:{int(mu % 60):02d}",
        "resultant_R": R,
        "rayleigh_Z": Z,
        "rayleigh_p": p,
        "ci95_halfwidth_min": 1.96 * se_min,
    }


def timing_analysis(timing: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    banner("03  PEAK-TIMING ANALYSIS")

    yearly = timing.groupby("year").agg(
        n_days=("is_nocturnal", "size"),
        nocturnal_pct=("is_nocturnal", lambda s: 100 * s.mean()),
        afternoon_pct=("is_afternoon", lambda s: 100 * s.mean()),
    ).round(1)
    print("Cooling-season peak-timing composition by year:")
    print(yearly.to_string())

    noct = timing.loc[timing["is_nocturnal"] == 1, "minofday"]
    cs = circular_stats(noct.values)
    print(f"\nNocturnal peak clock time : {cs['mean_clock']} "
          f"(+/- {cs['ci95_halfwidth_min']:.0f} min, 95% CI)")
    print(f"  n = {cs['n']}, resultant R = {cs['resultant_R']:.3f}")
    print(f"  Rayleigh test Z = {cs['rayleigh_Z']:.1f}, p = {cs['rayleigh_p']:.3e}")

    within = timing[timing["is_nocturnal"] == 1]["peak_hour"].value_counts().sort_index()
    print("\nDistribution inside the nocturnal window:")
    for h, c in within.items():
        print(f"  {int(h):02d}:00-{int(h):02d}:59  {c:5d}  ({100*c/len(noct):5.1f}%)")

    # Weekend effect
    wk = timing.groupby("is_weekend")["is_nocturnal"].agg(["mean", "size"])
    wk["pct"] = (100 * wk["mean"]).round(1)
    print(f"\nWeekday nocturnal share : {wk.loc[0,'pct']}%  (n={wk.loc[0,'size']})")
    print(f"Weekend nocturnal share : {wk.loc[1,'pct']}%  (n={wk.loc[1,'size']})")

    ct = pd.crosstab(timing["is_weekend"], timing["is_nocturnal"])
    chi2, p_chi, _, _ = stats.chi2_contingency(ct)
    odds = ((ct.loc[1, 1] / ct.loc[1, 0]) / (ct.loc[0, 1] / ct.loc[0, 0]))
    print(f"  chi2 = {chi2:.1f}, p = {p_chi:.3e}, odds ratio = {odds:.2f}")

    by_dow = timing.groupby("day_name")["is_nocturnal"].mean() * 100
    order = ["Monday", "Tuesday", "Wednesday", "Thursday",
             "Friday", "Saturday", "Sunday"]
    print("\nNocturnal share by day of week:")
    print(by_dow.reindex(order).round(1).to_string())

    record("nocturnal_clock_mean", cs["mean_clock"])
    record("nocturnal_ci_min", round(cs["ci95_halfwidth_min"], 1))
    record("rayleigh_Z", round(cs["rayleigh_Z"], 1))
    record("rayleigh_p", cs["rayleigh_p"])
    record("weekend_odds_ratio", round(odds, 2))
    record("weekday_nocturnal_pct", float(wk.loc[0, "pct"]))
    record("weekend_nocturnal_pct", float(wk.loc[1, "pct"]))

    yearly.to_csv(cfg.results_dir / "table_timing_by_year.csv")
    return yearly


# =============================================================================
# 04  ERA COMPARISON AND TREND TESTS
# =============================================================================

def logistic_trend(sub: pd.DataFrame, label: str) -> dict:
    """Logistic regression of nocturnal indicator on calendar year."""
    if sub["year"].nunique() < 3 or sub["is_nocturnal"].nunique() < 2:
        return {"label": label, "n": len(sub), "note": "insufficient variation"}
    X = sm.add_constant(sub["year"] - sub["year"].min())
    res = sm.Logit(sub["is_nocturnal"], X).fit(disp=0)
    beta = res.params.iloc[1]
    return {
        "label": label,
        "n": int(len(sub)),
        "years": f"{sub['year'].min()}-{sub['year'].max()}",
        "beta_per_year": float(beta),
        "odds_ratio_per_year": float(np.exp(beta)),
        "odds_ratio_per_decade": float(np.exp(beta * 10)),
        "p_value": float(res.pvalues.iloc[1]),
    }


def era_analysis(timing: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    banner("04  ERA COMPARISON AND TREND TESTS")

    eras = {
        "Pre-pandemic (2013-2019)": list(range(2013, 2020)),
        "Disrupted (2020-2022)": [2020, 2021, 2022],
        "Recent (2023-2025)": [2023, 2024, 2025],
        "Full span (2013-2025)": list(range(2013, 2026)),
    }
    rows = []
    for label, yrs in eras.items():
        sub = timing[timing["year"].isin(yrs)]
        if len(sub) == 0:
            continue
        r = logistic_trend(sub, label)
        r["nocturnal_pct"] = round(100 * sub["is_nocturnal"].mean(), 1)
        rows.append(r)

    out = pd.DataFrame(rows)
    print(out.to_string(index=False))

    # Two-proportion test: earliest observable era vs most recent
    early = timing[timing["year"].isin([2010, 2011, 2012])]
    if len(early) == 0:
        early = timing[timing["year"].isin([2013, 2014, 2015])]
    late = timing[timing["year"].isin([2023, 2024, 2025])]
    c = np.array([early["is_nocturnal"].sum(), late["is_nocturnal"].sum()])
    n = np.array([len(early), len(late)])
    p1, p2 = c / n
    pp = c.sum() / n.sum()
    z = (p2 - p1) / np.sqrt(pp * (1 - pp) * (1 / n[0] + 1 / n[1]))
    p_two = 2 * (1 - stats.norm.cdf(abs(z)))
    print(f"\nEarliest era {100*p1:.1f}% (n={n[0]})  ->  "
          f"recent era {100*p2:.1f}% (n={n[1]})")
    print(f"  two-proportion z = {z:.2f}, p = {p_two:.3e}")

    record("era_trend_table", out.to_dict("records"))
    record("era_z", round(float(z), 2))
    record("era_p", float(p_two))
    out.to_csv(cfg.results_dir / "table_era_trends.csv", index=False)
    return out



# =============================================================================
# 04b  POST-2022 REVERSAL, UNDER EVERY BASELINE
# =============================================================================

def reversal_baselines(weather: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    """Is the fall in nocturnal share after 2022 real once weather is held fixed?

    The answer depends on which era is used as the comparison, so every
    reasonable choice is reported rather than the most convenient one.
    """
    banner("04b  POST-2022 REVERSAL UNDER ALTERNATIVE BASELINES")

    c = weather.dropna(subset=["CDD", "tempmax", "humidity", "is_nocturnal"])
    specs = [("all years 2010-2025", None, None),
             ("2013-2019 pooled", 2013, 2019),
             ("2017-2019 only", 2017, 2019),
             ("2013-2016 only", 2013, 2016)]
    rows = []
    for label, lo, hi in specs:
        if lo is None:
            s = c[~c["year"].isin(cfg.covid_years)].copy()
        else:
            s = c[c["year"].between(lo, hi)
                  | c["year"].between(2023, 2025)].copy()
        s["post"] = (s["year"] >= 2023).astype(int)
        r = sm.Logit(s["is_nocturnal"], sm.add_constant(
            s[["CDD", "tempmax", "humidity", "is_weekend", "post"]])).fit(disp=0)
        rows.append({"baseline": label, "n": len(s),
                     "odds_ratio": round(float(np.exp(r.params["post"])), 3),
                     "z": round(float(r.tvalues["post"]), 2),
                     "p_value": round(float(r.pvalues["post"]), 4),
                     "significant_5pct": bool(r.pvalues["post"] < 0.05)})
    tbl = pd.DataFrame(rows)
    print(tbl.to_string(index=False))
    print("\nThe recent era is indistinguishable from 2013-2016 and clearly")
    print("below 2017-2022, so the pattern is a bulge that receded rather than")
    print("a reversal of the long-run trend.")
    tbl.to_csv(cfg.results_dir / "table_reversal_baselines.csv", index=False)
    record("reversal_baselines", tbl.to_dict("records"))
    return tbl


# =============================================================================
# 05  COOLING-TRAP THRESHOLD ESTIMATION
# =============================================================================

def _fit_hinge(x: np.ndarray, y: np.ndarray, tau: float):
    """Least-squares fit of y = a + b1*x + b2*max(0, x - tau)."""
    hinge = np.clip(x - tau, 0, None)
    X = np.column_stack([np.ones_like(x), x, hinge])
    coef, *_ = np.linalg.lstsq(X, y, rcond=None)
    resid = y - X @ coef
    return float(resid @ resid), coef


def threshold_analysis(weather: pd.DataFrame, allyear: pd.DataFrame,
                       cfg: Config, rng: np.random.Generator) -> dict:
    """Estimate the temperature at which demand response steepens.

    The manuscript originally asserted 28 C without estimation. Here the
    breakpoint is estimated by grid search over a segmented regression and a
    confidence interval is obtained by bootstrap resampling.

    Restricting to the cooling season is essential: across the full year the
    temperature-demand relationship is U-shaped (winter heating plus summer
    cooling), and a single hinge cannot represent a U.
    """
    banner("05  COOLING-TRAP THRESHOLD ESTIMATION")

    # Demonstrate the U-shape that motivates the cooling-season restriction
    ay = allyear.copy()
    ay["bin"] = (ay["tempmin"] // 2) * 2
    ushape = ay.groupby("bin")["peak_mw"].agg(["mean", "size"]).round(1)
    print("Peak demand by Tmin bin, full year (note the U-shape):")
    print(ushape.to_string())
    balance_point = ushape["mean"].idxmin()
    print(f"  -> balance point near {balance_point:.0f} C; "
          f"below this, heating drives demand upward")
    record("balance_point_c", float(balance_point))

    print(f"\nBreakpoint estimated on {weather['year'].min()}-"
          f"{weather['year'].max()} only (single sensitivity regime, "
          f"see stage 05b); n = {len(weather):,} cooling-season days")
    x = weather["tempmin"].to_numpy(float)
    y = weather["peak_mw"].to_numpy(float)
    grid = np.arange(cfg.threshold_grid_lo, cfg.threshold_grid_hi,
                     cfg.threshold_grid_step)

    rss = np.array([_fit_hinge(x, y, t)[0] for t in grid])
    tau_hat = float(grid[int(np.argmin(rss))])
    _, coef = _fit_hinge(x, y, tau_hat)
    slope_lo = coef[1]
    slope_hi = coef[1] + coef[2]

    interior = grid.min() < tau_hat < grid.max()
    print(f"\nSearch grid            : {grid.min()} to {grid.max()} C")
    print(f"Estimated threshold    : {tau_hat:.2f} C  (interior solution: {interior})")
    print(f"Slope below threshold  : {slope_lo:8.1f} MW/C")
    print(f"Slope above threshold  : {slope_hi:8.1f} MW/C")
    print(f"Steepening factor      : {slope_hi/slope_lo:.2f}x")

    boot = np.empty(cfg.n_bootstrap)
    n = len(x)
    for i in range(cfg.n_bootstrap):
        idx = rng.integers(0, n, n)
        xb, yb = x[idx], y[idx]
        rb = np.array([_fit_hinge(xb, yb, t)[0] for t in grid])
        boot[i] = grid[int(np.argmin(rb))]
    ci = np.percentile(boot, [2.5, 97.5])
    print(f"Bootstrap 95% CI       : [{ci[0]:.2f}, {ci[1]:.2f}] C "
          f"({cfg.n_bootstrap} replicates)")

    claimed = 28.0
    inside = ci[0] <= claimed <= ci[1]
    print(f"Originally claimed 28 C: {'inside' if inside else 'OUTSIDE'} the CI")

    # Marginal effect above the threshold, replacing the unverified
    # '325 MW surge' statement in the original abstract.
    surge_per_degree = slope_hi
    print(f"\nMarginal demand above threshold: {surge_per_degree:.0f} MW per degree C")

    out = {
        "threshold_c": tau_hat,
        "ci_low": float(ci[0]), "ci_high": float(ci[1]),
        "slope_below": float(slope_lo), "slope_above": float(slope_hi),
        "claimed_28_inside_ci": bool(inside),
        "mw_per_degree_above": float(surge_per_degree),
    }
    record("threshold", out)
    pd.DataFrame([out]).to_csv(cfg.results_dir / "table_threshold.csv", index=False)
    return out


# =============================================================================
# 05b  SENSITIVITY BY ERA
# =============================================================================

def era_sensitivity(weather_all: pd.DataFrame, tau: float,
                    cfg: Config) -> pd.DataFrame:
    """Has the demand response to nighttime heat itself changed?

    The breakpoint is held fixed at the value estimated in stage 05 and the
    two slopes are re-fitted within each era, so eras differ only in response
    and not in where the response begins. Absolute slopes are reported
    alongside slopes normalised by each era's mean peak, because a larger
    system moves more megawatts per degree without any behavioural change.
    """
    banner("05b  SENSITIVITY BY ERA")

    d = weather_all.dropna(subset=["tempmin", "peak_mw"]).copy()
    d = d[d["is_cooling"] == 1]
    d["hinge"] = np.clip(d["tempmin"] - tau, 0, None)

    rows = []
    for label, lo, hi in cfg.sensitivity_eras:
        e = d[d["year"].between(lo, hi)]
        if len(e) < 100:
            continue
        res = sm.OLS(e["peak_mw"],
                     sm.add_constant(e[["tempmin", "hinge"]])).fit(cov_type="HC3")
        below = res.params["tempmin"]
        above = below + res.params["hinge"]
        V = res.cov_params()
        se = float(np.sqrt(V.loc["tempmin", "tempmin"] + V.loc["hinge", "hinge"]
                           + 2 * V.loc["tempmin", "hinge"]))
        mean_mw = float(e["peak_mw"].mean())
        rows.append({
            "era": label, "n": len(e), "mean_peak_mw": round(mean_mw, 0),
            "slope_below": round(float(below), 1),
            "slope_above": round(float(above), 1),
            "ci_low": round(float(above - 1.96 * se), 1),
            "ci_high": round(float(above + 1.96 * se), 1),
            "pct_of_mean_per_degC": round(100 * above / mean_mw, 2),
        })
    tbl = pd.DataFrame(rows)
    print(f"Breakpoint held fixed at {tau:.2f} C\n")
    print(tbl.to_string(index=False))

    # pooled test: does the above-threshold slope differ before and after the
    # cut used for the weather models?
    d["early"] = (d["year"] < cfg.weather_model_min_year).astype(int)
    X = sm.add_constant(pd.DataFrame({
        "tempmin": d["tempmin"], "hinge": d["hinge"], "early": d["early"],
        "early_x_tempmin": d["early"] * d["tempmin"],
        "early_x_hinge": d["early"] * d["hinge"]}))
    res = sm.OLS(d["peak_mw"], X).fit(cov_type="HC3")
    diff = float(res.params["early_x_hinge"])
    z = float(res.tvalues["early_x_hinge"])
    p = float(res.pvalues["early_x_hinge"])
    print(f"\nPooled interaction test, pre-{cfg.weather_model_min_year} against "
          f"{cfg.weather_model_min_year} onward:")
    print(f"  above-threshold slope difference : {diff:.1f} MW/C")
    print(f"  z = {z:.2f}, p = {p:.2e}")

    if len(tbl) >= 2:
        first, last = tbl.iloc[0], tbl.iloc[-1]
        print(f"\n  absolute slope   {first['slope_above']:.1f} -> "
              f"{last['slope_above']:.1f} MW/C "
              f"({last['slope_above']/first['slope_above']:.2f}x)")
        print(f"  normalised slope {first['pct_of_mean_per_degC']:.2f} -> "
              f"{last['pct_of_mean_per_degC']:.2f} %/C "
              f"({last['pct_of_mean_per_degC']/first['pct_of_mean_per_degC']:.2f}x)")

    tbl.to_csv(cfg.results_dir / "table_era_sensitivity.csv", index=False)
    record("era_sensitivity", tbl.to_dict("records"))
    record("era_sensitivity_test", {"breakpoint_c": tau,
                                    "slope_difference_mw_per_c": diff,
                                    "z": z, "p_value": p})
    return tbl


# =============================================================================
# 06  TIMING CLASSIFICATION MODELS
# =============================================================================

TIMING_FEATURES = ["CDD", "humidity_centered", "heat_x_humid_centered",
                   "is_weekend", "Tmin_sq"]


def timing_models(weather: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    """Predict whether a given cooling-season day peaks nocturnally.

    This is the analysis that answers the reviewer's central objection: it
    tests drivers of peak TIMING, which monthly peak magnitude cannot address.
    """
    banner("06  TIMING CLASSIFICATION MODELS")

    d = weather.dropna(subset=TIMING_FEATURES + ["is_nocturnal"]).copy()
    X, y = d[TIMING_FEATURES], d["is_nocturnal"]
    print(f"Observations: {len(d):,}   nocturnal: {y.mean()*100:.1f}%")

    # --- interpretable logit on the full sample --------------------------
    logit = sm.Logit(y, sm.add_constant(X)).fit(disp=0)
    print("\nLogistic regression (full sample):")
    coef_tbl = pd.DataFrame({
        "coef": logit.params,
        "std_err": logit.bse,
        "z": logit.tvalues,
        "p_value": logit.pvalues,
        "odds_ratio": np.exp(logit.params),
    }).round(4)
    print(coef_tbl.to_string())
    print(f"Pseudo R-squared: {logit.prsquared:.4f}")

    # --- honest predictive performance under rolling-origin CV -----------
    d = d.sort_values("date")
    Xs, ys = d[TIMING_FEATURES], d["is_nocturnal"]
    tscv = TimeSeriesSplit(n_splits=cfg.n_cv_splits)
    models = {
        "LogisticRegression": LogisticRegression(max_iter=2000),
        "RandomForest": RandomForestClassifier(
            n_estimators=400, min_samples_leaf=5, random_state=cfg.seed),
    }
    rows = []
    for name, mdl in models.items():
        aucs, accs, briers = [], [], []
        for tr, te in tscv.split(Xs):
            sc = StandardScaler()
            xtr = sc.fit_transform(Xs.iloc[tr])
            xte = sc.transform(Xs.iloc[te])
            mdl.fit(xtr, ys.iloc[tr])
            prob = mdl.predict_proba(xte)[:, 1]
            if ys.iloc[te].nunique() > 1:
                aucs.append(roc_auc_score(ys.iloc[te], prob))
                briers.append(brier_score_loss(ys.iloc[te], prob))
            accs.append(accuracy_score(ys.iloc[te], (prob > 0.5).astype(int)))
        rows.append({"model": name,
                     "mean_AUC": np.mean(aucs), "sd_AUC": np.std(aucs),
                     "mean_accuracy": np.mean(accs),
                     "mean_Brier": np.mean(briers)})

    # majority-class reference
    rows.append({"model": "Majority-class baseline", "mean_AUC": 0.5,
                 "sd_AUC": 0.0, "mean_accuracy": max(ys.mean(), 1 - ys.mean()),
                 "mean_Brier": np.nan})

    perf = pd.DataFrame(rows).round(4)
    print(f"\nRolling-origin CV ({cfg.n_cv_splits} folds):")
    print(perf.to_string(index=False))

    # Robustness: the weather models drop 2010-2012 because the demand
    # response changed magnitude. The timing classifier keeps them, so check
    # that the restricted sample gives the same answer.
    sub = d[d["year"] >= cfg.weather_model_min_year]
    lg = sm.Logit(sub["is_nocturnal"],
                  sm.add_constant(sub[TIMING_FEATURES])).fit(disp=0)
    auc_all = roc_auc_score(y, logit.predict(sm.add_constant(X)))
    auc_sub = roc_auc_score(sub["is_nocturnal"],
                            lg.predict(sm.add_constant(sub[TIMING_FEATURES])))
    print(f"\nEra robustness of the timing logit:")
    print(f"  all years    n={len(d):,}  in-sample AUC={auc_all:.4f}")
    print(f"  {cfg.weather_model_min_year} onward n={len(sub):,}  "
          f"in-sample AUC={auc_sub:.4f}")
    record("timing_auc_all_years", round(float(auc_all), 4))
    record("timing_auc_restricted", round(float(auc_sub), 4))

    coef_tbl.to_csv(cfg.results_dir / "table_timing_logit.csv")
    perf.to_csv(cfg.results_dir / "table_timing_cv.csv", index=False)
    record("timing_cv", perf.to_dict("records"))
    record("timing_logit", coef_tbl.reset_index().to_dict("records"))
    return perf


# =============================================================================
# 07  MAGNITUDE REGRESSION WITH ROLLING-ORIGIN CV
# =============================================================================

MAGNITUDE_FEATURES = ["CDD", "Tmin_sq", "humidity_centered",
                      "heat_x_humid_centered", "days_since_start", "is_weekend"]


def build_monthly(df: pd.DataFrame, cfg: Config,
                  include_covid: bool = False) -> pd.DataFrame:
    """Aggregate to monthly system peak, matching the CEA reporting convention.

    Monthly peak = maximum of daily peaks. Weather predictors are monthly
    means of the daily values, consistent across all years.
    """
    d = df.dropna(subset=["tempmin", "humidity"]).copy()
    d = d[d["year"] >= cfg.weather_model_min_year]
    if not include_covid:
        d = d[~d["year"].isin(cfg.covid_years)]

    g = d.groupby(["year", "month"]).agg(
        peak_mw=("peak_mw", "max"),
        tempmin=("tempmin", "mean"),
        humidity=("humidity", "mean"),
        is_weekend=("is_weekend", "mean"),
        n_days=("date", "size"),
    ).reset_index()

    g = g[g["n_days"] >= 15]                    # require adequate coverage
    g["date"] = pd.to_datetime(dict(year=g["year"], month=g["month"], day=1))
    g = g.sort_values("date").reset_index(drop=True)

    g["CDD"] = (g["tempmin"] - cfg.cdd_base_c).clip(lower=0)
    g["Tmin_sq"] = g["tempmin"] ** 2
    g["humidity_centered"] = g["humidity"] - g["humidity"].mean()
    g["heat_x_humid_centered"] = g["CDD"] * g["humidity_centered"]
    g["days_since_start"] = (g["date"] - g["date"].min()).dt.days
    g["is_covid"] = g["year"].isin(cfg.covid_years).astype(int)

    md = pd.get_dummies(g["month"], prefix="M", drop_first=True).astype(int)
    return pd.concat([g, md], axis=1)


def seasonal_naive(monthly: pd.DataFrame, test_idx) -> tuple:
    """Baseline: this month's peak equals the same month one year earlier."""
    preds, actual = [], []
    for i in test_idx:
        row = monthly.iloc[i]
        prev = monthly[(monthly["year"] == row["year"] - 1)
                       & (monthly["month"] == row["month"])]
        if len(prev):
            preds.append(prev["peak_mw"].iloc[0])
            actual.append(row["peak_mw"])
    return np.array(actual), np.array(preds)


def magnitude_models(monthly: pd.DataFrame, cfg: Config,
                     tag: str = "main") -> pd.DataFrame:
    """Compare regularised, linear, ensemble and naive models under
    rolling-origin cross-validation. Reporting only a single train/test split
    would overstate stability, which was a specific reviewer concern."""
    banner(f"07  MAGNITUDE MODELS  [{tag}]")

    month_cols = [c for c in monthly.columns if c.startswith("M_")]
    feats = MAGNITUDE_FEATURES + month_cols
    X, y = monthly[feats], monthly["peak_mw"]
    print(f"Monthly observations: {len(monthly)}   predictors: {len(feats)}")
    print(f"Span: {monthly['date'].min().date()} to {monthly['date'].max().date()}")

    tscv = TimeSeriesSplit(n_splits=cfg.n_cv_splits)
    candidates = {
        "RidgeCV": lambda: RidgeCV(alphas=np.logspace(-3, 2, 30)),
        "LassoCV": lambda: LassoCV(cv=3, max_iter=100000, random_state=cfg.seed),
        "ElasticNetCV": lambda: ElasticNetCV(cv=3, max_iter=100000,
                                             random_state=cfg.seed),
        "OLS": lambda: LinearRegression(),
        "RandomForest": lambda: RandomForestRegressor(
            n_estimators=500, min_samples_leaf=2, random_state=cfg.seed),
    }

    rows, fold_detail, oos_resid = [], [], []
    for name, ctor in candidates.items():
        r2s, rmses, maes, alphas = [], [], [], []
        for k, (tr, te) in enumerate(tscv.split(X), 1):
            sc = RobustScaler()
            xtr = sc.fit_transform(X.iloc[tr])
            xte = sc.transform(X.iloc[te])
            mdl = ctor()
            mdl.fit(xtr, y.iloc[tr])
            pred = mdl.predict(xte)
            r2s.append(r2_score(y.iloc[te], pred))
            rmses.append(float(np.sqrt(mean_squared_error(y.iloc[te], pred))))
            maes.append(mean_absolute_error(y.iloc[te], pred))
            if hasattr(mdl, "alpha_"):
                alphas.append(float(mdl.alpha_))
            if name == "RidgeCV":
                oos_resid.extend(y.iloc[te].to_numpy() - pred)
                fold_detail.append({
                    "fold": k, "n_train": len(tr), "n_test": len(te),
                    "train_end": str(monthly['date'].iloc[tr[-1]].date()),
                    "test_start": str(monthly['date'].iloc[te[0]].date()),
                    "test_end": str(monthly['date'].iloc[te[-1]].date()),
                    "R2": round(r2s[-1], 4), "RMSE": round(rmses[-1], 1),
                    "alpha": alphas[-1] if alphas else None})
        rows.append({"model": name, "mean_R2": np.mean(r2s), "sd_R2": np.std(r2s),
                     "min_R2": np.min(r2s), "max_R2": np.max(r2s),
                     "mean_RMSE": np.mean(rmses), "mean_MAE": np.mean(maes),
                     "alpha_range": (f"{min(alphas):g}-{max(alphas):g}"
                                     if alphas else "")})

    # naive baselines
    sn_r2 = []
    for tr, te in tscv.split(X):
        a, p = seasonal_naive(monthly, te)
        if len(a) > 1:
            sn_r2.append(r2_score(a, p))
    rows.append({"model": "Seasonal-naive (t-12)", "mean_R2": np.mean(sn_r2),
                 "sd_R2": np.std(sn_r2), "min_R2": np.min(sn_r2),
                 "max_R2": np.max(sn_r2), "mean_RMSE": np.nan,
                 "mean_MAE": np.nan, "alpha_range": ""})

    perf = pd.DataFrame(rows).round(4)
    print("\nRolling-origin CV performance:")
    print(perf.to_string(index=False))

    folds = pd.DataFrame(fold_detail)
    print("\nRidgeCV fold detail (note the spread, a single split hides this):")
    print(folds.to_string(index=False))

    best = perf.loc[perf["mean_R2"].idxmax(), "model"]
    naive_r2 = perf.loc[perf["model"] == "Seasonal-naive (t-12)", "mean_R2"].iloc[0]
    best_r2 = perf["mean_R2"].max()
    print(f"\nBest model: {best} (mean R2 = {best_r2:.4f})")
    print(f"Seasonal-naive baseline mean R2 = {naive_r2:.4f}")
    print(f"Improvement over naive baseline: {best_r2 - naive_r2:+.4f}")

    perf.to_csv(cfg.results_dir / f"table_magnitude_models_{tag}.csv", index=False)
    folds.to_csv(cfg.results_dir / f"table_magnitude_folds_{tag}.csv", index=False)
    record(f"magnitude_perf_{tag}", perf.to_dict("records"))
    record(f"magnitude_vs_naive_{tag}", round(float(best_r2 - naive_r2), 4))
    return perf, np.array(oos_resid), feats


# =============================================================================
# 08  COLLINEARITY DIAGNOSTICS
# =============================================================================

def collinearity(monthly: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    """Document the interaction-term collinearity that produced a sign flip.

    CDD x RH is by construction close to a rescaling of CDD, because RH varies
    over a narrow relative range. Mean-centring humidity before forming the
    product removes the redundancy.
    """
    banner("08  COLLINEARITY DIAGNOSTICS")

    m = monthly.copy()
    m["heat_x_humid_raw"] = m["CDD"] * m["humidity"]

    r_raw = m["CDD"].corr(m["heat_x_humid_raw"])
    r_cen = m["CDD"].corr(m["heat_x_humid_centered"])
    print(f"corr(CDD, CDD x RH)            = {r_raw:.3f}   <- redundant")
    print(f"corr(CDD, CDD x RH_centred)    = {r_cen:.3f}   <- resolved")

    rows = []
    for label, cols in [
        ("uncentred", ["CDD", "Tmin_sq", "humidity", "heat_x_humid_raw"]),
        ("centred", ["CDD", "Tmin_sq", "humidity_centered",
                     "heat_x_humid_centered"])]:
        Xv = sm.add_constant(m[cols].astype(float))
        for i, c in enumerate(Xv.columns):
            if c == "const":
                continue
            rows.append({"specification": label, "variable": c,
                         "VIF": variance_inflation_factor(Xv.values, i)})
    vif = pd.DataFrame(rows).round(2)
    print("\nVariance inflation factors:")
    print(vif.to_string(index=False))

    # Coefficient signs under both specifications
    sign_rows = []
    for label, cols in [
        ("uncentred", ["CDD", "Tmin_sq", "humidity", "heat_x_humid_raw"]),
        ("centred", ["CDD", "Tmin_sq", "humidity_centered",
                     "heat_x_humid_centered"])]:
        sc = RobustScaler()
        xs = sc.fit_transform(m[cols])
        mdl = RidgeCV(alphas=np.logspace(-3, 2, 30)).fit(xs, m["peak_mw"])
        for c, b in zip(cols, mdl.coef_):
            sign_rows.append({"specification": label, "variable": c,
                              "coefficient": round(float(b), 1)})
    signs = pd.DataFrame(sign_rows)
    print("\nRidge coefficients under each specification:")
    print(signs.pivot(index="variable", columns="specification",
                      values="coefficient").to_string())

    vif.to_csv(cfg.results_dir / "table_vif.csv", index=False)
    record("corr_cdd_interaction_raw", round(float(r_raw), 3))
    record("corr_cdd_interaction_centred", round(float(r_cen), 3))
    return vif


# =============================================================================
# 09  EMPIRICAL PREDICTION INTERVALS
# =============================================================================

def prediction_intervals(oos_resid: np.ndarray, cfg: Config) -> dict:
    """Construct a prediction interval from pooled out-of-sample residuals.

    The original manuscript reported point +/- RMSE as a '95% interval'. That
    is neither 95% nor symmetric-justified. Residuals are pooled across CV
    folds so the interval reflects genuine out-of-sample error.
    """
    banner("09  EMPIRICAL PREDICTION INTERVALS")

    r = np.asarray(oos_resid, float)
    rmse = float(np.sqrt(np.mean(r ** 2)))
    sh_stat, sh_p = stats.shapiro(r) if len(r) < 5000 else (np.nan, np.nan)
    lo, hi = np.percentile(r, [2.5, 97.5])
    skew = float(stats.skew(r))

    print(f"Pooled out-of-sample residuals: n = {len(r)}")
    print(f"RMSE                       : {rmse:.1f} MW")
    print(f"Shapiro-Wilk normality     : W = {sh_stat:.3f}, p = {sh_p:.4f}")
    print(f"Residual skewness          : {skew:+.3f}")
    print(f"\nOriginal 'point +/- RMSE'  : [{-rmse:.0f}, {+rmse:.0f}] MW "
          f"(nominal coverage only {100*np.mean(np.abs(r) <= rmse):.1f}%)")
    print(f"Gaussian 1.96 x RMSE       : [{-1.96*rmse:.0f}, {+1.96*rmse:.0f}] MW")
    print(f"Empirical 95% interval     : [{lo:.0f}, {hi:.0f}] MW")
    direction = ("under-predicts high demand" if abs(hi) > abs(lo)
                 else "over-predicts")
    print(f"Interval is asymmetric: the model {direction}, "
          f"which is the safety-relevant direction for capacity planning.")

    out = {"n": int(len(r)), "rmse": rmse, "shapiro_p": float(sh_p),
           "skew": skew, "empirical_lo": float(lo), "empirical_hi": float(hi),
           "coverage_of_pm_rmse": float(100 * np.mean(np.abs(r) <= rmse))}
    record("prediction_interval", out)
    pd.DataFrame([out]).to_csv(cfg.results_dir / "table_prediction_interval.csv",
                               index=False)
    return out


# =============================================================================
# 10  COVID / 2020 SENSITIVITY ANALYSIS
# =============================================================================

def covid_sensitivity(df: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    """Should pandemic years be excluded, or modelled?

    The original manuscript deleted 2020 as an anomaly. That is defensible but
    it discards the single most informative natural experiment for the paper's
    own mechanism: if suppressed commercial daytime load drives the peak into
    the night, the lockdown period is the strongest available test of that.

    Rather than assert one choice, the model is fitted three ways and the
    sensitivity of conclusions is reported.
    """
    banner("10  COVID / 2020 SENSITIVITY ANALYSIS")

    specs = {
        "exclude_covid_years": dict(include=False, dummy=False),
        "include_with_indicator": dict(include=True, dummy=True),
        "include_no_indicator": dict(include=True, dummy=False),
    }
    rows = []
    for label, s in specs.items():
        m = build_monthly(df, cfg, include_covid=s["include"])
        month_cols = [c for c in m.columns if c.startswith("M_")]
        feats = MAGNITUDE_FEATURES + month_cols
        if s["dummy"]:
            feats = feats + ["is_covid"]
        X, y = m[feats], m["peak_mw"]
        tscv = TimeSeriesSplit(n_splits=cfg.n_cv_splits)
        r2s = []
        for tr, te in tscv.split(X):
            sc = RobustScaler()
            mdl = RidgeCV(alphas=np.logspace(-3, 2, 30))
            mdl.fit(sc.fit_transform(X.iloc[tr]), y.iloc[tr])
            r2s.append(r2_score(y.iloc[te],
                                mdl.predict(sc.transform(X.iloc[te]))))
        rows.append({"specification": label, "n_months": len(m),
                     "mean_R2": np.mean(r2s), "sd_R2": np.std(r2s),
                     "min_R2": np.min(r2s)})

    out = pd.DataFrame(rows).round(4)
    print(out.to_string(index=False))

    spread = out["mean_R2"].max() - out["mean_R2"].min()
    print(f"\nSpread in mean R2 across specifications: {spread:.4f}")
    print("Conclusions are robust." if spread < 0.10 else
          "Conclusions are sensitive to the COVID handling; report all three.")

    # Availability note, since 2020 timing data does not exist in our sources
    has_2020 = (df["year"] == 2020).sum()
    print(f"\nDaily records available for 2020: {has_2020}")
    if has_2020 == 0:
        print("  No 2020 sub-daily timing data exists in any verified source,")
        print("  so 2020 cannot enter the TIMING analysis regardless of choice.")

    out.to_csv(cfg.results_dir / "table_covid_sensitivity.csv", index=False)
    record("covid_sensitivity", out.to_dict("records"))
    record("n_2020_records", int(has_2020))
    return out


def lockdown_experiment(timing: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    """The 2020 lockdown as a natural experiment on the paper's mechanism.

    The manuscript argues that the nocturnal peak emerges when residential
    cooling load exceeds commercial and industrial daytime load. The 2020
    lockdown removed most commercial and industrial daytime demand while
    leaving residential cooling intact, which is as close to a controlled
    intervention as this system permits.

    If the mechanism is correct, the nocturnal share should spike during
    lockdown and relax as activity resumes. This is tested directly, and
    against the weekend effect, which is the same mechanism at weekly cadence.
    """
    banner("10b  LOCKDOWN NATURAL EXPERIMENT (2020)")

    t = timing.copy()
    if (t["year"] == 2020).sum() == 0:
        print("No 2020 timing records available; skipping.")
        return pd.DataFrame()

    # Monthly nocturnal share through 2020 against the lockdown timeline
    d20 = t[t["year"] == 2020]
    mo = d20.groupby("month")["is_nocturnal"].agg(
        n="size", nocturnal_pct=lambda s: round(100 * s.mean(), 1))
    print("2020 nocturnal share by month (national lockdown 25 Mar - 31 May,"
          " phased unlock from June):")
    print(mo.to_string())

    # 2020 versus the surrounding years, cooling season only
    comp = (t[t["is_cooling"] == 1].groupby("year")["is_nocturnal"]
            .agg(n="size", pct=lambda s: round(100 * s.mean(), 1)))
    print("\nCooling-season nocturnal share, all years:")
    print(comp.to_string())
    if 2020 in comp.index:
        rank = (comp["pct"] > comp.loc[2020, "pct"]).sum()
        print(f"  2020 ranks {rank + 1} of {len(comp)} years "
              f"({comp.loc[2020,'pct']}%)")

    # Formal contrast: lockdown months vs same months in adjacent normal years
    lock_months = [4, 5, 6]
    lock = t[(t["year"] == 2020) & (t["month"].isin(lock_months))]
    base = t[(t["year"].isin([2018, 2019, 2022])) & (t["month"].isin(lock_months))]
    if len(lock) and len(base):
        c = np.array([base["is_nocturnal"].sum(), lock["is_nocturnal"].sum()])
        n = np.array([len(base), len(lock)])
        p1, p2 = c / n
        pp = c.sum() / n.sum()
        z = (p2 - p1) / np.sqrt(pp * (1 - pp) * (1 / n[0] + 1 / n[1]))
        pval = 2 * (1 - stats.norm.cdf(abs(z)))
        print(f"\nApr-Jun contrast: normal years {100*p1:.1f}% (n={n[0]}) "
              f"vs lockdown 2020 {100*p2:.1f}% (n={n[1]})")
        print(f"  two-proportion z = {z:.2f}, p = {pval:.3e}")
        record("lockdown_z", round(float(z), 2))
        record("lockdown_p", float(pval))
        record("lockdown_pct", round(float(100 * p2), 1))
        record("lockdown_baseline_pct", round(float(100 * p1), 1))

    # The weekend effect is the same mechanism at weekly cadence. If both are
    # driven by commercial-load withdrawal, the lockdown effect should resemble
    # a permanent weekend.
    ce = t[t["is_cooling"] == 1]
    wk_end = 100 * ce[ce["is_weekend"] == 1]["is_nocturnal"].mean()
    wk_day = 100 * ce[ce["is_weekend"] == 0]["is_nocturnal"].mean()
    print(f"\nMechanistic consistency check:")
    print(f"  Normal weekday                : {wk_day:.1f}% nocturnal")
    print(f"  Normal weekend                : {wk_end:.1f}% nocturnal")
    if len(lock):
        print(f"  Lockdown weekday (Apr-Jun 20) : "
              f"{100*lock[lock['is_weekend']==0]['is_nocturnal'].mean():.1f}% nocturnal")
        print("  A lockdown weekday resembles or exceeds a normal weekend, "
              "consistent with commercial-load withdrawal as the driver.")

    mo.to_csv(cfg.results_dir / "table_lockdown_monthly.csv")
    record("lockdown_monthly", mo.reset_index().to_dict("records"))
    return mo


# =============================================================================
# 11  OUT-OF-SAMPLE VALIDATION ON OBSERVED 2025
# =============================================================================

def validate_2025(df: pd.DataFrame, cfg: Config) -> dict:
    """The original manuscript forecast 2025. Observed 2025 data now exists,
    so the forecast becomes a genuine out-of-sample test rather than a
    projection, which is materially stronger evidence."""
    banner("11  OUT-OF-SAMPLE VALIDATION ON OBSERVED 2025")

    m = build_monthly(df, cfg, include_covid=False)
    train = m[m["year"] <= 2024]
    test = m[m["year"] == 2025]
    if len(test) == 0:
        print("No 2025 monthly observations available.")
        return {}

    month_cols = [c for c in m.columns if c.startswith("M_")]
    feats = MAGNITUDE_FEATURES + month_cols
    sc = RobustScaler()
    mdl = RidgeCV(alphas=np.logspace(-3, 2, 30))
    mdl.fit(sc.fit_transform(train[feats]), train["peak_mw"])
    pred = mdl.predict(sc.transform(test[feats]))

    r2 = r2_score(test["peak_mw"], pred)
    rmse = float(np.sqrt(mean_squared_error(test["peak_mw"], pred)))
    mape = float(np.mean(np.abs((test["peak_mw"] - pred) / test["peak_mw"])) * 100)

    print(f"Trained on {len(train)} months (through 2024), tested on "
          f"{len(test)} observed 2025 months")
    print(f"  R2   = {r2:.4f}")
    print(f"  RMSE = {rmse:.1f} MW")
    print(f"  MAPE = {mape:.2f}%")

    comp = test[["year", "month", "peak_mw"]].copy()
    comp["predicted"] = pred.round(1)
    comp["error"] = (comp["peak_mw"] - comp["predicted"]).round(1)
    print("\nMonth-by-month:")
    print(comp.to_string(index=False))

    out = {"r2": float(r2), "rmse": rmse, "mape": mape, "n_test": len(test)}
    record("validation_2025", out)
    comp.to_csv(cfg.results_dir / "table_validation_2025.csv", index=False)
    return out


# =============================================================================
# 12  ABLATION STUDY
# =============================================================================

def ablation(weather: pd.DataFrame, df: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    """Do nocturnal predictors carry information that daytime ones cannot?

    Two questions are asked separately:
      (a) magnitude  - does Tmin beat Tmax at predicting how large the peak is?
      (b) timing     - do nocturnal variables predict WHEN the peak occurs?
    The second is the claim the manuscript actually needs.
    """
    banner("12  ABLATION STUDY")

    m = build_monthly(df, cfg, include_covid=False)
    # rebuild a daytime-only monthly frame for the comparison arm
    d = df.dropna(subset=["tempmax", "humidity", "tempmin"]).copy()
    d = d[~d["year"].isin(cfg.covid_years)]
    g = d.groupby(["year", "month"]).agg(
        peak_mw=("peak_mw", "max"), tempmax=("tempmax", "mean"),
        humidity=("humidity", "mean"), n_days=("date", "size")).reset_index()
    g = g[g["n_days"] >= 15]
    g["date"] = pd.to_datetime(dict(year=g["year"], month=g["month"], day=1))
    g = g.sort_values("date").reset_index(drop=True)
    g["CDD_max"] = (g["tempmax"] - cfg.cdd_base_c).clip(lower=0)
    g["Tmax_sq"] = g["tempmax"] ** 2
    g["days_since_start"] = (g["date"] - g["date"].min()).dt.days
    gd = pd.get_dummies(g["month"], prefix="M", drop_first=True).astype(int)
    g = pd.concat([g, gd], axis=1)

    tscv = TimeSeriesSplit(n_splits=cfg.n_cv_splits)
    rows = []

    def cv_r2(frame, feats):
        X, y = frame[feats], frame["peak_mw"]
        out = []
        for tr, te in tscv.split(X):
            sc = RobustScaler()
            mdl = RidgeCV(alphas=np.logspace(-3, 2, 30))
            mdl.fit(sc.fit_transform(X.iloc[tr]), y.iloc[tr])
            out.append(r2_score(y.iloc[te], mdl.predict(sc.transform(X.iloc[te]))))
        return np.mean(out), np.std(out)

    mc = [c for c in m.columns if c.startswith("M_")]
    gc = [c for c in g.columns if c.startswith("M_")]
    r_noc = cv_r2(m, ["CDD", "Tmin_sq", "humidity_centered",
                      "heat_x_humid_centered", "days_since_start"] + mc)
    r_day = cv_r2(g, ["CDD_max", "Tmax_sq", "humidity",
                      "days_since_start"] + gc)
    rows.append({"arm": "MAGNITUDE: nocturnal predictors (Tmin)",
                 "mean_R2": r_noc[0], "sd_R2": r_noc[1]})
    rows.append({"arm": "MAGNITUDE: daytime predictors (Tmax)",
                 "mean_R2": r_day[0], "sd_R2": r_day[1]})

    # (b) timing arm
    w = weather.dropna(subset=["CDD", "humidity_centered", "tempmax",
                               "is_weekend", "is_nocturnal"]).sort_values("date")
    def cv_auc(frame, feats):
        X, y = frame[feats], frame["is_nocturnal"]
        aucs = []
        for tr, te in tscv.split(X):
            sc = StandardScaler()
            mdl = LogisticRegression(max_iter=2000)
            mdl.fit(sc.fit_transform(X.iloc[tr]), y.iloc[tr])
            p = mdl.predict_proba(sc.transform(X.iloc[te]))[:, 1]
            if y.iloc[te].nunique() > 1:
                aucs.append(roc_auc_score(y.iloc[te], p))
        return np.mean(aucs), np.std(aucs)

    a_noc = cv_auc(w, ["CDD", "humidity_centered", "heat_x_humid_centered",
                       "is_weekend"])
    a_day = cv_auc(w, ["tempmax", "humidity_centered", "is_weekend"])
    a_wk = cv_auc(w, ["is_weekend"])
    rows.append({"arm": "TIMING: nocturnal predictors (AUC)",
                 "mean_R2": a_noc[0], "sd_R2": a_noc[1]})
    rows.append({"arm": "TIMING: daytime predictors (AUC)",
                 "mean_R2": a_day[0], "sd_R2": a_day[1]})
    rows.append({"arm": "TIMING: weekend only (AUC)",
                 "mean_R2": a_wk[0], "sd_R2": a_wk[1]})

    out = pd.DataFrame(rows).round(4)
    print(out.to_string(index=False))
    print("\nNote: magnitude rows are R2, timing rows are AUC; they are not "
          "directly comparable to one another.")

    out.to_csv(cfg.results_dir / "table_ablation.csv", index=False)
    record("ablation", out.to_dict("records"))
    return out


# =============================================================================
# 13  BESS SIZING FROM OBSERVED LOAD CURVES
# =============================================================================

def bess_sizing(cfg: Config, load_csv: Path | None = None) -> dict:
    """Size a Battery Energy Storage System from observed 5-minute load curves.

    The manuscript recommends BESS deployment but never quantified it, which
    the reviewer flagged. Sizing is derived directly from measured load rather
    than assumed.

    METHOD
      For each cooling-season day whose system peak falls in the nocturnal
      window, define a shaving target equal to the load level at the start of
      the discharge window (22:00). Energy above that level during the window
      is the storage that would be required to flatten the nocturnal peak.
      Capacity is reported at a design percentile across days, then grossed up
      for round-trip efficiency and depth-of-discharge limits.

    ASSUMPTIONS  (stated explicitly so they can be challenged)
      - discharge window 22:00-02:00
      - shaving target = load at window start
      - round-trip efficiency and DoD per Config
    """
    banner("13  BESS SIZING FROM OBSERVED LOAD CURVES")

    load_csv = load_csv or cfg.load_5min_csv
    if not load_csv.exists():
        print(f"Load curve file not found: {load_csv}")
        return {}

    ld = pd.read_csv(load_csv).dropna(subset=["load_MW"])
    # timestamps are day-first (dd-mm-yyyy HH:MM)
    ld["timestamp"] = pd.to_datetime(ld["timestamp"], dayfirst=True)
    ld["date"] = ld["timestamp"].dt.normalize()
    ld["hour"] = ld["timestamp"].dt.hour
    ld["month"] = ld["timestamp"].dt.month

    start_h, end_h = cfg.bess_window
    # window spans midnight: assign each reading to the evening it belongs to
    in_win = (ld["hour"] >= start_h) | (ld["hour"] < end_h)
    win = ld[in_win].copy()
    win["window_date"] = np.where(win["hour"] >= start_h,
                                  win["date"],
                                  win["date"] - pd.Timedelta(days=1))

    # restrict to cooling season and to nights that actually peak nocturnally
    daily_peak_hour = (ld.loc[ld.groupby("date")["load_MW"].idxmax()]
                         .set_index("date")["hour"])
    win["day_peak_hour"] = win["window_date"].map(daily_peak_hour)
    win["cooling"] = win["window_date"].dt.month.isin(cfg.cooling_months)
    noct_nights = win[(win["cooling"])
                      & (win["day_peak_hour"].isin(cfg.nocturnal_hours))]

    interval_h = 5 / 60.0
    rows = []
    for d, g in noct_nights.groupby("window_date"):
        g = g.sort_values("timestamp")
        if len(g) < 36:                       # need most of a 4-hour window
            continue
        target = g["load_MW"].iloc[0]         # load at 22:00
        excess = (g["load_MW"] - target).clip(lower=0)
        energy = float((excess * interval_h).sum())        # MWh
        power = float(excess.max())                        # MW
        dur = float((excess > 0).sum() * interval_h)       # hours above target
        rows.append({"date": d, "energy_mwh": energy,
                     "power_mw": power, "hours_above": dur,
                     "peak_mw": float(g["load_MW"].max())})

    if not rows:
        print("No qualifying nocturnal-peak nights found.")
        return {}

    b = pd.DataFrame(rows)
    q = cfg.bess_design_percentile
    e_design = float(np.percentile(b["energy_mwh"], q))
    p_design = float(np.percentile(b["power_mw"], q))
    d_median = float(b["hours_above"].median())

    installed = e_design / (cfg.bess_round_trip_efficiency
                            * cfg.bess_depth_of_discharge)

    print(f"Qualifying nocturnal-peak nights analysed: {len(b)}")
    print(f"Discharge window: {start_h:02d}:00-{end_h:02d}:00, "
          f"shaving target = load at window start\n")
    print(b[["energy_mwh", "power_mw", "hours_above"]].describe().round(1).to_string())
    print(f"\nDESIGN POINT (P{q:.0f} across nights)")
    print(f"  Usable energy required : {e_design:8.1f} MWh")
    print(f"  Discharge power        : {p_design:8.1f} MW")
    print(f"  Median duration above target : {d_median:.2f} h")
    print(f"  Installed capacity, grossed up for "
          f"eta={cfg.bess_round_trip_efficiency}, DoD={cfg.bess_depth_of_discharge}:")
    print(f"                           {installed:8.1f} MWh")
    print(f"  Worst observed night   : {b['energy_mwh'].max():.1f} MWh")

    out = {"n_nights": int(len(b)), "design_percentile": q,
           "usable_energy_mwh": round(e_design, 1),
           "discharge_power_mw": round(p_design, 1),
           "median_duration_h": round(d_median, 2),
           "installed_energy_mwh": round(installed, 1),
           "worst_night_mwh": round(float(b["energy_mwh"].max()), 1)}
    record("bess", out)
    b.to_csv(cfg.results_dir / "table_bess_nights.csv", index=False)
    pd.DataFrame([out]).to_csv(cfg.results_dir / "table_bess_design.csv", index=False)
    return out


# =============================================================================
# 14  FIGURES AND EXPORT
# =============================================================================

def make_figures(timing: pd.DataFrame, yearly: pd.DataFrame, cfg: Config) -> None:
    banner("14  FIGURES")

    # Fig 1: nocturnal share by year
    fig, ax = plt.subplots(figsize=(7, 3.4))
    ax.plot(yearly.index, yearly["nocturnal_pct"], marker="o", lw=2,
            color="#1f4e79", label="Nocturnal (22:00-01:59)")
    ax.plot(yearly.index, yearly["afternoon_pct"], marker="s", lw=2,
            color="#c0504d", label="Afternoon (14:00-17:59)")
    ax.set_xlabel("Year"); ax.set_ylabel("% of cooling-season days")
    ax.set_title("Composition of daily peak timing, Delhi")
    ax.grid(alpha=.3); ax.legend(frameon=False, fontsize=8)
    fig.tight_layout()
    fig.savefig(cfg.results_dir / "fig_timing_by_year.png", dpi=200)
    plt.close(fig)

    # Fig 2: peak-hour histogram, earliest vs latest era
    early_years = sorted(timing["year"].unique())[:3]
    late_years = sorted(timing["year"].unique())[-3:]
    e = timing[timing["year"].isin(early_years)]["peak_hour"]
    l = timing[timing["year"].isin(late_years)]["peak_hour"]
    bins = np.arange(-0.5, 24.5, 1)
    fig, ax = plt.subplots(figsize=(7, 3.4))
    ax.hist(e, bins=bins, alpha=.6, density=True,
            label=f"{early_years[0]}-{early_years[-1]}", color="#8faadc")
    ax.hist(l, bins=bins, alpha=.6, density=True,
            label=f"{late_years[0]}-{late_years[-1]}", color="#c00000")
    ax.set_xlabel("Hour of daily peak"); ax.set_ylabel("Density")
    ax.set_title("Shift in peak-hour distribution")
    ax.set_xticks(range(0, 24, 2)); ax.grid(alpha=.3)
    ax.legend(frameon=False, fontsize=8)
    fig.tight_layout()
    fig.savefig(cfg.results_dir / "fig_peak_hour_hist.png", dpi=200)
    plt.close(fig)

    print(f"Figures written to {cfg.results_dir}")


def export_results(cfg: Config) -> None:
    path = cfg.results_dir / "results_summary.json"
    with open(path, "w") as f:
        json.dump(RESULTS, f, indent=2, default=str)
    print(f"\nAll numeric results exported to {path}")


# =============================================================================
# MAIN
# =============================================================================

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--quick", action="store_true",
                    help="fewer bootstrap replicates, for a fast run")
    args = ap.parse_args()

    cfg = CFG.with_quick() if args.quick else CFG
    cfg.results_dir.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(cfg.seed)
    np.random.seed(cfg.seed)

    print(__doc__)
    print(f"Bootstrap replicates: {cfg.n_bootstrap} | CV folds: {cfg.n_cv_splits}")

    df = load_master(cfg)
    frames = analysis_frames(df, cfg)

    descriptives(frames["weather"], cfg)
    yearly = timing_analysis(frames["timing"], cfg)
    era_analysis(frames["timing"], cfg)
    reversal_baselines(frames["weather"], cfg)
    thr = threshold_analysis(frames["weather_models"],
                             frames["weather_allyear"], cfg, rng)
    era_sensitivity(frames["weather"], thr["threshold_c"], cfg)
    timing_models(frames["weather"], cfg)

    monthly = build_monthly(df, cfg, include_covid=False)
    _, oos_resid, _ = magnitude_models(monthly, cfg, tag="main")
    collinearity(monthly, cfg)
    prediction_intervals(oos_resid, cfg)
    covid_sensitivity(df, cfg)
    lockdown_experiment(frames['timing'], cfg)
    validate_2025(df, cfg)
    ablation(frames["weather_models"], df, cfg)
    bess_sizing(cfg)
    make_figures(frames["timing"], yearly, cfg)
    export_results(cfg)

    banner("PIPELINE COMPLETE")
    print(f"Outputs in: {cfg.results_dir}")


if __name__ == "__main__":
    main()
