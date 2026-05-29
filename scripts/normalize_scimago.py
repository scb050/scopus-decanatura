"""Normalize scimago_2014_2025.csv: collapse year-varying columns."""

import re
from pathlib import Path

import pandas as pd

CSV = Path(__file__).parent.parent / "data" / "raw" / "scimago_2014_2025.csv"


def find_year_varying_columns(df: pd.DataFrame) -> dict[str, list[str]]:
    """Return {base_name: [col_2014, col_2015, ...]} for columns that vary by year."""
    year_col_re = re.compile(r"^(.+)\s*\((\d{4})\)$")
    groups: dict[str, list[str]] = {}
    for col in df.columns:
        m = year_col_re.match(col)
        if m:
            base = m.group(1).strip()
            groups.setdefault(base, []).append(col)
    # Only flag groups with more than one year column
    return {k: sorted(v) for k, v in groups.items() if len(v) > 1}


def collapse_year_column(df: pd.DataFrame, year_cols: list[str], new_name: str) -> pd.DataFrame:
    """Pick the value from the column matching each row's Year, drop all year cols."""
    year_col_re = re.compile(r"\((\d{4})\)$")
    year_map = {}
    for col in year_cols:
        m = year_col_re.search(col)
        if m:
            year_map[int(m.group(1))] = col

    def pick(row):
        col = year_map.get(row["Year"])
        return row[col] if col else None

    df[new_name] = df.apply(pick, axis=1)
    df = df.drop(columns=year_cols)
    return df


def main():
    print(f"Reading {CSV} ...")
    df = pd.read_csv(CSV, low_memory=False)
    print(f"  Shape: {df.shape[0]:,} rows × {df.shape[1]} columns")

    # --- Detect year-varying columns ---
    varying = find_year_varying_columns(df)
    if varying:
        print(f"\nYear-varying column groups found ({len(varying)}):")
        for base, cols in varying.items():
            print(f"  '{base}': {cols}")
    else:
        print("\nNo additional year-varying column groups beyond Total Docs.")

    # --- Collapse each group ---
    rename_map = {
        "Total Docs.": "Total_Docs",
    }
    for base, cols in varying.items():
        new_name = rename_map.get(base, base.replace(" ", "_").replace(".", "").strip("_"))
        print(f"\n  Collapsing '{base}' -> '{new_name}'")
        df = collapse_year_column(df, cols, new_name)

    # --- Fix duplicate Publisher column (Publisher vs Publisher.1) ---
    if "Publisher.1" in df.columns:
        print("\nDuplicate 'Publisher' column detected (Publisher.1).")
        # Publisher.1 is the second 'Publisher' in the raw CSV — it's identical content.
        # Keep the first one and drop the duplicate.
        equal = df["Publisher"].equals(df["Publisher.1"])
        print(f"  Publisher == Publisher.1 for all rows: {equal}")
        df = df.drop(columns=["Publisher.1"])
        print("  Dropped 'Publisher.1'.")

    # --- Reorder: put Total_Docs right after H index ---
    cols = list(df.columns)
    if "Total_Docs" in cols and "H index" in cols:
        cols.remove("Total_Docs")
        h_pos = cols.index("H index")
        cols.insert(h_pos + 1, "Total_Docs")
        df = df[cols]

    # --- Save ---
    df.to_csv(CSV, index=False, encoding="utf-8")
    print(f"\nSaved to: {CSV}")
    print(f"Final shape: {df.shape[0]:,} rows × {df.shape[1]} columns")
    print(f"\nFinal columns ({len(df.columns)}):")
    for i, c in enumerate(df.columns, 1):
        print(f"  {i:2}. {c}")

    # --- Quick sanity check ---
    null_pct = df["Total_Docs"].isna().mean() * 100
    print(f"\nTotal_Docs null rate: {null_pct:.1f}%")


if __name__ == "__main__":
    main()
