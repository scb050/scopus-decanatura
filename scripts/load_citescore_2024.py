"""
Carga CiteScore 2024 y SNIP en fuente_metrica desde CiteScore 2024 annual values.csv.

Estrategia:
- Deduplica el CSV por Scopus Source ID (una fila por revista, no por sub-area)
- Normaliza Print ISSN y E-ISSN (zero-pad a 8 digitos → XXXX-XXXX)
- Cruza contra fuente.issn para obtener id_fuente
- Actualiza citescore, snip y cuartil_citescore en TODOS los anios
  de fuente_metrica para cada id_fuente que haga match
- Solo toca fuente_metrica; ninguna otra tabla ni archivo
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
from sqlalchemy import text

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from config.db_config import get_engine, get_session
from src.database.models import FuenteMetrica
from src.utils.logger import get_logger
from src.utils.text_normalization import normalize_issn

logger = get_logger(__name__)

CSV_PATH = ROOT / "data" / "raw" / "CiteScore 2024 annual values.csv"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _norm_issn_int(value: object) -> str | None:
    """Normaliza un ISSN que puede llegar como entero (Excel quita ceros iniciales)."""
    if pd.isna(value):
        return None
    s = str(value).strip()
    if "." in s:                      # float como "225002.0"
        s = s.split(".")[0]
    digits = s.replace("-", "").replace(" ", "")
    if digits.isdigit() and len(digits) < 8:
        digits = digits.zfill(8)
    return normalize_issn(digits)


def _safe_float(value: object) -> float | None:
    if pd.isna(value):
        return None
    try:
        return float(str(value).replace(",", "."))
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Cargar y deduplicar CSV
# ---------------------------------------------------------------------------

def load_citescore_csv(path: Path) -> pd.DataFrame:
    """Lee el CSV y retorna una fila por revista (deduplicado por Scopus Source ID)."""
    print(f"Cargando {path.name} ...")
    for enc in ("utf-8-sig", "latin-1", "cp1252"):
        try:
            df = pd.read_csv(path, encoding=enc, low_memory=False)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ValueError(f"No se pudo leer {path.name} con ninguna codificacion conocida")
    print(f"  {len(df):,} filas (antes de deduplicar por revista)")

    df_dedup = df.drop_duplicates(subset=["Scopus Source ID"], keep="first").copy()
    print(f"  {len(df_dedup):,} revistas unicas (Scopus Source ID)")

    df_dedup["issn_print_norm"] = df_dedup["Print ISSN"].apply(_norm_issn_int)
    df_dedup["issn_e_norm"]     = df_dedup["E-ISSN"].apply(_norm_issn_int)
    df_dedup["citescore_val"]   = df_dedup["CiteScore"].apply(_safe_float)
    df_dedup["snip_val"]        = df_dedup["SNIP"].apply(_safe_float)

    # Normalizar cuartil CiteScore (columna "Quartile" en el CSV)
    if "Quartile" in df_dedup.columns:
        df_dedup["cuartil_cs"] = df_dedup["Quartile"].apply(
            lambda v: f"Q{int(v)}" if pd.notna(v) and str(v).strip().isdigit() else None
        )
    else:
        df_dedup["cuartil_cs"] = None

    print(f"  CiteScore no nulo: {df_dedup['citescore_val'].notna().sum():,}")
    print(f"  SNIP no nulo     : {df_dedup['snip_val'].notna().sum():,}")
    return df_dedup


# ---------------------------------------------------------------------------
# Construir lookup ISSN → id_fuente desde la BD
# ---------------------------------------------------------------------------

def build_issn_lookup(engine) -> dict[str, int]:
    """Retorna {issn_normalizado: id_fuente} leyendo la tabla fuente."""
    with engine.connect() as conn:
        df = pd.read_sql(
            text("SELECT id_fuente, issn FROM biblio.fuente WHERE issn IS NOT NULL"),
            conn,
        )
    lookup: dict[str, int] = {}
    for _, row in df.iterrows():
        norm = normalize_issn(str(row["issn"]))
        if norm and norm not in lookup:
            lookup[norm] = int(row["id_fuente"])
    print(f"  Fuentes en BD con ISSN: {len(lookup):,}")
    return lookup


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    print("=" * 60)
    print("CARGA CiteScore 2024 -> fuente_metrica")
    print("=" * 60)

    df_cs = load_citescore_csv(CSV_PATH)

    print("\nConstruyendo lookup ISSN -> id_fuente ...")
    engine = get_engine()
    issn_to_id = build_issn_lookup(engine)

    # Resolver id_fuente para cada revista del CSV (Print ISSN primero, E-ISSN fallback)
    df_cs["id_fuente"] = df_cs["issn_print_norm"].map(issn_to_id)
    mask_missing = df_cs["id_fuente"].isna()
    df_cs.loc[mask_missing, "id_fuente"] = df_cs.loc[mask_missing, "issn_e_norm"].map(issn_to_id)

    matched = df_cs["id_fuente"].notna()
    print(f"\n  Revistas CSV con match en BD : {matched.sum():,} / {len(df_cs):,}")
    print(f"  Revistas CSV sin match       : {(~matched).sum():,}")

    df_match = df_cs[matched].copy()
    df_match["id_fuente"] = df_match["id_fuente"].astype(int)

    # ── Upsert en fuente_metrica ─────────────────────────────────────────────
    print("\nActualizando fuente_metrica ...")

    updated_cs   = 0
    updated_snip = 0
    updated_q    = 0

    # Pre-cargar todos los registros de fuente_metrica de las fuentes que matchearon
    id_fuentes_match = set(df_match["id_fuente"].tolist())

    with get_session() as session:
        registros: list[FuenteMetrica] = (
            session.query(FuenteMetrica)
            .filter(FuenteMetrica.id_fuente.in_(id_fuentes_match))
            .all()
        )
        print(f"  Registros fuente_metrica a actualizar: {len(registros):,}")

        # Mapa id_fuente → metricas
        metricas_map = {
            int(row["id_fuente"]): {
                "citescore": row["citescore_val"],
                "snip":      row["snip_val"],
                "cuartil":   row["cuartil_cs"],
            }
            for _, row in df_match.iterrows()
        }

        for rec in registros:
            m = metricas_map.get(rec.id_fuente)
            if m is None:
                continue
            if m["citescore"] is not None:
                rec.citescore = m["citescore"]
                updated_cs += 1
            if m["snip"] is not None:
                rec.snip = m["snip"]
                updated_snip += 1
            if m["cuartil"] is not None:
                rec.cuartil_citescore = m["cuartil"]
                updated_q += 1

        session.flush()

    # ── Diagnóstico: ISSNs sin match ─────────────────────────────────────────
    df_no_match = df_cs[~matched].copy()
    df_no_match["issn_display"] = df_no_match["issn_print_norm"].fillna(
        df_no_match["issn_e_norm"]
    )

    # ── Estadísticas finales ─────────────────────────────────────────────────
    total_fm = 20912  # ya conocido de la auditoria
    pct_cs   = updated_cs   / total_fm * 100
    pct_snip = updated_snip / total_fm * 100

    print("\n" + "=" * 60)
    print("RESUMEN FINAL")
    print("=" * 60)
    print(f"  Registros actualizados con CiteScore : {updated_cs:,} / {total_fm:,} ({pct_cs:.1f}%)")
    print(f"  Registros actualizados con SNIP      : {updated_snip:,} / {total_fm:,} ({pct_snip:.1f}%)")
    print(f"  Registros actualizados con cuartil CS: {updated_q:,}")

    if pct_cs < 50:
        print(f"\n  AVISO: cobertura CiteScore {pct_cs:.1f}% < 50% — revisar formato ISSN.")
    else:
        print(f"\n  OK: cobertura CiteScore supera 50%.")

    print(f"\n  Top 5 ISSNs sin match (diagnostico):")
    top5 = df_no_match[["issn_display", "Title"]].dropna(subset=["issn_display"]).head(5)
    if top5.empty:
        print("    (todos los ISSNs cruzaron)")
    else:
        for _, row in top5.iterrows():
            print(f"    {row['issn_display']}  [{str(row['Title'])[:55]}]")


if __name__ == "__main__":
    main()
