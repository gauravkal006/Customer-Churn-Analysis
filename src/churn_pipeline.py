"""
Customer Churn Analysis - end-to-end pipeline
Author: Gaurav Kalamkhede

Reproduces the analysis from notebooks/churn_analysis.ipynb as a script:
  Excel -> SQLite -> pandas -> cleaning -> feature engineering -> KPIs -> charts -> exports

Run from the project root:
    python src/churn_pipeline.py
"""

import json
import sqlite3
import warnings
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import seaborn as sns

warnings.filterwarnings("ignore")

ROOT = Path(__file__).resolve().parents[1]
RAW_XLSX = ROOT / "data" / "raw" / "customer_churn_data_raw.xlsx"
DB_PATH = ROOT / "data" / "customer_churn.db"
PROCESSED_DIR = ROOT / "data" / "processed"
CHART_DIR = ROOT / "images" / "charts"

# Fixed snapshot date so tenure / age are reproducible on every run
SNAPSHOT_DATE = pd.Timestamp("2025-12-31")

# muted palette: one dark accent for the segment that matters, greys for everything else
PALETTE = {"churned": "#2F4A6D", "retained": "#B8C0C8", "neutral": "#B8C0C8", "muted": "#D5DAE0", "medium": "#7D8B99"}


# ---------------------------------------------------------------- 1. Data load
def build_database(xlsx_path=RAW_XLSX, db_path=DB_PATH):
    """Load every Excel sheet into its own SQLite table."""
    with sqlite3.connect(db_path) as conn:
        for sheet in pd.ExcelFile(xlsx_path).sheet_names:
            pd.read_excel(xlsx_path, sheet_name=sheet).to_sql(sheet, conn, if_exists="replace", index=False)


def load_tables(db_path=DB_PATH):
    """Read all tables from SQLite into a dict of DataFrames."""
    with sqlite3.connect(db_path) as conn:
        names = pd.read_sql("SELECT name FROM sqlite_master WHERE type='table';", conn)["name"]
        return {n: pd.read_sql(f"SELECT * FROM {n}", conn) for n in names}


# ------------------------------------------------------------- 2. Data cleaning
def clean_customer(df):
    df = df.rename(columns={"name": "customer_name"}).drop(columns=["interests", "pincode"])
    df["dob"] = pd.to_datetime(df["dob"])
    df["gender"] = df["gender"].replace({"Men": "Male", "Women": "Female"})
    df["customer_name"] = df["customer_name"].str.title()
    # fill missing country from the state -> country pairs we already know
    state_country = df.dropna(subset=["country"]).set_index("state")["country"].to_dict()
    df["country"] = df["country"].fillna(df["state"].map(state_country))
    return df


def clean_subscription(df):
    date_cols = ["subscription_start_date", "renewal_date", "cancellation_date"]
    df[date_cols] = df[date_cols].apply(pd.to_datetime)
    df["subscription_type"] = df["subscription_type"].replace({"Refferal": "Referral"})
    return df


def clean_support(df):
    df = df.drop(columns=["col_1", "comment"])
    df["complaint_date"] = pd.to_datetime(df["complaint_date"])
    # one row per customer: keep total complaints, any escalation and the latest CSAT
    df["complaint_count"] = df.groupby("customerid")["customerid"].transform("count")
    df["escalations"] = df.groupby("customerid")["escalations"].transform(lambda s: "Y" if (s == "Y").any() else "N")
    return df.sort_values("complaint_date").drop_duplicates("customerid", keep="last")


# ------------------------------------------------------- 3. Feature engineering
def build_master(customer, subscription, support):
    subscription["churn_flag"] = np.where(subscription["cancellation_date"].notna(), 1, 0)

    df = (subscription
          .merge(customer, on="customerid", how="left")
          .merge(support, on="customerid", how="left"))
    assert len(df) == len(subscription), "merge created duplicate rows"

    end_date = df["cancellation_date"].fillna(SNAPSHOT_DATE)
    df["tenure_days"] = (end_date - df["subscription_start_date"]).dt.days
    df["age"] = ((SNAPSHOT_DATE - df["dob"]).dt.days // 365.25).astype(int)
    df["age_group"] = pd.cut(df["age"], bins=[0, 25, 35, 45, 100], labels=["<25", "25-34", "35-44", "45+"], right=False)
    df["complaint_count"] = df["complaint_count"].fillna(0).astype(int)
    df["escalation_flag"] = np.where(df["escalations"] == "Y", 1, 0)
    df["churn_risk"] = np.select(
        [df["churn_score"] < 50, df["churn_score"] < 70, df["churn_score"] >= 70],
        ["Low", "Medium", "High"], default="Unknown")
    df["cancellation_month"] = df["cancellation_date"].dt.to_period("M")
    return df


# -------------------------------------------------------------------- 4. KPIs
def compute_kpis(df):
    churned, active = df[df["churn_flag"] == 1], df[df["churn_flag"] == 0]
    total_mrr = df["monthly_charges"].sum()
    contract_churn = df.groupby("contract_type")["churn_flag"].mean().mul(100)
    risky_active = active[active["churn_risk"].isin(["High", "Medium"])]
    return {
        "total_customers": int(len(df)),
        "churned_customers": int(len(churned)),
        "churn_rate_pct": round(df["churn_flag"].mean() * 100, 2),
        "retention_rate_pct": round(100 - df["churn_flag"].mean() * 100, 2),
        "arpu": round(df["monthly_charges"].mean(), 2),
        "arpu_active": round(active["monthly_charges"].mean(), 2),
        "avg_tenure_days": int(round(df["tenure_days"].mean())),
        "total_mrr": round(total_mrr, 2),
        "mrr_lost": round(churned["monthly_charges"].sum(), 2),
        "mrr_lost_pct": round(churned["monthly_charges"].sum() / total_mrr * 100, 2),
        "cltv_lost": int(churned["cltv"].sum()),
        "avg_cltv_churned": round(churned["cltv"].mean(), 2),
        "avg_cltv_retained": round(active["cltv"].mean(), 2),
        "escalation_rate_pct": round(df["escalation_flag"].mean() * 100, 2),
        "avg_complaints_per_customer": round(df["complaint_count"].sum() / df["customerid"].nunique(), 2),
        "corr_escalation_churn": round(df["escalation_flag"].corr(df["churn_flag"]), 2),
        "churn_rate_with_escalation_pct": round(df.loc[df["escalation_flag"] == 1, "churn_flag"].mean() * 100, 2),
        "churn_rate_without_escalation_pct": round(df.loc[df["escalation_flag"] == 0, "churn_flag"].mean() * 100, 2),
        "monthly_contract_churn_pct": round(contract_churn.get("Monthly", 0), 2),
        "annual_contract_churn_pct": round(contract_churn.get("Annual", 0), 2),
        "at_risk_active_customers": int(len(risky_active)),
        "mrr_at_risk_active": round(risky_active["monthly_charges"].sum(), 2),
        "cltv_at_risk_active": int(risky_active["cltv"].sum()),
        "avg_customer_age": round(df["age"].mean(), 1),
    }


def segment(df, by):
    """Churn rate, customers and revenue per segment."""
    return (df.groupby(by, observed=True)
              .agg(customers=("customerid", "nunique"),
                   churned=("churn_flag", "sum"),
                   churn_rate_pct=("churn_flag", lambda s: round(s.mean() * 100, 2)),
                   monthly_revenue=("monthly_charges", "sum"),
                   cltv=("cltv", "sum"))
              .sort_values("churn_rate_pct", ascending=False)
              .reset_index())


# ------------------------------------------------------------- 5. Charts
def _save(fig, name):
    fig.tight_layout()
    fig.savefig(CHART_DIR / name, dpi=160, bbox_inches="tight")
    plt.close(fig)


def _bar(ax, series, title, color_fn=None, fmt="{:.0f}%"):
    colors = [color_fn(v) if color_fn else PALETTE["neutral"] for v in series.values]
    bars = ax.bar(series.index.astype(str), series.values, color=colors, width=0.6)
    for b, v in zip(bars, series.values):
        ax.annotate(fmt.format(v), (b.get_x() + b.get_width() / 2, b.get_height()),
                    ha="center", va="bottom", fontsize=10, xytext=(0, 3), textcoords="offset points")
    ax.set_title(title, loc="left", fontsize=13, fontweight="bold")
    ax.spines[["top", "right"]].set_visible(False)


def make_charts(df):
    CHART_DIR.mkdir(parents=True, exist_ok=True)
    sns.set_theme(style="white", font_scale=1.0)
    hi = lambda v: PALETTE["churned"] if v >= 40 else PALETTE["neutral"]

    # 1. Monthly churn trend
    trend = df[df["churn_flag"] == 1].groupby("cancellation_month").size()
    fig, ax = plt.subplots(figsize=(9, 3.6))
    ax.plot(trend.index.astype(str), trend.values, marker="o", markersize=10, linewidth=2.5, color=PALETTE["churned"])
    for x, y in zip(trend.index.astype(str), trend.values):
        ax.annotate(str(y), (x, y), ha="center", xytext=(0, 9), textcoords="offset points")
    ax.set_ylim(0, trend.max() + 1)
    ax.yaxis.set_major_locator(plt.MaxNLocator(integer=True))
    ax.set_title("Monthly churn trend (cancellations per month)", loc="left", fontsize=13, fontweight="bold")
    ax.set_ylabel("Churned customers")
    ax.spines[["top", "right"]].set_visible(False)
    _save(fig, "01_monthly_churn_trend.png")

    # 2. Churn by plan and contract
    fig, axes = plt.subplots(1, 2, figsize=(10, 4))
    plan = df.groupby("plan_type")["churn_flag"].mean().mul(100).reindex(["Basic", "Standard", "Premium"])
    contract = df.groupby("contract_type")["churn_flag"].mean().mul(100).reindex(["Monthly", "Annual"])
    _bar(axes[0], plan, "Churn rate by plan type", hi)
    _bar(axes[1], contract, "Churn rate by contract type", hi)
    for ax in axes:
        ax.set_ylim(0, 75)
        ax.set_ylabel("Churn rate (%)")
    _save(fig, "02_churn_by_plan_contract.png")

    # 3. Churn by state
    state = df.groupby("state")["churn_flag"].mean().mul(100).sort_values(ascending=False)
    fig, ax = plt.subplots(figsize=(11, 4.2))
    _bar(ax, state, "Churn rate by state", lambda v: PALETTE["churned"] if v > 0 else PALETTE["muted"])
    ax.set_ylabel("Churn rate (%)")
    ax.set_ylim(0, 115)
    plt.setp(ax.get_xticklabels(), rotation=40, ha="right")
    _save(fig, "03_churn_by_state.png")

    # 4. Cancellation reasons
    reasons = df["cancellation_reason"].value_counts().sort_values()
    fig, ax = plt.subplots(figsize=(8, 3.6))
    ax.barh(reasons.index, reasons.values, color=PALETTE["churned"], height=0.55)
    for i, v in enumerate(reasons.values):
        ax.annotate(str(v), (v, i), xytext=(4, 0), textcoords="offset points", va="center")
    ax.set_title("Why customers cancelled", loc="left", fontsize=13, fontweight="bold")
    ax.set_xlabel("Customers")
    ax.spines[["top", "right"]].set_visible(False)
    _save(fig, "04_cancellation_reasons.png")

    # 5. Escalation vs churn
    esc = df.groupby("escalation_flag")["churn_flag"].mean().mul(100).rename({0: "No escalation", 1: "Escalated"})
    fig, ax = plt.subplots(figsize=(6, 4))
    _bar(ax, esc, "Churn rate: escalated vs not", hi)
    ax.set_ylabel("Churn rate (%)")
    ax.set_ylim(0, 110)
    _save(fig, "05_escalation_vs_churn.png")

    # 6. Risk segments - customers and MRR
    order = ["Low", "Medium", "High"]
    risk = df.groupby("churn_risk").agg(customers=("customerid", "count"), mrr=("monthly_charges", "sum")).reindex(order)
    fig, axes = plt.subplots(1, 2, figsize=(10, 4))
    risk_colors = {"Low": PALETTE["retained"], "Medium": PALETTE["medium"], "High": PALETTE["churned"]}
    for ax, col, title, fmt in [(axes[0], "customers", "Customers by churn-risk tier", "{:.0f}"),
                                (axes[1], "mrr", "Monthly revenue by churn-risk tier", "Rs {:.0f}")]:
        bars = ax.bar(risk.index, risk[col], color=[risk_colors[r] for r in risk.index], width=0.6)
        for b, v in zip(bars, risk[col]):
            ax.annotate(fmt.format(v), (b.get_x() + b.get_width() / 2, v), ha="center", xytext=(0, 3), textcoords="offset points")
        ax.set_title(title, loc="left", fontsize=13, fontweight="bold")
        ax.spines[["top", "right"]].set_visible(False)
    _save(fig, "06_churn_risk_segments.png")

    # 7. CLTV churned vs retained
    np.random.seed(42)  # fixed jitter so the chart is identical on every run
    fig, ax = plt.subplots(figsize=(7, 4))
    sns.boxplot(data=df.assign(status=df["churn_flag"].map({0: "Retained", 1: "Churned"})),
                x="status", y="cltv", order=["Retained", "Churned"], hue="status", legend=False,
                palette={"Retained": PALETTE["retained"], "Churned": PALETTE["churned"]}, width=0.5, showfliers=False, ax=ax)
    sns.stripplot(data=df.assign(status=df["churn_flag"].map({0: "Retained", 1: "Churned"})),
                  x="status", y="cltv", order=["Retained", "Churned"], color="#222", size=5, ax=ax)
    ax.set_title("Customer lifetime value: retained vs churned", loc="left", fontsize=13, fontweight="bold")
    ax.set_xlabel("")
    ax.set_ylabel("CLTV (Rs)")
    ax.spines[["top", "right"]].set_visible(False)
    _save(fig, "07_cltv_retained_vs_churned.png")

    # 8. Correlation heatmap (ordinal encoding)
    enc = df[["plan_type", "contract_type", "churn_score", "churn_flag", "churn_risk", "escalation_flag",
              "monthly_charges", "cltv", "tenure_days", "complaint_count"]].copy()
    for col, order_ in {"plan_type": ["Basic", "Standard", "Premium"], "contract_type": ["Monthly", "Annual"],
                        "churn_risk": ["Low", "Medium", "High"]}.items():
        enc[col] = pd.Categorical(enc[col], categories=order_, ordered=True).codes
    fig, ax = plt.subplots(figsize=(9, 7))
    sns.heatmap(enc.corr(), annot=True, fmt=".2f", cmap=sns.blend_palette(["#8A96A3", "#FFFFFF", "#2F4A6D"], as_cmap=True), vmin=-1, vmax=1, linewidths=0.5, ax=ax)
    ax.set_title("Correlation matrix (ordinal-encoded features)", loc="left", fontsize=13, fontweight="bold")
    _save(fig, "08_correlation_heatmap.png")

    # 9. Subscription channel
    chan = df.groupby("subscription_type")["churn_flag"].mean().mul(100).sort_values(ascending=False)
    fig, ax = plt.subplots(figsize=(6.5, 4))
    _bar(ax, chan, "Churn rate by acquisition channel", hi)
    ax.set_ylabel("Churn rate (%)")
    ax.set_ylim(0, 75)
    _save(fig, "09_churn_by_channel.png")


# --------------------------------------------------------------- main
def run():
    if not DB_PATH.exists():
        build_database()
    t = load_tables()
    customer = clean_customer(t["db_customer"])
    subscription = clean_subscription(t["db_subscription"])
    support = clean_support(t["db_support"])
    df = build_master(customer, subscription, support)

    kpis = compute_kpis(df)
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    df.drop(columns=["cancellation_month"]).to_csv(PROCESSED_DIR / "churn_master_clean.csv", index=False)
    (PROCESSED_DIR / "kpi_summary.json").write_text(json.dumps(kpis, indent=2))
    for by in ["plan_type", "contract_type", "subscription_type", "state", "gender", "age_group", "churn_risk"]:
        segment(df, by).to_csv(PROCESSED_DIR / f"segment_{by}.csv", index=False)

    make_charts(df)
    return df, kpis


if __name__ == "__main__":
    _, kpis = run()
    for k, v in kpis.items():
        print(f"{k:<36}{v}")
