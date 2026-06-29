import streamlit as st
import pandas as pd
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from db import load_all
from engine import parse_input, calcular

st.set_page_config(page_title="Motor de Cálculo", layout="wide")
st.title("⚡ Motor de Cálculo de Precios")

# --- 1. Excel de configuración ---
st.subheader("1. Configuración (BBDD)")

BBDD_DEFAULT = Path(__file__).parent / "MOTOR_CALCULO.xlsx"

with st.expander("🔄 Actualizar Excel de configuración (opcional)"):
    fichero_bbdd = st.file_uploader(
        "Sube un nuevo Excel para reemplazar la configuración actual",
        type=["xlsx"], key="bbdd"
    )

@st.cache_resource
def cargar_tablas(fichero=None):
    if fichero is not None:
        return load_all(fichero)
    return load_all(BBDD_DEFAULT)

try:
    if "bbdd" in st.session_state and st.session_state["bbdd"] is not None:
        tables = cargar_tablas(st.session_state["bbdd"])
        st.success("✅ Configuración cargada desde fichero subido")
    else:
        tables = cargar_tablas()
        st.success(f"✅ Configuración cargada automáticamente ({BBDD_DEFAULT.name})")
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

    cal = parsed["calendario"]
    primer_dia = parsed["fecha_ini"]
    vals = cal[cal["FECHA"] == primer_dia].set_index("ATRIBUTO")["VALOR"]

    st.write("**Valores del primer día:**")
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Producto",   str(vals.get("PRODUCTO",  "—")))
    col2.metric("Tarifa ATR", str(vals.get("TARIFATR",  "—")))
    col3.metric("Geozona",    str(vals.get("GEOZONA",   "—")))
    col4.metric("Versión",    str(vals.get("VERSION",   "—")))

    if st.button("🚀 Calcular", type="primary"):
        with st.spinner("Calculando..."):
            resultado = calcular(tables, parsed)
        st.session_state["resultado"] = resultado

    if "resultado" in st.session_state:
        resultado = st.session_state["resultado"]
        st.success(f"✅ {len(resultado):,} filas calculadas")

        # Tabla compacta
        resumen_precio = resultado.groupby(
            ["PRODUCTO", "TARIFA_PRECIO", "PROF_DATE", "PROF_TIME"],
            as_index=False
        ).agg(PRECIO_FINAL_TOTAL=("PRECIO_FINAL", "sum"))

        # Resumen por atributo
        st.subheader("Resumen por atributo")
        resumen = resultado[resultado["PRECIO_FINAL"] != 0].groupby("ATRIBUTO").agg(
            PRECIO_MEDIO      =("PRECIO",       "mean"),
            PRECIO_FINAL_MEDIO=("PRECIO_FINAL", "mean"),
            FILAS             =("PRECIO_FINAL", "count")
        ).round(6)
        st.dataframe(resumen, use_container_width=True)

        # Precio compactado
        st.subheader("Precio compactado (suma por fecha/hora)")
        st.dataframe(resumen_precio.head(1000), use_container_width=True)

        # Detalle completo
        st.subheader("Detalle completo")
        atributos  = ["Todos"] + sorted(resultado["ATRIBUTO"].unique().tolist())
        atr_sel    = st.selectbox("Filtrar por atributo", atributos)
        df_mostrar = resultado if atr_sel == "Todos" else resultado[resultado["ATRIBUTO"] == atr_sel]
        st.dataframe(df_mostrar.head(1000), use_container_width=True)

        # Descargas — solo se generan al pulsar el botón
        st.subheader("Descargar resultado")

        if st.button("📦 Preparar ficheros de descarga"):
            with st.spinner("Generando ficheros..."):
                # Excel
                buf = io.BytesIO()
                with pd.ExcelWriter(buf, engine="openpyxl") as writer:
                    resumen_precio.to_excel(writer, index=False, sheet_name="PRECIO_COMPACTO")
                    resultado.to_excel(writer, index=False, sheet_name="DETALLE")
                st.session_state["excel_bytes"] = buf.getvalue()
                # CSV
                st.session_state["csv_bytes"] = resultado.to_csv(index=False).encode("utf-8")

        if "excel_bytes" in st.session_state:
            st.download_button(
                label="⬇️ Descargar Excel (compacto + detalle)",
                data=st.session_state["excel_bytes"],
                file_name="resultado_calculo.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                key="dl_xlsx"
            )
            st.download_button(
                label="⬇️ Descargar CSV (detalle)",
                data=st.session_state["csv_bytes"],
                file_name="resultado_calculo.csv",
                mime="text/csv",
                key="dl_csv"
            )

except ValueError as e:
    st.error(f"❌ Error de validación: {e}")
except Exception as e:
    st.error(f"❌ Error inesperado: {e}")
    st.exception(e)
