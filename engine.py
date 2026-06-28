"""
motor_calculo/engine.py
=======================
Motor de cálculo de precios para el mercado eléctrico español.

Flujo:
  1. parse_input()  → calendario diario de atributos (FDESDE/FHASTA)
  2. get_segmentos() → detecta tramos donde PRODUCTO+TARIFATR+GEOZONA+VERSION son constantes
  3. calcular()     → procesa cada segmento de forma independiente y concatena

Granularidad:
  - Tarifa INDEXADO=X → 96 filas/día (cuarto-horario)
  - Tarifa sin INDEXADO → 1 fila/día, PROF_TIME = 00:00:00

Obligatoriedad:
  - PRB: siempre obligatorio → ValueError si no se encuentra
  - REG: siempre obligatorio → ValueError si no hay valor en EPREIH o EPROFVAL15
  - COM: opcional → 0.0 si no viene o no tiene valor para ese día
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

ATRIBUTOS_OBLIGATORIOS = {"CALC_INI", "CALC_FIN", "PRODUCTO", "TARIFATR", "GEOZONA", "VERSION"}


# ---------------------------------------------------------------------------
# PARSE INPUT — calendario diario
# ---------------------------------------------------------------------------

def parse_input(df_input: pd.DataFrame) -> dict:
    """
    Lee el INPUT y construye el calendario diario de atributos.

    Reglas:
    - CALC_INI / CALC_FIN definen el periodo (su VALOR es la fecha)
    - Resto de atributos: vigentes si FDESDE <= dia <= FHASTA
    - Validación de solapamiento: un atributo NO puede tener dos filas
      con vigencias que se solapen en el mismo día
    - Atributos obligatorios (PRODUCTO, TARIFATR, GEOZONA, VERSION):
      deben tener cobertura para TODOS los días del periodo
    - Atributos COM: opcionales, 0 si no hay valor ese día
    """
    df = df_input.copy()
    df["FDESDE"] = pd.to_datetime(df["FDESDE"]).dt.date
    df["FHASTA"] = pd.to_datetime(df["FHASTA"]).dt.date

    # Obtener periodo de cálculo desde CALC_INI / CALC_FIN
    def get_valor_atributo(atr: str):
        rows = df[df["ATRIBUTO"] == atr]
        if rows.empty:
            raise ValueError(f"Atributo obligatorio '{atr}' no encontrado en el INPUT.")
        return rows.iloc[0]["VALOR"]

    fecha_ini = pd.Timestamp(get_valor_atributo("CALC_INI")).date()
    fecha_fin = pd.Timestamp(get_valor_atributo("CALC_FIN")).date()

    # Generar todos los días del periodo
    dias = pd.date_range(start=fecha_ini, end=fecha_fin, freq="D")
    fechas = [d.date() for d in dias]

    # Validar solapamientos por atributo
    atributos_unicos = [a for a in df["ATRIBUTO"].unique()
                        if a not in ("CALC_INI", "CALC_FIN")]

    for atr in atributos_unicos:
        filas = df[df["ATRIBUTO"] == atr].sort_values("FDESDE")
        for i in range(len(filas) - 1):
            fila_a = filas.iloc[i]
            fila_b = filas.iloc[i + 1]
            if fila_a["FHASTA"] >= fila_b["FDESDE"]:
                raise ValueError(
                    f"Solapamiento en atributo '{atr}': "
                    f"fila {fila_a['FDESDE']}→{fila_a['FHASTA']} solapa con "
                    f"{fila_b['FDESDE']}→{fila_b['FHASTA']}"
                )

    # Construir calendario: para cada día, valor vigente de cada atributo
    registros = []
    for fecha in fechas:
        for atr in atributos_unicos:
            vigentes = df[
                (df["ATRIBUTO"] == atr) &
                (df["FDESDE"]   <= fecha) &
                (df["FHASTA"]   >= fecha)
            ]
            if not vigentes.empty:
                registros.append({
                    "FECHA":    fecha,
                    "ATRIBUTO": atr,
                    "VALOR":    vigentes.iloc[0]["VALOR"]
                })

    calendario = pd.DataFrame(registros, columns=["FECHA", "ATRIBUTO", "VALOR"])

    # Validar atributos obligatorios: cobertura todos los días
    for atr in ("PRODUCTO", "TARIFATR", "GEOZONA", "VERSION"):
        dias_con_valor = set(calendario[calendario["ATRIBUTO"] == atr]["FECHA"])
        dias_sin_valor = [f for f in fechas if f not in dias_con_valor]
        if dias_sin_valor:
            raise ValueError(
                f"Atributo obligatorio '{atr}' sin cobertura para los días: "
                f"{dias_sin_valor[:5]}{'...' if len(dias_sin_valor) > 5 else ''}"
            )

    return {
        "fecha_ini":  fecha_ini,
        "fecha_fin":  fecha_fin,
        "calendario": calendario,
    }


# ---------------------------------------------------------------------------
# SEGMENTOS — tramos donde PRODUCTO+TARIFATR+GEOZONA+VERSION son constantes
# ---------------------------------------------------------------------------

def get_segmentos(parsed: dict) -> list[dict]:
    """
    Detecta tramos consecutivos donde los atributos clave son constantes.
    Devuelve lista de dicts con:
      fecha_ini, fecha_fin, producto, tarifatr, geozona, version,
      atributos_com_por_dia: {fecha: {atributo: valor}}
    """
    cal = parsed["calendario"]
    fecha_ini = parsed["fecha_ini"]
    fecha_fin = parsed["fecha_fin"]
    dias = pd.date_range(start=fecha_ini, end=fecha_fin, freq="D")
    fechas = [d.date() for d in dias]

    def get_val_dia(fecha: date, atr: str, default=None):
        rows = cal[(cal["FECHA"] == fecha) & (cal["ATRIBUTO"] == atr)]
        return rows.iloc[0]["VALOR"] if not rows.empty else default

    segmentos = []
    seg_ini = fechas[0]
    prev = {
        "PRODUCTO": get_val_dia(fechas[0], "PRODUCTO"),
        "TARIFATR": get_val_dia(fechas[0], "TARIFATR"),
        "GEOZONA":  get_val_dia(fechas[0], "GEOZONA"),
        "VERSION":  get_val_dia(fechas[0], "VERSION"),
    }

    def cerrar_segmento(seg_fin: date, claves: dict, fechas_seg: list):
        # Recopilar atributos COM por día para este segmento
        atrs_com = {}
        for f in fechas_seg:
            vals_dia = {}
            for _, row in cal[cal["FECHA"] == f].iterrows():
                atr = row["ATRIBUTO"]
                if atr not in ("PRODUCTO", "TARIFATR", "GEOZONA", "VERSION",
                               "VENTANA", "VENTANA_C1"):
                    try:
                        vals_dia[atr] = float(row["VALOR"]) if pd.notna(row["VALOR"]) else 0.0
                    except (TypeError, ValueError):
                        vals_dia[atr] = 0.0
            atrs_com[f] = vals_dia

        # VENTANA: puede variar pero la tomamos del primer día del segmento
        ventana = get_val_dia(fechas_seg[0], "VENTANA")
        if ventana is not None and isinstance(ventana, float) and math.isnan(ventana):
            ventana = None

        segmentos.append({
            "fecha_ini":          seg_ini,
            "fecha_fin":          seg_fin,
            "producto":           claves["PRODUCTO"],
            "tarifatr":           claves["TARIFATR"],
            "geozona":            claves["GEOZONA"],
            "version":            claves["VERSION"],
            "ventana":            ventana,
            "atributos_com_dia":  atrs_com,
        })

    fechas_seg_actual = [fechas[0]]

    for fecha in fechas[1:]:
        curr = {
            "PRODUCTO": get_val_dia(fecha, "PRODUCTO"),
            "TARIFATR": get_val_dia(fecha, "TARIFATR"),
            "GEOZONA":  get_val_dia(fecha, "GEOZONA"),
            "VERSION":  get_val_dia(fecha, "VERSION"),
        }
        if curr != prev:
            cerrar_segmento(fechas_seg_actual[-1], prev, fechas_seg_actual)
            seg_ini = fecha
            fechas_seg_actual = [fecha]
            prev = curr
        else:
            fechas_seg_actual.append(fecha)

    cerrar_segmento(fechas_seg_actual[-1], prev, fechas_seg_actual)
    return segmentos


# ---------------------------------------------------------------------------
# PERFILES CUARTO-HORARIOS (UTC → CET)
# ---------------------------------------------------------------------------

def build_perfil_series(profval_df: pd.DataFrame, perfil: int,
                        slots_cet: pd.DatetimeIndex,
                        obligatorio: bool = True) -> np.ndarray:
    slots_utc = slots_cet.tz_localize(TZ_CET).tz_convert(TZ_UTC)
    result = np.zeros(len(slots_cet))
    try:
        perfil_data = profval_df.loc[perfil]
    except KeyError:
        if obligatorio:
            raise ValueError(f"Perfil {perfil} no encontrado en EPROFVAL15.")
        return result

    for i, dt_utc in enumerate(slots_utc):
        col = f"VAL{dt_utc.hour:02d}{dt_utc.minute:02d}"
        ts  = pd.Timestamp(dt_utc.date())
        try:
            val = float(perfil_data.loc[ts, col])
            if obligatorio and (pd.isna(val)):
                raise ValueError(
                    f"Perfil {perfil}: valor nulo en {ts.date()} col {col}."
                )
            result[i] = val / DIVISOR_PERFIL
        except KeyError:
            if obligatorio:
                raise ValueError(
                    f"Perfil {perfil}: no hay datos para {ts.date()} col {col}."
                )
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
# CÁLCULO DE UN SEGMENTO
# ---------------------------------------------------------------------------

def calcular_segmento(tables: dict, seg: dict) -> pd.DataFrame:
    """
    Calcula el detalle de precios para un segmento (periodo con atributos clave constantes).
    """
    fecha_ini     = seg["fecha_ini"]
    fecha_fin     = seg["fecha_fin"]
    producto      = seg["producto"]
    tarifatr      = seg["tarifatr"]
    geozona       = seg["geozona"]
    version       = seg["version"]
    ventana       = seg["ventana"]
    atrib_com_dia = seg["atributos_com_dia"]   # {fecha: {atributo: valor}}

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

    # Series temporales
    slots_qh = pd.date_range(
        start=datetime.combine(fecha_ini, datetime.min.time()),
        end=datetime.combine(fecha_fin,   datetime.min.time()) + timedelta(days=1),
        freq="15min", inclusive="left",
    )
    n_qh      = len(slots_qh)
    fechas_qh = slots_qh.date
    horas_qh  = slots_qh.strftime("%H:%M:%S")

    dias       = pd.date_range(start=fecha_ini, end=fecha_fin, freq="D")
    fechas_dia = dias.date
    horas_dia  = np.full(len(dias), "00:00:00")
    n_dia      = len(dias)

    # Cache de perfiles
    _cache: dict[int, np.ndarray] = {}

    def get_serie(perfil: int, obligatorio: bool = True) -> np.ndarray:
        if perfil not in _cache:
            _cache[perfil] = build_perfil_series(profval_df, perfil, slots_qh, obligatorio)
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

    def get_precio_epreih(preis: str, fecha: date, obligatorio: bool = True) -> float:
        ts   = pd.Timestamp(fecha)
        mask = (epreih_df["PREIS"]    == preis) & \
               (epreih_df["ABDATUM"]  <= ts)    & \
               (epreih_df["BISDATUM"] >= ts)
        r = epreih_df.loc[mask, "PREISBTR"]
        if r.empty:
            if obligatorio:
                raise ValueError(
                    f"EPREIH: no hay precio para PREIS='{preis}' en fecha {fecha}."
                )
            return 0.0
        return float(r.iloc[0])

    def get_prb(atributo: str, obligatorio: bool = True) -> float:
        val = get_precio_base(
            tables, pais="ES",
            tipo_energia=tipo_energia,
            tipo_producto=tipo_producto,
            tarifa_acceso=tarifatr,
            version=version,
            ventana=ventana,
            atributo=atributo,
        )
        if val is None and obligatorio:
            raise ValueError(
                f"ZFA_T_MATRIZ_PRB: no hay precio base para "
                f"TIPO_PRODUCTO={tipo_producto}, TARIFA={tarifatr}, "
                f"VERSION={version}, ATRIBUTO={atributo}."
            )
        return val or 0.0

    # Perfiles comunes
    pf_periodos_num = get_perfil_num("E_PERIODOS")
    if pf_periodos_num:
        periodos_serie = np.round(get_serie(pf_periodos_num, False) * DIVISOR_PERFIL).astype(int)
    else:
        periodos_serie = np.ones(n_qh, dtype=int)

    pf_perdidas_num = get_perfil_num("E_PERDIDAS")
    perdidas_serie  = get_serie(pf_perdidas_num, False) if pf_perdidas_num else np.zeros(n_qh)

    tasa_mun_valor  = get_precio_epreih("E_TASAMUNI", fecha_ini, obligatorio=False)
    inc_tasas_valor = get_prb("E_PBINCMU", obligatorio=False)

    inc_perdidas_por_periodo = {p: get_prb(f"E_PBINCMP{p}", obligatorio=False) for p in range(1, 7)}
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

        try:
            tarifa_info = tarifas_df.loc[tarifa_precio]
        except KeyError:
            continue

        # EXPLICITO
        explicito = tarifa_info.get("EXPLICITO")
        if pd.notna(explicito) and explicito:
            # Comprobar si el atributo tiene valor en algún día del segmento
            tiene_valor = any(
                atrib_com_dia.get(f, {}).get(str(explicito), 0.0) != 0.0
                for f in fechas_dia
            )
            if not tiene_valor:
                continue

        tarifa_indexada = str(tarifa_info.get("INDEXADO", "")) == "X"

        # VENTANA desde tarifa
        ventana_campo = tarifa_info.get("VENTANA")
        vent = ventana
        if pd.notna(ventana_campo) and ventana_campo:
            # Usar el valor del campo indicado del primer día
            vent_raw = atrib_com_dia.get(fechas_dia[0], {}).get(str(ventana_campo))
            if vent_raw is not None and not (isinstance(vent_raw, float) and math.isnan(vent_raw)):
                vent = vent_raw

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
            n      = n_qh
            fechas = fechas_qh
            horas  = horas_qh
            preis_val  = None
            perfil_num = None
            precio_arr = np.zeros(n)

            if matriz_precio == "COM":
                # COM cuarto-horario: expandir valor diario a 96 valores
                precio_arr = np.array([
                    atrib_com_dia.get(f, {}).get(atributo, 0.0)
                    for f in fechas_qh
                ])

            elif matriz_precio == "PRB":
                precio_arr[:] = get_prb(atributo, obligatorio=True)

            elif matriz_precio == "REG":
                preis_val = get_preis(atributo)
                if not preis_val:
                    raise ValueError(
                        f"ZFA_T_MATRIZ_REG: no hay PREIS para "
                        f"TIPO_ENERGIA={tipo_energia}, TARIFA={tarifatr}, ATRIBUTO={atributo}."
                    )
                if atr_indexado:
                    perfil_num = get_perfil_num(preis_val)
                    if not perfil_num:
                        raise ValueError(
                            f"ZFA_T_PERFILES: no hay perfil para "
                            f"INDICE={preis_val}, TARIFA={tarifatr}, GEOZONA={geozona}."
                        )
                    precio_arr = get_serie(perfil_num, obligatorio=True)
                else:
                    precios_dia = {
                        f: get_precio_epreih(preis_val, f, obligatorio=True)
                        for f in sorted(set(fechas_dia))
                    }
                    precio_arr = np.array([precios_dia[f] for f in fechas_qh])

            perdidas_arr     = perdidas_serie.copy() if flag_perdidas == "X" else np.zeros(n)
            inc_perdidas_arr = inc_perdidas_serie.copy() if flag_perdidas == "X" else np.zeros(n)
            tasa_arr         = np.full(n, tasa_mun_valor) if flag_tasa == "X" else np.zeros(n)
            inc_tasas        = inc_tasas_valor if flag_tasa == "X" else 0.0
            apunt_arr        = np.zeros(n)

        else:
            n      = n_dia
            fechas = fechas_dia
            horas  = horas_dia
            preis_val  = None
            perfil_num = None
            precio_arr = np.zeros(n)

            if matriz_precio == "COM":
                precio_arr = np.array([
                    atrib_com_dia.get(f, {}).get(atributo, 0.0)
                    for f in fechas_dia
                ])

            elif matriz_precio == "PRB":
                precio_arr[:] = get_prb(atributo, obligatorio=True)

            elif matriz_precio == "REG":
                preis_val = get_preis(atributo)
                if not preis_val:
                    raise ValueError(
                        f"ZFA_T_MATRIZ_REG: no hay PREIS para "
                        f"TIPO_ENERGIA={tipo_energia}, TARIFA={tarifatr}, ATRIBUTO={atributo}."
                    )
                precio_arr = np.array([
                    get_precio_epreih(preis_val, f, obligatorio=True)
                    for f in fechas_dia
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

        all_dfs.append(pd.DataFrame({
            "PRODUCTO":            producto,
            "TARIFA_PRECIO":       tarifa_precio,
            "ATRIBUTO":            atributo,
            "PROF_DATE":           fechas,
            "PROF_TIME":           horas,
            "TIPO_ENERGIA":        tipo_energia,
            "TIPO_PRODUCTO":       tipo_producto,
            "TARIFA_ACCESO":       tarifatr,
            "VERSION":             version,
            "VENTANA":             vent,
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
        }))

    return pd.concat(all_dfs, ignore_index=True) if all_dfs else pd.DataFrame()


# ---------------------------------------------------------------------------
# PUNTO DE ENTRADA PRINCIPAL
# ---------------------------------------------------------------------------

def calcular(tables: dict, parsed: dict) -> pd.DataFrame:
    """
    Calcula el detalle de precios para todos los segmentos del INPUT.
    """
    segmentos = get_segmentos(parsed)
    resultados = []
    for seg in segmentos:
        df_seg = calcular_segmento(tables, seg)
        if not df_seg.empty:
            resultados.append(df_seg)
    return pd.concat(resultados, ignore_index=True) if resultados else pd.DataFrame()


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

    print("Parseando INPUT...")
    parsed = parse_input(df_input)
    segs   = get_segmentos(parsed)
    print(f"  Periodo  : {parsed['fecha_ini']} → {parsed['fecha_fin']}")
    print(f"  Segmentos: {len(segs)}")
    for s in segs:
        print(f"    {s['fecha_ini']} → {s['fecha_fin']} | "
              f"{s['producto']} | {s['tarifatr']} | {s['version']}")

    t0 = time.time()
    resultado = calcular(tables, parsed)
    print(f"\n  {len(resultado):,} filas en {time.time()-t0:.1f}s")

    df_out = pd.read_excel(path, sheet_name="OUTPUT")
    print(f"  OUTPUT SAP: {len(df_out):,} filas")

    resultado["PROF_DATE"] = pd.to_datetime(resultado["PROF_DATE"])
    df_out["PROF_DATE"]    = pd.to_datetime(df_out["PROF_DATE"])
    resultado["PROF_TIME"] = resultado["PROF_TIME"].astype(str)
    df_out["PROF_TIME"]    = df_out["PROF_TIME"].astype(str)

    merged = resultado.merge(
        df_out[["PROF_DATE","PROF_TIME","TARIFA_PRECIO","ATRIBUTO","PRECIO_FINAL"]],
        on=["PROF_DATE","PROF_TIME","TARIFA_PRECIO","ATRIBUTO"],
        suffixes=("_calc","_real")
    )
    if len(merged):
        diff = (merged["PRECIO_FINAL_calc"] - merged["PRECIO_FINAL_real"]).abs()
        print(f"\n  Filas comparadas : {len(merged):,}")
        print(f"  Match exacto     : {(diff < 1e-5).sum():,} / {len(merged):,}  ({(diff<1e-5).mean()*100:.1f}%)")
        print(f"  MAE              : {diff.mean():.8f}")
