"""
motor_calculo/engine.py
=======================
Motor de cálculo de precios cuarto-horarios para el mercado eléctrico español.

Flujo principal:
  BUCLE 1 — Para cada (tarifa_precio, atributo) del producto:
    - Resolver PRECIO según MATRIZ_PRECIO:
        PRB  → ZFA_T_MATRIZ_PRB
        REG  → ZFA_T_MATRIZ_REG → EPREIH (no indexado) o EPROFVAL15 (indexado)
        COM  → valor del INPUT (0 si no viene)
    - Filtrar tarifa si EXPLICITO está informado y el atributo no tiene valor en INPUT
    - Usar VENTANA de ZFA_T_TARIFAS para seleccionar fila en ZFA_T_MATRIZ_PRB

  BUCLE 2 — Para los atributos con flags de multiplicadores:
    - PERDIDAS=X      → rellenar PERDIDAS con perfil E_PERDIDAS (cuarto-horario)
                        rellenar INCR_PERDIDAS con E_PBINCMP{periodo} de PRB
    - TASA_MUNICIPAL=X → rellenar TASA con E_TASAMUNI de EPREIH
                         rellenar INCR_TASAS con E_PBINCMU de PRB
    - APUNTAMIENTOS=X → rellenar APUNT con perfil de apuntamientos (cuarto-horario)

    Los atributos con ATRIB_FATOMIC en ZFA_T_ATRIBUTOS son inputs de la fórmula
    atómica de otros atributos → su PRECIO_FINAL = 0 (no generan importe propio).

  FUNCIÓN ATÓMICA (traducción exacta de zfa_fm_precio_atomic ABAP):
    PRECIO_FINAL = PRECIO
                 × (1 + PERDIDAS/100 + INC_PERDIDAS/100)
                 × (1 + TASA/100 + INC_TASAS/100)
                 × (1 + APUNT/100)
                 × (MULTIPLICADOR/100)
    Redondeado a 6 decimales.
"""

import math
import pandas as pd
import numpy as np
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from db import load_all, load_input, get_precio_base

DIVISOR_PERFIL = 1e10
TZ_CET = ZoneInfo("Europe/Madrid")
TZ_UTC = ZoneInfo("UTC")


# ---------------------------------------------------------------------------
# PARSE INPUT
# ---------------------------------------------------------------------------

def parse_input(df_input: pd.DataFrame) -> dict:
    """
    Lee el DataFrame del INPUT y devuelve un dict con los datos del contrato.

    Campos de cabecera: CALC_INI, CALC_FIN, PRODUCTO, TARIFATR, GEOZONA,
                        VERSION, VENTANA, VENTANA_C1
    El resto son atributos COM con sus valores numéricos (0 si no informado).
    """
    campos_contrato = {"CALC_INI", "CALC_FIN", "PRODUCTO", "TARIFATR",
                       "GEOZONA", "VERSION", "VENTANA", "VENTANA_C1"}
    contrato = {}
    atributos_com = {}

    for _, row in df_input.iterrows():
        atr = str(row["ATRIBUTO"]).strip()
        val = row["VALOR"]
        if atr in campos_contrato:
            contrato[atr] = val
        else:
            try:
                atributos_com[atr] = float(val) if pd.notna(val) else 0.0
            except (TypeError, ValueError):
                atributos_com[atr] = 0.0

    # Normalizar fechas
    for campo in ("CALC_INI", "CALC_FIN"):
        v = contrato.get(campo)
        if v is not None and not isinstance(v, date):
            contrato[campo] = pd.Timestamp(v).date()

    # Normalizar VENTANA: NaN / vacío → None
    for campo in ("VENTANA", "VENTANA_C1"):
        v = contrato.get(campo)
        if v is None or (isinstance(v, float) and math.isnan(v)):
            contrato[campo] = None

    contrato["atributos_com"] = atributos_com
    return contrato


# ---------------------------------------------------------------------------
# PERFILES CUARTO-HORARIOS (UTC → CET)
# ---------------------------------------------------------------------------

def build_perfil_series(profval_df: pd.DataFrame, perfil: int,
                        slots_cet: pd.DatetimeIndex) -> np.ndarray:
    """
    Para cada timestamp CET (naive) de slots_cet, convierte a UTC y lee el
    valor del perfil en EPROFVAL15. Divide por 1e10.
    """
    slots_utc = slots_cet.tz_localize(TZ_CET).tz_convert(TZ_UTC)
    result = np.zeros(len(slots_cet))

    try:
        perfil_data = profval_df.loc[perfil]   # DataFrame: VALUEDAY × VAL_cols
    except KeyError:
        return result

    for i, dt_utc in enumerate(slots_utc):
        col = f"VAL{dt_utc.hour:02d}{dt_utc.minute:02d}"
        ts  = pd.Timestamp(dt_utc.date())
        try:
            result[i] = float(perfil_data.loc[ts, col]) / DIVISOR_PERFIL
        except (KeyError, TypeError):
            result[i] = 0.0

    return result


# ---------------------------------------------------------------------------
# FUNCIÓN ATÓMICA — traducción exacta del ABAP zfa_fm_precio_atomic
# ---------------------------------------------------------------------------

def precio_final_atomico_vec(
    precio:        np.ndarray,
    perdidas:      np.ndarray,
    inc_perdidas:  np.ndarray,
    tasa:          np.ndarray,
    inc_tasas:     float,
    apuntamientos: np.ndarray,
    multiplicador: float,
    precio_min:    float,
    precio_max:    float,
) -> np.ndarray:
    p = precio.copy()
    if precio_min != 0:
        p = np.where(p < precio_min, precio_min, p)
    if precio_max != 0:
        p = np.where(p > precio_max, precio_max, p)

    mult_p = 1 + perdidas      / 100 + inc_perdidas / 100
    mult_t = 1 + tasa          / 100 + inc_tasas    / 100
    mult_a = 1 + apuntamientos / 100
    mult_g = multiplicador     / 100

    return np.round(p * mult_p * mult_t * mult_a * mult_g, 6)


# ---------------------------------------------------------------------------
# MOTOR PRINCIPAL
# ---------------------------------------------------------------------------

def calcular(tables: dict, contrato: dict) -> pd.DataFrame:
    """
    Ejecuta el motor de cálculo y devuelve el DataFrame de detalle cuarto-horario.
    """
    # ---- Datos del contrato ----
    fecha_ini:    date = contrato["CALC_INI"]
    fecha_fin:    date = contrato["CALC_FIN"]
    producto:     str  = contrato["PRODUCTO"]
    tarifatr:     str  = contrato["TARIFATR"]
    geozona:      str  = contrato["GEOZONA"]
    version:      str  = contrato["VERSION"]
    ventana_cont        = contrato.get("VENTANA")     # ventana del contrato (o None)
    atributos_com: dict = contrato["atributos_com"]

    # ---- Info del producto ----
    prod_info     = tables["ZFA_T_PRODUCTOS"].loc[producto]
    tipo_energia  = int(prod_info["TIPO_ENERGIA"])     # 1=luz, 2=gas
    tipo_producto = str(prod_info["TIPO_PRODUCTO"])    # FIJ, IND, HYB...

    # ---- Tablas de configuración ----
    atr_info_df   = tables["ZFA_T_ATRIBUTOS"]          # indexed by ATRIBUTO
    tarifas_df    = tables["ZFA_T_TARIFAS"]            # indexed by TARIFA_PRECIO
    perfiles_df   = tables["ZFA_T_PERFILES"].reset_index()
    profval_df    = tables["EPROFVAL15"]               # indexed by (PROFILE, VALUEDAY)
    matriz_reg_df = tables["ZFA_T_MATRIZ_REG"].reset_index()
    prod_atrib_df = tables["ZFA_T_PROD_ATRIB"]
    epreih_df     = tables["EPREIH"]

    # Filas de ZFA_T_PROD_ATRIB para este producto
    conceptos = prod_atrib_df[prod_atrib_df["PRODUCTO"] == producto].copy()

    # ---- Serie temporal CET (naive) ----
    slots = pd.date_range(
        start=datetime.combine(fecha_ini, datetime.min.time()),
        end=datetime.combine(fecha_fin,   datetime.min.time()) + timedelta(days=1),
        freq="15min", inclusive="left",
    )
    n      = len(slots)
    fechas = slots.date
    horas  = slots.strftime("%H:%M:%S")

    # ---- Cache de series de perfiles ----
    _cache: dict[int, np.ndarray] = {}

    def get_serie(perfil: int) -> np.ndarray:
        if perfil not in _cache:
            _cache[perfil] = build_perfil_series(profval_df, perfil, slots)
        return _cache[perfil].copy()

    # ---- Helpers ----

    def get_perfil_num(indice: str) -> int | None:
        """Obtiene el número de perfil para un INDICE dado tarifa ATR y geozona."""
        rows = perfiles_df[
            (perfiles_df["INDICE"]     == indice) &
            (perfiles_df["TARIFA_ATR"] == tarifatr) &
            (perfiles_df["GEOZONA"]    == geozona)
        ]
        if rows.empty:
            rows = perfiles_df[perfiles_df["INDICE"] == indice]
        return int(rows.iloc[0]["PERFIL"]) if not rows.empty else None

    def get_preis(atributo: str) -> str | None:
        """Obtiene el código PRECIO_REGULADO de ZFA_T_MATRIZ_REG."""
        rows = matriz_reg_df[
            (matriz_reg_df["PAIS"]          == "ES") &
            (matriz_reg_df["TIPO_ENERGIA"]  == tipo_energia) &
            (matriz_reg_df["TARIFA_ACCESO"] == tarifatr) &
            (matriz_reg_df["ATRIBUTO"]      == atributo)
        ]
        return str(rows.iloc[0]["PRECIO_REGULADO"]) if not rows.empty else None

    def get_precio_epreih(preis: str, fecha: date) -> float:
        """Obtiene el precio regulado vigente en EPREIH para una fecha."""
        ts   = pd.Timestamp(fecha)
        mask = (epreih_df["PREIS"]   == preis) & \
               (epreih_df["ABDATUM"] <= ts)    & \
               (epreih_df["BISDATUM"] >= ts)
        r = epreih_df.loc[mask, "PREISBTR"]
        return float(r.iloc[0]) if not r.empty else 0.0

    def get_prb(atributo: str, ventana=None) -> float:
        """Obtiene el PRECIO_BASE de ZFA_T_MATRIZ_PRB."""
        return get_precio_base(
            tables,
            pais="ES",
            tipo_energia=tipo_energia,
            tipo_producto=tipo_producto,
            tarifa_acceso=tarifatr,
            version=version,
            ventana=ventana,
            atributo=atributo,
        ) or 0.0

    # ---- Perfiles comunes precalculados ----

    # Perfil de periodos horarios (P1-P6) — cuarto-horario
    # Se usa para saber qué E_PBINCMP{p} aplicar como incremento de pérdidas
    pf_periodos_num = get_perfil_num("E_PERIODOS")
    if pf_periodos_num:
        periodos_serie_raw = get_serie(pf_periodos_num)
        # Los valores en EPROFVAL15 están divididos por 1e10 pero representan
        # enteros 1-6 → reescalar multiplicando por 1e10
        periodos_serie = np.round(periodos_serie_raw * DIVISOR_PERFIL).astype(int)
    else:
        periodos_serie = np.ones(n, dtype=int)

    # Perfil de pérdidas cuarto-horario
    pf_perdidas_num = get_perfil_num("E_PERDIDAS")
    perdidas_serie  = get_serie(pf_perdidas_num) if pf_perdidas_num else np.zeros(n)

    # Tasa municipal fija desde EPREIH
    tasa_mun_valor = get_precio_epreih("E_TASAMUNI", fecha_ini)

    # Incrementos de pérdidas por periodo desde PRB (E_PBINCMP1..6)
    inc_perdidas_por_periodo = {
        p: get_prb(f"E_PBINCMP{p}") for p in range(1, 7)
    }
    # Array cuarto-horario de incremento de pérdidas según periodo
    inc_perdidas_serie = np.array([
        inc_perdidas_por_periodo.get(int(per), 0.0)
        for per in periodos_serie
    ])

    # Incremento de tasas municipales desde PRB (único, sin periodo)
    inc_tasas_valor = get_prb("E_PBINCMU")

    # ---- BUCLE 1: calcular PRECIO para cada (tarifa_precio, atributo) ----
    resultados: list[dict] = []

    for _, row in conceptos.iterrows():
        tarifa_precio  = row["TARIFA_PRECIO"]
        atributo       = row["ATRIBUTO"]
        flag_perdidas  = row.get("PERDIDAS")       # "X" o NaN
        flag_tasa      = row.get("TASA_MUNICIPAL") # "X" o NaN
        flag_apunt     = row.get("APUNTAMIENTOS")  # "X" o NaN
        excluir        = row.get("EXCLUIR")

        # Info de la tarifa
        try:
            tarifa_info = tarifas_df.loc[tarifa_precio]
        except KeyError:
            continue

        # EXPLICITO: si está informado, la tarifa solo aplica si ese atributo
        # tiene valor en el INPUT (distinto de vacío / NaN)
        explicito = tarifa_info.get("EXPLICITO")
        if pd.notna(explicito) and explicito:
            val_exp = atributos_com.get(str(explicito), 0.0)
            if val_exp == 0.0:
                continue   # tarifa no aplica para este contrato

        # VENTANA: qué campo del INPUT usar para la ventana de precios base
        # La columna VENTANA de ZFA_T_TARIFAS indica el nombre del campo INPUT
        ventana_campo = tarifa_info.get("VENTANA")
        if pd.notna(ventana_campo) and ventana_campo:
            ventana = contrato.get(str(ventana_campo))
            if ventana is None or (isinstance(ventana, float) and math.isnan(ventana)):
                ventana = None
        else:
            ventana = ventana_cont   # fallback a la ventana del contrato

        # Info del atributo
        try:
            atr_info = atr_info_df.loc[atributo]
        except KeyError:
            continue

        matriz_precio     = str(atr_info.get("MATRIZ_PRECIO", "COM"))
        indexado          = str(atr_info.get("INDEXADO", "")) == "X"
        atrib_fatomic_raw = atr_info.get("ATRIB_FATOMIC")
        atrib_fatomic     = None if pd.isna(atrib_fatomic_raw) else str(atrib_fatomic_raw)

        # Atributos con ATRIB_FATOMIC son inputs de la fórmula atómica de otros
        # → generan PRECIO pero su PRECIO_FINAL = 0 (no producen importe)
        es_portador = atrib_fatomic is not None

        # ---- Resolver PRECIO ----
        preis_val  = None
        perfil_num = None
        precio_arr = np.zeros(n)

        if matriz_precio == "COM":
            # Valor directo del INPUT; 0 si no viene informado
            precio_arr[:] = atributos_com.get(atributo, 0.0)

        elif matriz_precio == "PRB":
            # Precio base de ZFA_T_MATRIZ_PRB
            precio_arr[:] = get_prb(atributo, ventana)

        elif matriz_precio == "REG":
            # 1. Obtener etiqueta de precio regulado
            preis_val = get_preis(atributo)
            if preis_val:
                if indexado:
                    # 2a. Indexado → ZFA_T_PERFILES → EPROFVAL15 (UTC→CET)
                    perfil_num = get_perfil_num(preis_val)
                    if perfil_num:
                        precio_arr = get_serie(perfil_num)
                else:
                    # 2b. No indexado → EPREIH (precio fijo, puede cambiar vigencia)
                    precios_dia = {
                        f: get_precio_epreih(preis_val, f)
                        for f in sorted(set(fechas))
                    }
                    precio_arr = np.array([precios_dia[f] for f in fechas])

        # ---- BUCLE 2: multiplicadores cuarto-horarios ----

        # Pérdidas (cuarto-horarias del perfil E_PERDIDAS)
        if flag_perdidas == "X":
            perdidas_arr     = perdidas_serie.copy()
            inc_perdidas_arr = inc_perdidas_serie.copy()
        else:
            perdidas_arr     = np.zeros(n)
            inc_perdidas_arr = np.zeros(n)

        # Tasa municipal (valor fijo de EPREIH + incremento de PRB)
        if flag_tasa == "X":
            tasa_arr    = np.full(n, tasa_mun_valor)
            inc_tasas   = inc_tasas_valor
        else:
            tasa_arr    = np.zeros(n)
            inc_tasas   = 0.0

        # Apuntamientos — por implementar según producto (de momento 0)
        apunt_arr = np.zeros(n)

        # Multiplicador efectivo:
        # - Atributos portadores (ATRIB_FATOMIC informado) → 0 (no generan importe)
        # - Resto → 100 (por defecto; PRECIO_MIN/MAX/MULT de tabla ignorados por ahora)
        multiplicador = 0.0 if es_portador else 100.0

        # ---- Función atómica ----
        precio_final_arr = precio_final_atomico_vec(
            precio=precio_arr,
            perdidas=perdidas_arr,
            inc_perdidas=inc_perdidas_arr,
            tasa=tasa_arr,
            inc_tasas=inc_tasas,
            apuntamientos=apunt_arr,
            multiplicador=multiplicador,
            precio_min=0.0,
            precio_max=0.0,
        )

        resultados.append({
            "PRODUCTO":            producto,
            "TARIFA_PRECIO":       tarifa_precio,
            "ATRIBUTO":            atributo,
            "PROF_DATE":           fechas,
            "PROF_TIME":           horas,
            "TIPO_ENERGIA":        tipo_energia,
            "TIPO_PRODUCTO":       tipo_producto,
            "TARIFA_ACCESO":       tarifatr,
            "VERSION":             version,
            "VENTANA":             ventana,
            "GEOZONA":             geozona,
            "EXCLUIR":             excluir,
            "PREIS":               preis_val,
            "PERFIL":              perfil_num,
            "ATRIB_FATOMIC":       atrib_fatomic,
            "PERDIDAS":            perdidas_arr,
            "INCERMENTO_PERDIDAS": inc_perdidas_arr,
            "TASA_MUNICIPAL":      tasa_arr,
            "INCREMENTO_TASAS":    inc_tasas,
            "APUNTAMIENTOS":       apunt_arr,
            "MULTIPLICADOR":       multiplicador,
            "PRECIO_MIN":          0.0,
            "PRECIO_MAX":          0.0,
            "PRECIO":              precio_arr,
            "PRECIO_FINAL":        precio_final_arr,
        })

    if not resultados:
        return pd.DataFrame()

    # Construir DataFrame final
    dfs = [pd.DataFrame(r) for r in resultados]
    return pd.concat(dfs, ignore_index=True)


# ---------------------------------------------------------------------------
# EJECUCIÓN DIRECTA
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys, time
    from pathlib import Path

    excel = sys.argv[1] if len(sys.argv) > 1 else None
    path  = Path(excel) if excel else Path("MOTOR_CALCULO.xlsx")

    print("Cargando tablas...")
    tables   = load_all(path)
    df_input = load_input(path)
    contrato = parse_input(df_input)
    print(f"  {contrato['PRODUCTO']} | {contrato['CALC_INI']} → {contrato['CALC_FIN']}")
    print(f"  Tarifa ATR: {contrato['TARIFATR']} | Geozona: {contrato['GEOZONA']}")
    print(f"  VERSION: {contrato['VERSION']} | VENTANA: {contrato.get('VENTANA')}")

    t0 = time.time()
    resultado = calcular(tables, contrato)
    print(f"\n  {len(resultado):,} filas calculadas en {time.time()-t0:.1f}s")

    df_out = pd.read_excel(path, sheet_name="OUTPUT")
    df_out["PROF_DATE"]    = pd.to_datetime(df_out["PROF_DATE"])
    resultado["PROF_DATE"] = pd.to_datetime(resultado["PROF_DATE"])

    # Comparación por atributo
    for atr in ["E_CROMIE", "E_CRPCAPP1", "E_CRVATRP1", "E_CR%PER", "E_CRSSAA"]:
        cols = ["PROF_DATE", "PROF_TIME", "PERDIDAS", "INCERMENTO_PERDIDAS",
                "TASA_MUNICIPAL", "PRECIO", "PRECIO_FINAL"]
        calc = resultado[resultado["ATRIBUTO"] == atr][cols].head(4)
        real = df_out[df_out["ATRIBUTO"]       == atr][cols].head(4)
        if calc.empty and real.empty:
            continue
        print(f"\n=== {atr} ===")
        print("CALC:"); print(calc.to_string(index=False))
        print("REAL:"); print(real.to_string(index=False))

    # Estadística global de diferencias
    merged = resultado.merge(
        df_out[["PROF_DATE", "PROF_TIME", "TARIFA_PRECIO", "ATRIBUTO", "PRECIO_FINAL"]],
        on=["PROF_DATE", "PROF_TIME", "TARIFA_PRECIO", "ATRIBUTO"],
        suffixes=("_calc", "_real"),
    )
    if len(merged):
        diff = (merged["PRECIO_FINAL_calc"] - merged["PRECIO_FINAL_real"]).abs()
        pct_match = (diff < 1e-5).sum() / len(merged) * 100
        print(f"\n=== Diferencias globales ({len(merged):,} filas comparadas) ===")
        print(f"  MAE       : {diff.mean():.8f}")
        print(f"  Max error : {diff.max():.8f}")
        print(f"  Match <1e-5: {pct_match:.1f}%  ({(diff < 1e-5).sum():,} / {len(merged):,})")
