"""
motor_calculo/db.py
===================
Carga todas las tablas de configuración desde el Excel y expone
funciones de resolución de precios.

Acepta como excel_path tanto una ruta (str/Path) como un objeto
fichero (UploadedFile de Streamlit o similar).
"""

import pandas as pd
from datetime import date
from pathlib import Path
import math


EXCEL_PATH = Path(__file__).parent / "MOTOR_CALCULO.xlsx"


def _read(src, sheet: str, **kwargs) -> pd.DataFrame:
    """Lee una pestaña del Excel, aceptando ruta o fichero."""
    if hasattr(src, "seek"):
        src.seek(0)
    return pd.read_excel(src, sheet_name=sheet, **kwargs)


def load_all(excel_path=None) -> dict:
    """
    Carga todas las tablas de configuración en un diccionario de DataFrames.
    excel_path puede ser:
      - None            → usa MOTOR_CALCULO.xlsx junto al módulo
      - str / Path      → ruta al fichero
      - UploadedFile    → objeto fichero de Streamlit
    """
    src = excel_path if excel_path is not None else EXCEL_PATH

    tables = {}

    tables["ZFA_T_PRODUCTOS"] = _read(src, "ZFA_T_PRODUCTOS").set_index("PRODUCTO")
    tables["ZFA_T_TARIFAS"] = _read(src, "ZFA_T_TARIFAS").set_index("TARIFA_PRECIO")
    tables["ZFA_T_ATRIBUTOS"] = _read(src, "ZFA_T_ATRIBUTOS").set_index("ATRIBUTO")
    tables["ZFA_T_PROD_ATRIB"] = _read(src, "ZFA_T_PROD_ATRIB")
    tables["ZFA_T_MATRIZ_PRB"] = _read(src, "ZFA_T_MATRIZ_PRB").set_index(
        ["PAIS", "TIPO_ENERGIA", "TIPO_PRODUCTO", "TARIFA_ACCESO", "VERSION", "VENTANA", "ATRIBUTO"]
    )
    tables["ZFA_T_MATRIZ_REG"] = _read(src, "ZFA_T_MATRIZ_REG").set_index(
        ["PAIS", "TIPO_ENERGIA", "TARIFA_ACCESO", "ATRIBUTO"]
    )
    tables["EPREIH"] = _read(src, "EPREIH")
    tables["ZFA_T_PERFILES"] = _read(src, "ZFA_T_PERFILES").set_index(
        ["INDICE", "TARIFA_ATR", "GEOZONA"]
    )
    tables["EPROFVAL15"] = _read(src, "EPROFVAL15").set_index(["PROFILE", "VALUEDAY"])

    return tables


def load_input(excel_path=None) -> pd.DataFrame:
    """Lee la pestaña INPUT del Excel."""
    src = excel_path if excel_path is not None else EXCEL_PATH
    if hasattr(src, "seek"):
        src.seek(0)
    return pd.read_excel(src, sheet_name="INPUT")


def get_precio_regulado(tables: dict, preis: str, fecha) -> float | None:
    df = tables["EPREIH"]
    ts = pd.Timestamp(fecha)
    mask = (df["PREIS"] == preis) & (df["ABDATUM"] <= ts) & (df["BISDATUM"] >= ts)
    resultado = df.loc[mask, "PREISBTR"]
    return float(resultado.iloc[0]) if not resultado.empty else None


def get_precio_base(
    tables: dict,
    pais: str,
    tipo_energia: int,
    tipo_producto: str,
    tarifa_acceso: str,
    version: str,
    ventana,
    atributo: str,
) -> float | None:
    def _get(vent):
        try:
            val = tables["ZFA_T_MATRIZ_PRB"].loc[
                (pais, tipo_energia, tipo_producto, tarifa_acceso, version, vent, atributo),
                "PRECIO_BASE"
            ]
            if isinstance(val, pd.Series):
                val = val.iloc[0]
            return float(val)
        except KeyError:
            return None

    tiene_ventana = ventana is not None and not (isinstance(ventana, float) and math.isnan(ventana))

    if tiene_ventana:
        precio = _get(ventana)
        if precio is not None:
            return precio

    return _get(float("nan"))


def get_atributos_producto(tables: dict, producto: str, tarifa_precio: str) -> pd.DataFrame:
    df = tables["ZFA_T_PROD_ATRIB"]
    resultado = df[(df["PRODUCTO"] == producto) & (df["TARIFA_PRECIO"] == tarifa_precio)]
    return resultado.reset_index(drop=True)
