"""Merge SCImago annual CSV files into a single combined dataset."""

import re
from pathlib import Path

import pandas as pd

RAW_DIR = Path(__file__).parent.parent / "data" / "raw"
OUTPUT = RAW_DIR / "scimago_2014_2025.csv"


def load_scimago_files(raw_dir: Path) -> pd.DataFrame:
    files = sorted(raw_dir.glob("scimagojr *.csv"))
    if not files:
        raise FileNotFoundError(f"No SCImago files found in {raw_dir}")

    frames = []
    column_sets = {}

    for f in files:
        match = re.search(r"(\d{4})", f.name)
        if not match:
            print(f"  SKIP (no year found): {f.name}")
            continue
        year = int(match.group(1))

        df = pd.read_csv(f, sep=";", encoding="utf-8", low_memory=False)
        df.insert(0, "Year", year)
        column_sets[year] = list(df.columns)
        frames.append(df)
        print(f"  {f.name}: {len(df):>6,} rows, {df.shape[1]} columns")

    # Check column consistency across years (excluding 'Year' itself)
    base_cols = [c for c in column_sets[min(column_sets)] if c != "Year"]
    inconsistent = {}
    for year, cols in column_sets.items():
        year_cols = [c for c in cols if c != "Year"]
        if year_cols != base_cols:
            missing = set(base_cols) - set(year_cols)
            extra = set(year_cols) - set(base_cols)
            inconsistent[year] = {"missing": missing, "extra": extra}

    if inconsistent:
        print("\n  Column differences detected:")
        for year, diff in inconsistent.items():
            if diff["missing"]:
                print(f"    {year} missing: {diff['missing']}")
            if diff["extra"]:
                print(f"    {year} extra:   {diff['extra']}")
    else:
        print("\n  All years share identical columns.")

    combined = pd.concat(frames, ignore_index=True)
    return combined


def main():
    print("Loading SCImago files...")
    combined = load_scimago_files(RAW_DIR)

    print(f"\nTotal rows combined: {len(combined):,}")
    print(f"Columns ({len(combined.columns)}): {list(combined.columns)}")
    print(f"\nRows per year:\n{combined.groupby('Year').size().to_string()}")

    combined.to_csv(OUTPUT, index=False, encoding="utf-8")
    print(f"\nSaved to: {OUTPUT}")


if __name__ == "__main__":
    main()
