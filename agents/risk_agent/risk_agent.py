import os
from datetime import datetime, UTC

import pandas as pd


SIGNAL_SETUPS_FILE = os.path.join("data", "signal_setups.csv")


def load_signal_setups():
    if not os.path.exists(SIGNAL_SETUPS_FILE):
        return pd.DataFrame()

    df = pd.read_csv(SIGNAL_SETUPS_FILE)

    if df.empty:
        return pd.DataFrame()

    return df


def apply_risk_checks(row):
    atr_pct = float(row["atr_pct"])
    latest_close = float(row["latest_close"])
    ma20 = float(row["ma20"])
    avg_volume_20 = float(row["avg_volume_20"])
    adjusted_setup_status = str(row["adjusted_setup_status"]).strip().lower()

    extension_pct = ((latest_close / ma20) - 1) * 100 if ma20 > 0 else 0

    high_atr_risk = atr_pct > 6.0
    overextended = extension_pct > 8.0
    low_liquidity = avg_volume_20 < 500000

    risk_flags = 0
    if high_atr_risk:
        risk_flags += 1
    if overextended:
        risk_flags += 1
    if low_liquidity:
        risk_flags += 1

    if adjusted_setup_status == "weak":
        risk_decision = "veto"
    elif risk_flags >= 2:
        risk_decision = "veto"
    elif risk_flags == 1:
        risk_decision = "caution"
    else:
        risk_decision = "approved"

    risk_notes = []
    if high_atr_risk:
        risk_notes.append("high_atr")
    if overextended:
        risk_notes.append("overextended")
    if low_liquidity:
        risk_notes.append("low_liquidity")

    if not risk_notes:
        risk_notes.append("clear")

    return {
        "extension_pct": round(extension_pct, 2),
        "high_atr_risk": high_atr_risk,
        "overextended": overextended,
        "low_liquidity": low_liquidity,
        "risk_flag_count": risk_flags,
        "risk_decision": risk_decision,
        "risk_notes": ",".join(risk_notes),
    }


def main():
    df = load_signal_setups()

    if df.empty:
        print("No signal setups found. Run Signal Agent first.")
        return

    risk_results = []

    for _, row in df.iterrows():
        result = apply_risk_checks(row)
        risk_results.append(result)

    risk_df = pd.DataFrame(risk_results)
    combined_df = pd.concat([df.reset_index(drop=True), risk_df], axis=1)

    combined_df = combined_df.sort_values(
        by=["risk_decision", "adjusted_setup_score", "ticker"],
        ascending=[True, False, True]
    )

    os.makedirs("data", exist_ok=True)

    all_risk_path = os.path.join("data", "risk_review.csv")
    approved_path = os.path.join("data", "risk_approved.csv")
    caution_path = os.path.join("data", "risk_caution.csv")
    veto_path = os.path.join("data", "risk_veto.csv")
    final_shortlist_path = os.path.join("data", "final_shortlist.csv")

    combined_df["checked_at_risk"] = datetime.now(UTC).isoformat()

    combined_df.to_csv(all_risk_path, index=False)
    combined_df[combined_df["risk_decision"] == "approved"].to_csv(approved_path, index=False)
    combined_df[combined_df["risk_decision"] == "caution"].to_csv(caution_path, index=False)
    combined_df[combined_df["risk_decision"] == "veto"].to_csv(veto_path, index=False)

    final_shortlist_df = combined_df[
        combined_df["risk_decision"].isin(["approved", "caution"])
    ].copy()

    final_shortlist_df.to_csv(final_shortlist_path, index=False)

    approved_count = len(combined_df[combined_df["risk_decision"] == "approved"])
    caution_count = len(combined_df[combined_df["risk_decision"] == "caution"])
    veto_count = len(combined_df[combined_df["risk_decision"] == "veto"])
    total_count = len(combined_df)

    print("\nRisk Agent finished.")
    print(f"Saved full risk review to: {all_risk_path}")
    print(f"Saved approved names to: {approved_path}")
    print(f"Saved caution names to: {caution_path}")
    print(f"Saved veto names to: {veto_path}")
    print(f"Saved final shortlist to: {final_shortlist_path}")

    print("\nRun summary:")
    print(f"Total names checked: {total_count}")
    print(f"Approved: {approved_count}")
    print(f"Caution: {caution_count}")
    print(f"Veto: {veto_count}")

    print("\nPreview:")
    print(combined_df)


if __name__ == "__main__":
    main()