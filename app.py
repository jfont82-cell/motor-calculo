import streamlit as st
import pandas as pd
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from db import load_all
from engine import parse_input, calcular

st.set_page_config(page_title="Motor de Cálculo", layout="wide")
st.title("⚡ Motor de Cálculo de Precios")

# --- 1. Excel de configuración ---
st.subheader("1. Sube el Excel de configuración (BBDD)")
fichero_bbdd = st.file_uploader(
    "Excel con las tablas de configuración (MOTOR_CALCULO.xlsx)",
    type=["xlsx"], key="bbdd"
)

if not fichero_bbdd:
    st.info("Sube primero el Excel de configuración para continuar.")
    st.stop()

@st.cache_resource
def cargar_tablas(fichero):
    return load_all(fichero)

try:
    tables = cargar_tablas(fichero_bbdd)
    st.success("✅ Tablas de configuración cargadas correctamente")
except Exception as e:
    st.error(f"❌ Error cargando configuración: {e}")
    st.stop()

# --- 2. INPUT del contrato ---
st.subheader("2. Sube el fichero INPUT del contrato")
fichero_input = st.file_uploader(
    "Excel con el INPUT del contrato",
    type=["xlsx"], key="input"
)

if not fichero_input:
    st.info("Sube el fichero INPUT para continuar.")
    st.stop()

try:
    df_input = pd.read_excel(fichero_input, sheet_name="INPUT")
    parsed   = parse_input(df_input)

    st.write("**Periodo de cálculo:**")
    col1, col2 = st.columns(2)
    col1.metric("Desde", str(parsed["fecha_ini"]))
    col2.metric("Hasta", str(parsed["fecha_fin"]))

    # Mostrar resumen del calendario
    cal = parsed["calendario"]
    primer_dia = parsed["fecha_ini"]
    vals_primer_dia = cal[cal["FECHA"] == primer_dia].set_index("ATRIBUTO")["VALOR"]

    st.write("**Valores del primer día:**")
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Producto",   str(vals_primer_dia.get("PRODUCTO",  "—")))
    col2.metric("Tarifa ATR", str(vals_primer_dia.get("TARIFATR",  "—")))
    col3.metric("Geozona",    str(vals_primer_dia.get("GEOZONA",   "—")))
    col4.metric("Versión",    str(vals_primer_dia.get("VERSION",   "—")))

    if st.button("🚀 Calcular", type="primary"):
        with st.spinner("Calculando..."):
            resultado = calcular(tables, parsed)

        st.success(f"✅ {len(resultado):,} filas calculadas")

        # Resumen por atributo
        st.subheader("Resumen por atributo")
        resumen = resultado[resultado["PRECIO_FINAL"] != 0].groupby("ATRIBUTO").agg(
            PRECIO_MEDIO      =("PRECIO",       "mean"),
            PRECIO_FINAL_MEDIO=("PRECIO_FINAL", "mean"),
            FILAS             =("PRECIO_FINAL", "count")
        ).round(6)
        st.dataframe(resumen, use_container_width=True)

        # Detalle con filtro
        st.subheader("Detalle completo")
        atributos = ["Todos"] + sorted(resultado["ATRIBUTO"].unique().tolist())
        atr_sel   = st.selectbox("Filtrar por atributo", atributos)
        df_mostrar = resultado if atr_sel == "Todos" else resultado[resultado["ATRIBUTO"] == atr_sel]
        st.dataframe(df_mostrar.head(1000), use_container_width=True)

        # Descarga
        csv = resultado.to_csv(index=False).encode("utf-8")
        st.download_button(
            label="⬇️ Descargar resultado CSV",
            data=csv,
            file_name="resultado_calculo.csv",
            mime="text/csv"
        )

except ValueError as e:
    st.error(f"❌ Error de validación: {e}")
except Exception as e:
    st.error(f"❌ Error inesperado: {e}")
    st.exception(e)
