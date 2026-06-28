"""
motor_calculo/db.py
===================
Carga todas las tablas de configuración desde el Excel y expone
funciones de resolución de precios.
"""

import pandas as pd
from datetime import date, datetime
from pathlib import Path
import math


EXCEL_PATH = Path(__file__).parent.parent / "MOTOR_CALCULO.xlsx"


def _read(path: Path, sheet: str, **kwargs) -> pd.DataFrame:
    return pd.read_excel(path, sheet_name=sheet, **kwargs)


def load_all(excel_path=None) -> dict:
    """
    Carga todas las tablas de configuración en un diccionario de DataFrames.
    """
  if excel_path is None:
    path = EXCEL_PATH
elif hasattr(excel_path, 'read'):
    import io
    path = excel_path
else:
    path = Path(excel_path)
    tables = {}

    # Catálogo de productos
    tables["ZFA_T_PRODUCTOS"] = _read(path, "ZFA_T_PRODUCTOS").set_index("PRODUCTO")

    # Conceptos de facturación
    tables["ZFA_T_TARIFAS"] = _read(path, "ZFA_T_TARIFAS").set_index("TARIFA_PRECIO")

    # Atributos de precio
    tables["ZFA_T_ATRIBUTOS"] = _read(path, "ZFA_T_ATRIBUTOS").set_index("ATRIBUTO")

    # Matriz producto → concepto → atributo
    tables["ZFA_T_PROD_ATRIB"] = _read(path, "ZFA_T_PROD_ATRIB")

    # Precios base comerciales
    # Clave: PAIS + TIPO_ENERGIA + TIPO_PRODUCTO + TARIFA_ACCESO + VERSION + VENTANA + ATRIBUTO
    tables["ZFA_T_MATRIZ_PRB"] = _read(path, "ZFA_T_MATRIZ_PRB").set_index(
        ["PAIS", "TIPO_ENERGIA", "TIPO_PRODUCTO", "TARIFA_ACCESO", "VERSION", "VENTANA", "ATRIBUTO"]
    )

    # Mapping atributo regulado → código precio regulado
    tables["ZFA_T_MATRIZ_REG"] = _read(path, "ZFA_T_MATRIZ_REG").set_index(
        ["PAIS", "TIPO_ENERGIA", "TARIFA_ACCESO", "ATRIBUTO"]
    )

    # Serie histórica de precios regulados (sin set_index: necesitamos filtrar por BISDATUM)
    tables["EPREIH"] = _read(path, "EPREIH")

    # Perfiles de consumo de referencia
    tables["ZFA_T_PERFILES"] = _read(path, "ZFA_T_PERFILES").set_index(
        ["INDICE", "TARIFA_ATR", "GEOZONA"]
    )

    # Curvas cuarto-horarias
    tables["EPROFVAL15"] = _read(path, "EPROFVAL15").set_index(["PROFILE", "VALUEDAY"])

    return tables


def load_input(excel_path=None) -> pd.DataFrame:
    path = Path(excel_path) if excel_path else EXCEL_PATH
    return pd.read_excel(path, sheet_name="INPUT")


# ---------------------------------------------------------------------------
# RESOLUCIÓN DE PRECIO REGULADO
# ---------------------------------------------------------------------------

def get_precio_regulado(tables: dict, preis: str, fecha) -> float | None:
    """
    Devuelve el PREISBTR vigente para un código PREIS en una fecha dada.
    """
    df = tables["EPREIH"]
    ts = pd.Timestamp(fecha)
    mask = (df["PREIS"] == preis) & (df["ABDATUM"] <= ts) & (df["BISDATUM"] >= ts)
    resultado = df.loc[mask, "PREISBTR"]
    return float(resultado.iloc[0]) if not resultado.empty else None


# ---------------------------------------------------------------------------
# RESOLUCIÓN DE PRECIO BASE
# ---------------------------------------------------------------------------

def get_precio_base(
    tables: dict,
    pais: str,
    tipo_energia: int,
    tipo_producto: str,
    tarifa_acceso: str,
    version: str,
    ventana,          # puede ser NaN / None / string
    atributo: str,
) -> float | None:
    """
    Devuelve PRECIO_BASE de ZFA_T_MATRIZ_PRB para la clave dada.
    Si ventana no es None/NaN, busca primero con ventana y luego sin ella.
    """
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

    # Fallback: sin ventana (NaN)
    return _get(float("nan"))


# ---------------------------------------------------------------------------
# ATRIBUTOS DE UN PRODUCTO PARA UN CONCEPTO
# ---------------------------------------------------------------------------

def get_atributos_producto(tables: dict, producto: str, tarifa_precio: str) -> pd.DataFrame:
    df = tables["ZFA_T_PROD_ATRIB"]
    resultado = df[(df["PRODUCTO"] == producto) & (df["TARIFA_PRECIO"] == tarifa_precio)]
    return resultado.reset_index(drop=True)


# ---------------------------------------------------------------------------
# TEST DIRECTO
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    excel = sys.argv[1] if len(sys.argv) > 1 else None
    print("Cargando tablas...")
    tables = load_all(excel)

    print("\n=== Tablas cargadas ===")
    for nombre, df in tables.items():
        print(f"  {nombre}: {len(df)} filas")

    print("\n=== Test: precio regulado E_EATR20P1 a 2025-01-01 ===")
    print(f"  PREISBTR = {get_precio_regulado(tables, 'E_EATR20P1', date(2025, 1, 1))}")

    print("\n=== Test: precio base E_PBPOBJ (FIJ, 2.0TD, V2631, sin ventana) ===")
    print(f"  PRECIO_BASE = {get_precio_base(tables, 'ES', 1, 'FIJ', '2.0TD', 'V2631', None, 'E_PBPOBJ')}")

    print("\n=== Test: atributos E_INDCL / EI_ENEINPS ===")
    print(get_atributos_producto(tables, "E_INDCL", "EI_ENEINPS").to_string())
