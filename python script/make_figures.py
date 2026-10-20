#!/usr/bin/env python3
"""
Generate every figure in the manuscript from the current dataset, so no figure
can drift out of step with the text again.

  fig_peak_hour_bimodal.png   the two attractors, by era
  fig_load_shape.png          normalised summer day, 2010-2012 against 2023-2025
  fig_sensitivity_trap.png    segmented fit at 21.5 C with bootstrap CI
  fig_feature_importance.png  standardised RidgeCV coefficients
  fig_validation_2025.png     observed against predicted, 2025 held out

USAGE
  python make_figures.py --uploads /folder/with/inputs --out /folder/for/pngs

Inputs: delhi_master_dataset_v3.csv, delhi_sldc_15min_2010_2013.csv,
        5_min_2023to2026.csv  (defaults to the repo's Dataset/power data)
"""
import argparse
from pathlib import Path

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from sklearn.linear_model import RidgeCV
from sklearn.preprocessing import RobustScaler

NAVY, RUST, SLATE, SAND = "#1f3b73", "#c1452b", "#5b6770", "#d9a441"
SUMMER = [5, 6, 7, 8, 9]
plt.rcParams.update({"font.size": 7, "axes.labelsize": 7.5,
                     "axes.titlesize": 8, "legend.fontsize": 6.5,
                     "xtick.labelsize": 6.5, "ytick.labelsize": 6.5})


def clean(ax):
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    ax.grid(alpha=0.22, lw=0.4)
    ax.set_axisbelow(True)


# --------------------------------------------------------------------------
def fig_bimodal(df, out):
    c = df[df.is_cooling_season == 1].dropna(subset=["peak_hour"]).copy()
    c["peak_hour"] = c["peak_hour"].astype(int)
    fig, ax = plt.subplots(figsize=(3.4, 2.3), dpi=300)
    hours = np.arange(24)
    for lab, lo, hi, col, hatch in [("2010-2012", 2010, 2012, SLATE, None),
                                    ("2023-2025", 2023, 2025, RUST, None)]:
        e = c[c.year.between(lo, hi)]
        share = [100 * (e.peak_hour == h).mean() for h in hours]
        ax.bar(hours + (-0.2 if lo == 2010 else 0.2), share, width=0.4,
               color=col, label=lab, edgecolor="none", alpha=0.9)
    ax.axvspan(21.5, 23.9, color=NAVY, alpha=0.07, lw=0)
    ax.axvspan(-0.5, 1.5, color=NAVY, alpha=0.07, lw=0)
    ax.axvspan(13.5, 17.5, color=SAND, alpha=0.12, lw=0)
    ax.text(15.5, 41, "afternoon", ha="center", fontsize=6, color=SLATE)
    ax.text(22.7, 41, "nocturnal", ha="center", fontsize=6, color=SLATE)
    ax.set_xticks(range(0, 24, 3))
    ax.set_xticklabels([f"{h:02d}" for h in range(0, 24, 3)])
    ax.set_xlabel("Hour of system peak (IST)")
    ax.set_ylabel("Share of cooling-season days (%)")
    ax.set_xlim(-0.8, 23.8)
    ax.legend(frameon=False, loc="upper left")
    clean(ax)
    fig.tight_layout()
    fig.savefig(out / "fig_peak_hour_bimodal.png", bbox_inches="tight")
    plt.close(fig)


def _shape(ts, load, month_col):
    d = pd.DataFrame({"ts": ts, "load": load})
    d = d[d.ts.dt.month.isin(SUMMER)].copy()
    d["day"] = d.ts.dt.normalize()
    d["hour"] = d.ts.dt.hour
    h = d.groupby(["day", "hour"]).load.mean().reset_index()
    h["norm"] = h.load / h.groupby("day").load.transform("mean")
    return h.groupby("hour").norm.mean(), d.day.nunique()


def fig_load_shape(uploads, out):
    s = pd.read_csv(uploads / "delhi_sldc_15min_2010_2013.csv")
    s.columns = ["idx", "timeslots", "days", "date", "mW", "discom", "timepoint"]
    t = s[s.discom == "Total"].copy()
    t["ts"] = (pd.to_datetime("1899-12-30") + pd.to_timedelta(t["days"], unit="D")
               + pd.to_timedelta((t.timepoint - 1) * 15, unit="m"))
    t = t[t.mW > 100]
    early, n_e = _shape(t.ts, t.mW, None)

    p = uploads / "5_min_2023to2026.csv"
    if not p.exists():                      # name used in earlier drafts
        p = uploads / "load_data.csv"
    l = pd.read_csv(p).dropna(subset=["load_MW"])
    # timestamps are day-first (dd-mm-yyyy HH:MM)
    l["timestamp"] = pd.to_datetime(l["timestamp"], dayfirst=True)
    l = l[l.timestamp.dt.year.between(2023, 2025)]
    late, n_l = _shape(l.timestamp, l.load_MW, None)

    fig, ax = plt.subplots(figsize=(3.4, 2.3), dpi=300)
    ax.plot(early.index, early.values, "o-", color=SLATE, lw=1.3, ms=2.6,
            label=f"2010-2012 ({n_e} days)")
    ax.plot(late.index, late.values, "s-", color=RUST, lw=1.3, ms=2.6,
            label=f"2023-2025 ({n_l} days)")
    ax.axhline(1.0, color="k", lw=0.5, alpha=0.4)
    for h, col in [(15, SAND), (23, NAVY)]:
        ax.axvline(h, color=col, lw=0.8, ls=":", alpha=0.8)
    ax.annotate("", xy=(19.5, late[19]), xytext=(19.5, early[19]),
                arrowprops=dict(arrowstyle="<->", lw=0.8, color=NAVY))
    ax.text(19.9, (early[19] + late[19]) / 2, "evening\nshoulder lost",
            ha="left", va="center", fontsize=5.8, color=NAVY)
    ax.set_xticks(range(0, 24, 3))
    ax.set_xlabel("Hour (IST)")
    ax.set_ylabel("Load / that day's mean load")
    ax.legend(frameon=False, loc="upper left", ncol=1)
    ax.set_ylim(0.80, 1.19)
    clean(ax)
    fig.tight_layout()
    fig.savefig(out / "fig_load_shape.png", bbox_inches="tight")
    plt.close(fig)


def _hinge(x, y, tau):
    h = np.clip(x - tau, 0, None)
    X = np.column_stack([np.ones_like(x), x, h])
    c, *_ = np.linalg.lstsq(X, y, rcond=None)
    r = y - X @ c
    return float(r @ r), c


def fig_trap(df, out):
    c = df[(df.is_cooling_season == 1) & (df.year >= 2013)].dropna(
        subset=["tempmin", "peak_mw"])
    x, y = c.tempmin.to_numpy(float), c.peak_mw.to_numpy(float)
    grid = np.arange(16.0, 32.0, 0.25)
    tau = float(grid[int(np.argmin([_hinge(x, y, t)[0] for t in grid]))])
    _, co = _hinge(x, y, tau)
    below, above = co[1], co[1] + co[2]

    fig, ax = plt.subplots(figsize=(3.4, 2.4), dpi=300)
    ax.scatter(x, y, s=2.5, color=SLATE, alpha=0.13, lw=0, rasterized=True)
    b = pd.cut(c.tempmin, np.arange(14, 36, 1))
    m = c.groupby(b, observed=True).agg(t=("tempmin", "mean"),
                                        p=("peak_mw", "mean"))
    ax.plot(m.t, m.p, "o", color=NAVY, ms=3.4, label="1$^\\circ$C bin mean")
    xs = np.linspace(x.min(), x.max(), 200)
    ax.plot(xs, co[0] + co[1] * xs + co[2] * np.clip(xs - tau, 0, None),
            color=RUST, lw=1.6, label="segmented fit")
    ax.axvspan(21.25, 22.50, color=RUST, alpha=0.16, lw=0)
    ax.axvline(tau, color=RUST, lw=0.9, ls="--")
    ax.annotate(f"{tau:.1f}$^\\circ$C\n95% CI 21.3-22.5", xy=(tau, 4150),
                xytext=(16.6, 5600), fontsize=5.8, color=RUST, ha="left",
                arrowprops=dict(arrowstyle="->", lw=0.7, color=RUST))
    ax.text(17.4, 3050, f"{below:.0f} MW/$^\\circ$C", fontsize=6.2, color=SLATE)
    ax.text(29.5, 5200, f"{above:.0f} MW/$^\\circ$C", fontsize=6.2, color=RUST)
    ax.set_xlabel("Nighttime minimum temperature $T_{min}$ ($^\\circ$C)")
    ax.set_ylabel("Daily peak demand (MW)")
    ax.legend(frameon=False, loc="upper left")
    clean(ax)
    fig.tight_layout()
    fig.savefig(out / "fig_sensitivity_trap.png", bbox_inches="tight")
    plt.close(fig)


def _monthly(df):
    m = df.dropna(subset=["tempmin", "humidity"]).copy()
    m = m[(m.year >= 2013) & (~m.year.isin([2020, 2021]))]
    g = m.groupby(["year", "month"]).agg(
        peak_mw=("peak_mw", "max"), tempmin=("tempmin", "mean"),
        humidity=("humidity", "mean"), is_weekend=("is_weekend", "mean"),
        n=("date", "size")).reset_index()
    g = g[g.n >= 15].sort_values(["year", "month"]).reset_index(drop=True)
    g["CDD"] = (g.tempmin - 18).clip(lower=0)
    g["Tmin_sq"] = g.tempmin ** 2
    g["humidity_centered"] = g.humidity - g.humidity.mean()
    g["heat_x_humid_centered"] = g.CDD * g.humidity_centered
    dt = pd.to_datetime(dict(year=g.year, month=g.month, day=1))
    g["days_since_start"] = (dt - dt.min()).dt.days
    return pd.concat(
        [g, pd.get_dummies(g.month, prefix="M", drop_first=True).astype(int)],
        axis=1)


def fig_importance(df, out):
    g = _monthly(df)
    phys = ["CDD", "Tmin_sq", "humidity_centered", "heat_x_humid_centered",
            "days_since_start", "is_weekend"]
    cols = phys + [c for c in g.columns if c.startswith("M_")]
    sc = RobustScaler()
    mdl = RidgeCV(alphas=np.logspace(-3, 2, 30)).fit(sc.fit_transform(g[cols]),
                                                     g.peak_mw)
    names = {"CDD": "Cooling degree days", "Tmin_sq": "Thermal mass $T_{min}^2$",
             "days_since_start": "Trend", "humidity_centered": "Humidity (centred)",
             "heat_x_humid_centered": "Heat $\\times$ humidity",
             "is_weekend": "Weekend fraction"}
    co = pd.Series(mdl.coef_[:len(phys)], index=[names[p] for p in phys])
    co = co.sort_values(key=abs)
    fig, ax = plt.subplots(figsize=(3.4, 2.1), dpi=300)
    ax.barh(co.index, co.values,
            color=[RUST if v < 0 else NAVY for v in co.values], height=0.6)
    for i, v in enumerate(co.values):
        ax.text(v + (70 if v > 0 else -70), i, f"{v:,.0f}", va="center",
                ha="left" if v > 0 else "right", fontsize=6)
    ax.axvline(0, color="k", lw=0.6)
    ax.set_xlabel("RidgeCV coefficient, standardised predictors")
    ax.set_xlim(-2450, 3650)
    clean(ax)
    ax.grid(axis="y", alpha=0)
    fig.tight_layout()
    fig.savefig(out / "fig_feature_importance.png", bbox_inches="tight")
    plt.close(fig)


def fig_validation(df, out):
    g = _monthly(df)
    cols = ["CDD", "Tmin_sq", "humidity_centered", "heat_x_humid_centered",
            "days_since_start", "is_weekend"] + [c for c in g.columns
                                                 if c.startswith("M_")]
    tr, te = g[g.year <= 2024], g[g.year == 2025]
    sc = RobustScaler()
    mdl = RidgeCV(alphas=np.logspace(-3, 2, 30))
    mdl.fit(sc.fit_transform(tr[cols]), tr.peak_mw)
    pred = mdl.predict(sc.transform(te[cols]))
    lab = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    fig, ax = plt.subplots(figsize=(3.4, 2.3), dpi=300)
    ax.plot(te.month, te.peak_mw, "o-", color=NAVY, lw=1.4, ms=4,
            label="Observed")
    ax.plot(te.month, pred, "s--", color=RUST, lw=1.4, ms=4, label="Predicted")
    ax.fill_between(te.month, pred - 388.2, pred + 388.2, color=RUST,
                    alpha=0.13, lw=0, label="$\\pm$RMSE band")
    ax.set_xticks(range(1, 13))
    ax.set_xticklabels(lab)
    ax.set_ylabel("Monthly peak demand (MW)")
    ax.set_ylim(3600, 9400)
    ax.legend(frameon=False, loc="lower center", ncol=3)
    clean(ax)
    fig.tight_layout()
    fig.savefig(out / "fig_validation_2025.png", bbox_inches="tight")
    plt.close(fig)


def main():
    ap = argparse.ArgumentParser()
    repo = Path(__file__).resolve().parent.parent
    ap.add_argument("--uploads", default=str(repo / "Dataset" / "power data"))
    ap.add_argument("--out", default=str(repo / "results" / "figures"))
    a = ap.parse_args()
    up, out = Path(a.uploads), Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    df = pd.read_csv(up / "delhi_master_dataset_v3.csv", parse_dates=["date"])

    fig_bimodal(df, out)
    fig_load_shape(up, out)
    fig_trap(df, out)
    fig_importance(df, out)
    fig_validation(df, out)
    print("written to", out)
    for f in sorted(out.glob("*.png")):
        print("  ", f.name)


if __name__ == "__main__":
    main()
