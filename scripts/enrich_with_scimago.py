"""
Enriquece fuente_metrica con SCImago 2014-2025 y Scopus Source List 2024.

Proceso:
1. Carga data/raw/scimago_2014_2025.csv  → SJR, cuartil, H-index por anio
2. Carga scopus_source_list_2024 (.xlsx) → CiteScore, SNIP (snapshot 2024,
   aplicado a todos los anios por ser el unico disponible)
3. Lee la tabla fuente de PostgreSQL → {issn_normalizado: id_fuente}
4. Por cada anio 2014-2025: cruza por ISSN con fallback de 1 anio para SJR
5. Upsert en fuente_metrica (inserta o actualiza sin borrar lo existente)
6. Imprime cobertura por anio y top ISSNs sin cruce para diagnostico
"""

from __future__ import annotations

import sys
from collections import Counter, defaultdict
from pathlib import Path

import pandas as pd
from sqlalchemy.orm import Session

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from config.db_config import get_session
from src.database.models import Fuente, FuenteMetrica
from src.etl.enrich_sources import _explode_issns, _safe_float, load_scopus_source_list
from src.utils.logger import get_logger
from src.utils.text_normalization import normalize_issn

logger = get_logger(__name__)

SCIMAGO_CSV = ROOT / "data" / "raw" / "scimago_2014_2025.csv"
YEARS = list(range(2014, 2026))


# ---------------------------------------------------------------------------
# Localizar Scopus Source List
# ---------------------------------------------------------------------------

def _find_scopus_sl() -> Path:
    candidates = [
        ROOT / "data" / "raw" / "scopus_source_list_2024.xlsx",
        ROOT / "data" / "external" / "scopus_source_list_2024.xlsx",
    ]
    for p in candidates:
        if p.exists():
            return p
    # Busqueda flexible
    for folder in [ROOT / "data" / "raw", ROOT / "data" / "external"]:
        for f in folder.glob("*.xlsx"):
            if "scopus" in f.name.lower() and "source" in f.name.lower():
                return f
    raise FileNotFoundError(
        "No se encontro Scopus Source List en data/raw/ ni data/external/"
    )


# ---------------------------------------------------------------------------
# Cargar SCImago combinado
# ---------------------------------------------------------------------------

def load_scimago_combined(path: Path) -> dict[tuple[int, str], dict]:
    """Carga el CSV combinado y retorna {(anio, issn_norm): {sjr, cuartil, h_index}}.

    Cuando un ISSN aparece varias veces en el mismo anio (explosion de
    multi-ISSN), conserva la fila con mayor rango (menor Rank).
    """
    print(f"Cargando SCImago: {path.name} ...")
    df = pd.read_csv(path, low_memory=False)
    print(f"  {len(df):,} filas, anios {df['Year'].min()}-{df['Year'].max()}")

    # Explotar ISSNs multiples
    df["_issn_list"] = df["Issn"].apply(_explode_issns)
    df = df.explode("_issn_list")
    df = df[df["_issn_list"].notna()].copy()
    df = df.rename(columns={"_issn_list": "issn_norm"})

    # Convertir SJR
    df["sjr_float"] = df["SJR"].apply(_safe_float)

    # Normalizar cuartil
    df["cuartil"] = (
        df["SJR Best Quartile"].astype(str).str.strip().str.upper()
    )
    valid_q = {"Q1", "Q2", "Q3", "Q4"}
    df.loc[~df["cuartil"].isin(valid_q), "cuartil"] = None

    # Ordenar para que menor Rank quede primero
    if "Rank" in df.columns:
        df = df.sort_values(["Year", "issn_norm", "Rank"])

    lookup: dict[tuple[int, str], dict] = {}
    for _, row in df.iterrows():
        key = (int(row["Year"]), str(row["issn_norm"]))
        if key not in lookup:
            lookup[key] = {
                "sjr":     row["sjr_float"],
                "cuartil": row["cuartil"] if pd.notna(row["cuartil"]) else None,
                "h_index": int(row["H index"]) if pd.notna(row["H index"]) else None,
            }

    issns_unicos = len({k[1] for k in lookup})
    print(f"  {len(lookup):,} entradas (anio, ISSN) | {issns_unicos:,} ISSNs unicos")
    return lookup


# ---------------------------------------------------------------------------
# Normalizar ISSN desde entero de Excel (zero-pad a 8 digitos)
# ---------------------------------------------------------------------------

def _normalize_issn_excel(value: object) -> str | None:
    """Normaliza un ISSN que puede haber sido leido como entero por Excel.

    Excel elimina ceros iniciales (p.ej. '00181390' -> 181390).
    Este wrapper zero-padea a 8 digitos antes de llamar normalize_issn.
    """
    if pd.isna(value):
        return None
    cleaned = str(value).strip()
    # Eliminar posible decimal de float: "181390.0" -> "181390"
    if "." in cleaned:
        cleaned = cleaned.split(".")[0]
    # Eliminar guiones/espacios para contar solo digitos
    digits_only = cleaned.replace("-", "").replace(" ", "")
    if digits_only.isdigit() and len(digits_only) < 8:
        digits_only = digits_only.zfill(8)
        cleaned = digits_only
    return normalize_issn(cleaned)


# ---------------------------------------------------------------------------
# Cargar Scopus CiteScore (archivo de metricas, no lista de titulos)
# ---------------------------------------------------------------------------

def load_scopus_combined(path: Path) -> dict[str, dict]:
    """Retorna {issn_norm: {citescore, snip}} desde archivo de CiteScore de Scopus.

    Acepta el formato estandar del archivo CiteScore exportado desde Scopus
    (columnas 'CiteScore', 'SNIP', 'ISSN', 'EISSN').

    Si el archivo no contiene columnas de metricas (CiteScore/SNIP), lanza
    un ValueError explicativo para que el usuario sepa que archivo descargar.
    """
    print(f"Cargando archivo de metricas Scopus: {path.name} ...")

    if path.suffix.lower() in (".xlsx", ".xls"):
        df_raw = pd.read_excel(path)
    else:
        df_raw = pd.read_csv(path, encoding="utf-8-sig")

    df_raw.columns = df_raw.columns.str.strip()

    # Detectar columnas de metricas (flexibles: "CiteScore 2024", "CiteScore", etc.)
    cs_col   = next((c for c in df_raw.columns if c.lower().startswith("citescore")), None)
    snip_col = next((c for c in df_raw.columns if c.lower().startswith("snip")), None)

    if cs_col is None and snip_col is None:
        cols_found = list(df_raw.columns[:15])
        raise ValueError(
            f"\nEl archivo '{path.name}' NO contiene columnas CiteScore ni SNIP.\n"
            f"Columnas encontradas: {cols_found}\n\n"
            "Este archivo parece ser la LISTA DE TITULOS de Scopus, no el archivo "
            "de metricas CiteScore.\n"
            "Para obtener CiteScore y SNIP, descarga el archivo desde:\n"
            "  Scopus > Sources > Download > 'CiteScore' (formato Excel/CSV)\n"
            "  o busca 'Scopus CiteScore 2024 Metrics' en la web de Elsevier.\n\n"
            "El script continua SIN CiteScore/SNIP — solo SCImago (SJR/cuartil) "
            "quedara en la BD."
        )

    # Identificar columnas ISSN
    issn_col  = next((c for c in df_raw.columns if c.upper() in ("ISSN", "PRINT ISSN")), None)
    eissn_col = next((c for c in df_raw.columns if c.upper() in ("EISSN", "E-ISSN", "ELECTRONIC ISSN")), None)

    lookup: dict[str, dict] = {}
    matched = 0
    for _, row in df_raw.iterrows():
        cs   = None if cs_col   is None else (None if pd.isna(row[cs_col])   else float(str(row[cs_col]).replace(",", ".")))
        snip = None if snip_col is None else (None if pd.isna(row[snip_col]) else float(str(row[snip_col]).replace(",", ".")))

        if cs is None and snip is None:
            continue

        entry = {"citescore": cs, "snip": snip}
        for col in [issn_col, eissn_col]:
            if col is None:
                continue
            norm = _normalize_issn_excel(row.get(col))
            if norm and norm not in lookup:
                lookup[norm] = entry
                matched += 1

    print(f"  {matched:,} ISSNs con CiteScore/SNIP")
    return lookup


# ---------------------------------------------------------------------------
# Leer fuentes de la BD
# ---------------------------------------------------------------------------

def load_fuentes_db(session: Session) -> tuple[dict[str, int], dict[str, str]]:
    """Retorna (issn_norm->id_fuente, issn_norm->source_title)."""
    fuentes = session.query(Fuente.id_fuente, Fuente.source_title, Fuente.issn).all()
    issn_to_id: dict[str, int] = {}
    issn_to_title: dict[str, str] = {}
    sin_issn = 0
    for f in fuentes:
        if not f.issn:
            sin_issn += 1
            continue
        norm = normalize_issn(f.issn)
        if norm and norm not in issn_to_id:
            issn_to_id[norm] = f.id_fuente
            issn_to_title[norm] = f.source_title or ""
    print(f"  Fuentes en BD: {len(fuentes)} total | "
          f"{len(issn_to_id)} con ISSN valido | {sin_issn} sin ISSN")
    return issn_to_id, issn_to_title


# ---------------------------------------------------------------------------
# Upsert fuente_metrica para un anio
# ---------------------------------------------------------------------------

def upsert_year(
    session: Session,
    anio: int,
    issn_to_id: dict[str, int],
    sci: dict[tuple[int, str], dict],
    scopus: dict[str, dict],
) -> dict:
    """Inserta o actualiza registros en fuente_metrica para un anio dado.

    Retorna estadisticas: matched_sci, matched_scopus, sin_cruce, inserted, updated.
    """
    # Pre-cargar existentes para este anio
    existing: dict[int, FuenteMetrica] = {
        m.id_fuente: m
        for m in session.query(FuenteMetrica).filter_by(anio=anio).all()
    }

    stats = dict(matched_sci=0, matched_scopus=0, sin_cruce=0,
                 inserted=0, updated=0, issns_sin_sci=[])

    for issn_norm, id_fuente in issn_to_id.items():
        # Cruce SCImago: anio exacto → fallback anio-1
        sci_data = sci.get((anio, issn_norm)) or sci.get((anio - 1, issn_norm))
        scopus_data = scopus.get(issn_norm, {})

        sjr     = sci_data["sjr"]     if sci_data else None
        cuartil = sci_data["cuartil"] if sci_data else None
        h_index = sci_data["h_index"] if sci_data else None
        citescore = scopus_data.get("citescore")
        snip      = scopus_data.get("snip")

        has_sci    = sci_data is not None
        has_scopus = citescore is not None or snip is not None

        if not has_sci:
            stats["issns_sin_sci"].append(issn_norm)
        if has_sci:
            stats["matched_sci"] += 1
        if has_scopus:
            stats["matched_scopus"] += 1
        if not has_sci and not has_scopus:
            stats["sin_cruce"] += 1
            continue

        rec = existing.get(id_fuente)
        if rec:
            if sjr      is not None: rec.sjr         = sjr
            if cuartil  is not None: rec.cuartil_sjr  = cuartil
            if citescore is not None: rec.citescore   = citescore
            if snip     is not None: rec.snip         = snip
            rec.fuente_datos = "scimago+scopus_sl"
            stats["updated"] += 1
        else:
            session.add(FuenteMetrica(
                id_fuente=id_fuente,
                anio=anio,
                sjr=sjr,
                cuartil_sjr=cuartil,
                citescore=citescore,
                snip=snip,
                percentil_sjr=None,
                fuente_datos="scimago+scopus_sl",
            ))
            stats["inserted"] += 1

    session.flush()
    return stats


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    print("=" * 60)
    print("ENRIQUECIMIENTO fuente_metrica con SCImago + Scopus SL")
    print("=" * 60)

    # --- Cargar fuentes externas ---
    sci_lookup = load_scimago_combined(SCIMAGO_CSV)

    scopus_lookup: dict[str, dict] = {}
    try:
        sl_path       = _find_scopus_sl()
        scopus_lookup = load_scopus_combined(sl_path)
    except FileNotFoundError as exc:
        print(f"\n  AVISO: {exc}\n  Continuando sin CiteScore/SNIP.")
    except ValueError as exc:
        print(f"\n  AVISO:{exc}\n  Continuando solo con SCImago (SJR/cuartil).")

    # --- Procesar por anio ---
    all_issns_sin_sci: Counter = Counter()
    resumen: list[dict] = []

    with get_session() as session:
        print("\nLeyendo fuentes de la base de datos ...")
        issn_to_id, issn_to_title = load_fuentes_db(session)
        total_fuentes = len(issn_to_id)

        print(f"\nProcesando {len(YEARS)} anios ...\n")

        for anio in YEARS:
            stats = upsert_year(session, anio, issn_to_id, sci_lookup, scopus_lookup)

            pct_sci = stats["matched_sci"] / total_fuentes * 100 if total_fuentes else 0
            pct_sl  = stats["matched_scopus"] / total_fuentes * 100 if total_fuentes else 0

            print(
                f"  {anio}: SCImago {stats['matched_sci']:>4}/{total_fuentes} "
                f"({pct_sci:5.1f}%) | "
                f"CiteScore/SNIP {stats['matched_scopus']:>4}/{total_fuentes} "
                f"({pct_sl:5.1f}%) | "
                f"+{stats['inserted']} ins / {stats['updated']} upd"
            )

            all_issns_sin_sci.update(stats["issns_sin_sci"])
            resumen.append({
                "anio": anio,
                "matched_sci":    stats["matched_sci"],
                "matched_scopus": stats["matched_scopus"],
                "sin_cruce":      stats["sin_cruce"],
                "inserted":       stats["inserted"],
                "updated":        stats["updated"],
            })

    # --- Estadisticas finales ---
    total_ops     = sum(r["inserted"] + r["updated"] for r in resumen)
    pct_sci_prom  = sum(r["matched_sci"] for r in resumen) / (len(YEARS) * total_fuentes) * 100
    pct_sl_prom   = sum(r["matched_scopus"] for r in resumen) / (len(YEARS) * total_fuentes) * 100

    print("\n" + "=" * 60)
    print("RESUMEN FINAL")
    print("=" * 60)
    print(f"  Fuentes en BD con ISSN valido : {total_fuentes}")
    print(f"  Registros upserted (total)    : {total_ops:,}")
    print(f"  Cobertura SCImago (promedio)  : {pct_sci_prom:.1f}%")
    print(f"  Cobertura CiteScore/SNIP      : {pct_sl_prom:.1f}%")

    # ISSNs que nunca cruzaron SCImago en ningun anio
    never_matched = [(issn, cnt) for issn, cnt in all_issns_sin_sci.items()
                     if cnt == len(YEARS)]
    print(f"\n  ISSNs sin cruce SCImago en NINGUN anio: {len(never_matched)}")

    if all_issns_sin_sci:
        print("\n  Top 10 ISSNs con menos cruces SCImago (diagnostico):")
        for issn, miss_count in all_issns_sin_sci.most_common(10):
            title = issn_to_title.get(issn, "")
            match_years = len(YEARS) - miss_count
            print(f"    {issn}  cruces: {match_years}/{len(YEARS)}  [{title[:50]}]")

    pct_sci_minimo = min(r["matched_sci"] for r in resumen) / total_fuentes * 100 if total_fuentes else 0
    if pct_sci_minimo < 50:
        print(
            f"\n  AVISO: el anio con menor cobertura SCImago tiene "
            f"{pct_sci_minimo:.1f}% < 50%. Verificar formato de ISSNs en la BD."
        )
    else:
        print("\n  OK: todos los anios superan 50% de cobertura SCImago.")

    print("\nEnriquecimiento completado. El dashboard lee fuente_metrica — no requiere cambios.")


if __name__ == "__main__":
    main()
