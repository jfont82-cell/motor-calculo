import streamlit as st
import pandas as pd
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from db import load_all
from engine import parse_input, calcular

st.set_page_config(page_title="Motor de Cálculo", layout="wide")
st.title("⚡ Motor de Cálculo de Precios")

# --- Carga de tablas de configuración ---
st.subheader("1. Sube el Excel de configuración (BBDD)")
fichero_bbdd = st.file_uploader(
    "Excel con las tablas de configuración (MOTOR_CALCULO.xlsx)",
    type=["xlsx"],
    key="bbdd"
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
    st.exception(e)
    st.stop()

# --- Upload del INPUT ---
st.subheader("2. Sube el fichero INPUT del contrato")
fichero_input = st.file_uploader(
    "Excel con el INPUT del contrato",
    type=["xlsx"],
    key="input"
)

if not fichero_input:
    st.info("Sube el fichero INPUT para continuar.")
    st.stop()

try:
    df_input = pd.read_excel(fichero_input, sheet_name="INPUT")
    contrato = parse_input(df_input)

    st.write("**Datos del contrato:**")
    col1, col2, col3 = st.columns(3)
    col1.metric("Producto", contrato["PRODUCTO"])
    col2.metric("Tarifa ATR", contrato["TARIFATR"])
    col3.metric("Geozona", contrato["GEOZONA"])
    col1.metric("Desde", str(contrato["CALC_INI"]))
    col2.metric("Hasta", str(contrato["CALC_FIN"]))
    col3.metric("Versión", contrato["VERSION"])

    if st.button("🚀 Calcular", type="primary"):
        with st.spinner("Calculando..."):
            resultado = calcular(tables, contrato)

        st.success(f"✅ {len(resultado):,} filas calculadas")

        # Resumen por atributo
        st.subheader("Resumen por atributo")
        resumen = resultado[resultado["PRECIO_FINAL"] != 0].groupby("ATRIBUTO").agg(
            PRECIO_MEDIO=("PRECIO", "mean"),
            PRECIO_FINAL_MEDIO=("PRECIO_FINAL", "mean"),
            FILAS=("PRECIO_FINAL", "count")
        ).round(6)
        st.dataframe(resumen, use_container_width=True)

        # Detalle completo
        st.subheader("Detalle completo")
        atributos = ["Todos"] + sorted(resultado["ATRIBUTO"].unique().tolist())
        atr_sel = st.selectbox("Filtrar por atributo", atributos)
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

except Exception as e:
    st.error(f"❌ Error: {e}")
    st.exception(e)
