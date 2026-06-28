"""
motor_calculo/engine.py
=======================
Motor de cálculo de precios para el mercado eléctrico español.

Granularidad determinada por la TARIFA (ZFA_T_TARIFAS.INDEXADO):
  - Tarifa INDEXADO=X  → todos sus atributos generan 96 filas/día (cuarto-horario)
      · Atributo indexado     → 96 valores distintos (OMIE, pérdidas...)
      · Atributo no indexado  → 96 filas con el mismo valor repetido
  - Tarifa sin INDEXADO → 1 fila/día, PROF_TIME = 00:00:00
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

    for campo in ("CALC_INI", "CALC_FIN"):
        v = contrato.get(campo)
        if v is not None and not isinstance(v, date):
            contrato[campo] = pd.Timestamp(v).date()

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
    slots_utc = slots_cet.tz_localize(TZ_CET).tz_convert(TZ_UTC)
    result = np.zeros(len(slots_cet))
    try:
        perfil_data = profval_df.loc[perfil]
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
# FUNCIÓN ATÓMICA vectorizada
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
    fecha_ini:    date = contrato["CALC_INI"]
    fecha_fin:    date = contrato["CALC_FIN"]
    producto:     str  = contrato["PRODUCTO"]
    tarifatr:     str  = contrato["TARIFATR"]
    geozona:      str  = contrato["GEOZONA"]
    version:      str  = contrato["VERSION"]
    ventana_cont        = contrato.get("VENTANA")
    atributos_com: dict = contrato["atributos_com"]

    prod_info     = tables["ZFA_T_PRODUCTOS"].loc[producto]
    tipo_energia  = int(prod_info["TIPO_ENERGIA"])
    tipo_producto = str(prod_info["TIPO_PRODUCTO"])

    atr_info_df   = tables["ZFA_T_ATRIBUTOS"]
    tarifas_df    = tables["ZFA_T_TARIFAS"]
    perfiles_df   = tables["ZFA_T_PERFILES"].reset_index()
    profval_df    = tables["EPROFVAL15"]
    matriz_reg_df = tables["ZFA_T_MATRIZ_REG"].reset_index()
    prod_atrib_df = tables["ZFA_T_PROD_ATRIB"]
    epreih_df     = tables["EPREIH"]
    conceptos     = prod_atrib_df[prod_atrib_df["PRODUCTO"] == producto].copy()

    # Serie temporal cuarto-horaria CET (para tarifas indexadas)
    slots_qh = pd.date_range(
        start=datetime.combine(fecha_ini, datetime.min.time()),
        end=datetime.combine(fecha_fin,   datetime.min.time()) + timedelta(days=1),
        freq="15min", inclusive="left",
    )
    n_qh      = len(slots_qh)
    fechas_qh = slots_qh.date
    horas_qh  = slots_qh.strftime("%H:%M:%S")

    # Serie temporal diaria (para tarifas no indexadas)
    dias       = pd.date_range(start=fecha_ini, end=fecha_fin, freq="D")
    fechas_dia = dias.date
    horas_dia  = np.full(len(dias), "00:00:00")
    n_dia      = len(dias)

    # Cache de perfiles cuarto-horarios
    _cache: dict[int, np.ndarray] = {}

    def get_serie(perfil: int) -> np.ndarray:
        if perfil not in _cache:
            _cache[perfil] = build_perfil_series(profval_df, perfil, slots_qh)
        return _cache[perfil].copy()

    def get_perfil_num(indice: str) -> int | None:
        rows = perfiles_df[
            (perfiles_df["INDICE"]     == indice) &
            (perfiles_df["TARIFA_ATR"] == tarifatr) &
            (perfiles_df["GEOZONA"]    == geozona)
        ]
        if rows.empty:
            rows = perfiles_df[perfiles_df["INDICE"] == indice]
        return int(rows.iloc[0]["PERFIL"]) if not rows.empty else None

    def get_preis(atributo: str) -> str | None:
        rows = matriz_reg_df[
            (matriz_reg_df["PAIS"]          == "ES") &
            (matriz_reg_df["TIPO_ENERGIA"]  == tipo_energia) &
            (matriz_reg_df["TARIFA_ACCESO"] == tarifatr) &
            (matriz_reg_df["ATRIBUTO"]      == atributo)
        ]
        return str(rows.iloc[0]["PRECIO_REGULADO"]) if not rows.empty else None

    def get_precio_epreih(preis: str, fecha: date) -> float:
        ts   = pd.Timestamp(fecha)
        mask = (epreih_df["PREIS"]    == preis) & \
               (epreih_df["ABDATUM"]  <= ts)    & \
               (epreih_df["BISDATUM"] >= ts)
        r = epreih_df.loc[mask, "PREISBTR"]
        return float(r.iloc[0]) if not r.empty else 0.0

    def get_prb(atributo: str, ventana=None) -> float:
        return get_precio_base(
            tables, pais="ES",
            tipo_energia=tipo_energia,
            tipo_producto=tipo_producto,
            tarifa_acceso=tarifatr,
            version=version,
            ventana=ventana,
            atributo=atributo,
        ) or 0.0

    # ---- Perfiles comunes cuarto-horarios ----
    pf_periodos_num = get_perfil_num("E_PERIODOS")
    if pf_periodos_num:
        periodos_serie = np.round(get_serie(pf_periodos_num) * DIVISOR_PERFIL).astype(int)
    else:
        periodos_serie = np.ones(n_qh, dtype=int)

    pf_perdidas_num = get_perfil_num("E_PERDIDAS")
    perdidas_serie  = get_serie(pf_perdidas_num) if pf_perdidas_num else np.zeros(n_qh)

    tasa_mun_valor  = get_precio_epreih("E_TASAMUNI", fecha_ini)
    inc_tasas_valor = get_prb("E_PBINCMU")

    inc_perdidas_por_periodo = {p: get_prb(f"E_PBINCMP{p}") for p in range(1, 7)}
    inc_perdidas_serie = np.array([
        inc_perdidas_por_periodo.get(int(per), 0.0) for per in periodos_serie
    ])

    all_dfs = []

    for _, row in conceptos.iterrows():
        tarifa_precio  = row["TARIFA_PRECIO"]
        atributo       = row["ATRIBUTO"]
        flag_perdidas  = row.get("PERDIDAS")
        flag_tasa      = row.get("TASA_MUNICIPAL")
        excluir        = row.get("EXCLUIR")

        # Info de la tarifa
        try:
            tarifa_info = tarifas_df.loc[tarifa_precio]
        except KeyError:
            continue

        # EXPLICITO: solo calcular si el atributo tiene valor en INPUT
        explicito = tarifa_info.get("EXPLICITO")
        if pd.notna(explicito) and explicito:
            if atributos_com.get(str(explicito), 0.0) == 0.0:
                continue

        # INDEXADO de la tarifa → determina granularidad
        tarifa_indexada = str(tarifa_info.get("INDEXADO", "")) == "X"

        # VENTANA: campo del INPUT a usar para PRB
        ventana_campo = tarifa_info.get("VENTANA")
        if pd.notna(ventana_campo) and ventana_campo:
            ventana = contrato.get(str(ventana_campo))
            if ventana is None or (isinstance(ventana, float) and math.isnan(ventana)):
                ventana = None
        else:
            ventana = ventana_cont

        # Info del atributo
        try:
            atr_info = atr_info_df.loc[atributo]
        except KeyError:
            continue

        matriz_precio     = str(atr_info.get("MATRIZ_PRECIO", "COM"))
        atr_indexado      = str(atr_info.get("INDEXADO", "")) == "X"
        atrib_fatomic_raw = atr_info.get("ATRIB_FATOMIC")
        atrib_fatomic     = None if pd.isna(atrib_fatomic_raw) else str(atrib_fatomic_raw)
        es_portador       = atrib_fatomic is not None
        multiplicador     = 0.0 if es_portador else 100.0

        if tarifa_indexada:
            # ---- TARIFA INDEXADA → 96 filas/día ----
            n      = n_qh
            fechas = fechas_qh
            horas  = horas_qh

            preis_val  = None
            perfil_num = None
            precio_arr = np.zeros(n)

            if matriz_precio == "COM":
                precio_arr[:] = atributos_com.get(atributo, 0.0)

            elif matriz_precio == "PRB":
                precio_arr[:] = get_prb(atributo, ventana)

            elif matriz_precio == "REG":
                preis_val = get_preis(atributo)
                if preis_val:
                    if atr_indexado:
                        perfil_num = get_perfil_num(preis_val)
                        if perfil_num:
                            precio_arr = get_serie(perfil_num)
                    else:
                        precios_dia = {
                            f: get_precio_epreih(preis_val, f)
                            for f in sorted(set(fechas_dia))
                        }
                        precio_arr = np.array([precios_dia[f] for f in fechas_qh])

            perdidas_arr     = perdidas_serie.copy() if flag_perdidas == "X" else np.zeros(n)
            inc_perdidas_arr = inc_perdidas_serie.copy() if flag_perdidas == "X" else np.zeros(n)
            tasa_arr         = np.full(n, tasa_mun_valor) if flag_tasa == "X" else np.zeros(n)
            inc_tasas        = inc_tasas_valor if flag_tasa == "X" else 0.0
            apunt_arr        = np.zeros(n)

        else:
            # ---- TARIFA NO INDEXADA → 1 fila/día ----
            n      = n_dia
            fechas = fechas_dia
            horas  = horas_dia

            preis_val  = None
            perfil_num = None
            precio_arr = np.zeros(n)

            if matriz_precio == "COM":
                precio_arr[:] = atributos_com.get(atributo, 0.0)

            elif matriz_precio == "PRB":
                precio_arr[:] = get_prb(atributo, ventana)

            elif matriz_precio == "REG":
                preis_val = get_preis(atributo)
                if preis_val:
                    precio_arr = np.array([
                        get_precio_epreih(preis_val, f) for f in fechas_dia
                    ])

            if flag_perdidas == "X" and pf_perdidas_num:
                perdidas_diaria  = []
                inc_per_diaria   = []
                for i in range(n_dia):
                    idx_ini = i * 96
                    idx_fin = idx_ini + 96
                    perdidas_diaria.append(perdidas_serie[idx_ini:idx_fin].mean())
                    inc_per_diaria.append(inc_perdidas_serie[idx_ini:idx_fin].mean())
                perdidas_arr     = np.array(perdidas_diaria)
                inc_perdidas_arr = np.array(inc_per_diaria)
            else:
                perdidas_arr     = np.zeros(n)
                inc_perdidas_arr = np.zeros(n)

            tasa_arr  = np.full(n, tasa_mun_valor) if flag_tasa == "X" else np.zeros(n)
            inc_tasas = inc_tasas_valor if flag_tasa == "X" else 0.0
            apunt_arr = np.zeros(n)

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

        df_atr = pd.DataFrame({
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
        all_dfs.append(df_atr)

    return pd.concat(all_dfs, ignore_index=True) if all_dfs else pd.DataFrame()
