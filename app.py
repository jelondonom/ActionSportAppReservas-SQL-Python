"""
ACTION SPORT CLUB · Centro de reservas y consultas
==================================================
Streamlit + Oracle Autonomous Database (modelo en estrella).

Ejecutar desde la carpeta del proyecto:
    python -m streamlit run app.py

Credenciales: .streamlit/secrets.toml, sección [oracle] (ver .streamlit/secrets.toml.example).

Principios del backend
    * La app NUNCA descarga tablas completas: cada sección ejecuta solo el SQL que necesita
      y Oracle agrupa, cuenta y suma. Python recibe resultados ya resumidos (tope MAX_FILAS).
    * Lecturas: SELECT con parámetros (:nombre), nunca valores pegados al texto SQL.
    * Escrituras: INSERT / UPDATE dentro de una transacción (COMMIT o ROLLBACK completo).
    * La app se conecta con APP_RESERVAS, que no tiene DELETE ni DDL.
    * Las reglas de negocio (usuario activo, cancha disponible, cruces de horario, ciclo de
      vida de la reserva, reservas a futuro) se validan aquí, en la aplicación.
"""
import io
import re
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import altair as alt
import oracledb
import pandas as pd
import streamlit as st
from sklearn.cluster import KMeans
from sklearn.metrics import silhouette_score
from sklearn.preprocessing import StandardScaler

st.set_page_config(page_title="Action Sport Club · Reservas", page_icon="🎾", layout="wide")

# =============================================== 0. REGLAS Y CONSTANTES ===============================================
TZ = ZoneInfo("America/Bogota")        # Autonomous Database trabaja en UTC; la app usa la hora de Bogotá
MAX_FILAS = 2000                       # tope de filas por consulta: otra defensa para no traer tablas completas
HORA_ABRE, HORA_CIERRA = 6, 22         # horario de atención (último bloque inicia a las 21:00)
DURACIONES = [1, 2, 3]
METODOS = ["EFECTIVO", "TARJETA", "TRANSFERENCIA", "NEQUI"]
TIPOS_USUARIO = ["MIEMBRO", "EXTERNO", "ESTUDIANTE"]
DEPORTES = ["PADEL", "FUTBOL_7X7", "FUTBOL_9X9", "VOLEY_ARENA", "BASKET_3X3"]
ESTADOS = ["PENDIENTE", "RESERVADA", "PAGADA", "VENCIDA", "CANCELADA"]
ESTADOS_CANCHA = ["DISPONIBLE", "MANTENIMIENTO", "CERRADA"]
# Ciclo de vida de la reserva: desde cada estado solo se puede pasar a estos
SIGUIENTE = {"PENDIENTE": ["RESERVADA", "CANCELADA", "VENCIDA"],
             "RESERVADA": ["PAGADA", "CANCELADA", "VENCIDA"],
             "PAGADA": ["CANCELADA"]}

# Paleta de la marca (tomada del sitio web del club)
VERDE, VERDE_OSCURO, CREMA, BEIGE, FONDO, AZUL = "#233D1A", "#1A2716", "#F8F3D7", "#F4F0E6", "#FCFCF8", "#195A87"
PALETA = [VERDE, AZUL, "#9DB17C", "#C9A961", "#5E8C9E", "#7A4E2D"]

# Traducción de códigos de la base de datos a lenguaje administrativo
ETIQUETAS = {
    "PADEL": "Pádel", "FUTBOL_7X7": "Fútbol 7x7", "FUTBOL_9X9": "Fútbol 9x9", "VOLEY_ARENA": "Vóley arena",
    "BASKET_3X3": "Basket 3x3", "MANANA": "Mañana", "MIERCOLES": "Miércoles", "SABADO": "Sábado", "SI": "Sí",
    "TODAS": "Todas", "TODOS": "Todos", "SINTETICA": "Sintética", "ACRILICA": "Acrílica",
}


def etiqueta(v):
    """Código de la base de datos -> texto legible (PADEL -> Pádel, PAGADA -> Pagada)."""
    if isinstance(v, str) and v.isupper():
        return ETIQUETAS.get(v, v.replace("_", " ").capitalize())
    return v


def es_texto(serie):
    """Columna de texto (pandas 2 usa 'object'; pandas 3 usa 'str')."""
    return pd.api.types.is_object_dtype(serie) or pd.api.types.is_string_dtype(serie)


def titulo_col(c):
    return c.replace("_", " ").capitalize().replace("Pct", "%")


def pesos(v):
    v = 0 if v is None or pd.isna(v) else float(v)
    return "$ " + f"{v:,.0f}".replace(",", ".")


def ahora():
    """Fecha y hora actual de Bogotá, sin zona horaria (así la recibe una columna DATE de Oracle)."""
    return datetime.now(TZ).replace(tzinfo=None)


def hoy():
    return ahora().date()


# =============================================== 1. CONEXIÓN A ORACLE =================================================
def carpeta_wallet(c):
    """Local: carpeta del wallet. Nube (Streamlit Cloud): el wallet llega como texto en secrets
    y se escribe en una carpeta temporal; así el wallet nunca se sube al repositorio."""
    if "wallet_pem" in c:
        carpeta = Path(tempfile.gettempdir()) / "wallet_actionsport"
        carpeta.mkdir(exist_ok=True)
        (carpeta / "ewallet.pem").write_text(c["wallet_pem"])
        (carpeta / "tnsnames.ora").write_text(c["tnsnames"])
        return str(carpeta)
    return c["config_dir"]


@st.cache_resource(show_spinner=False)
def pool():
    """Pool de conexiones: se crea una vez y se reutiliza (conectar es lento)."""
    c = st.secrets["oracle"]
    carpeta = carpeta_wallet(c)
    esquema = c.get("schema", "")

    def preparar_sesion(con, _tag):
        # Las tablas pertenecen a RESERVAS; la app se conecta como APP_RESERVAS
        if esquema.isidentifier():
            con.cursor().execute(f"ALTER SESSION SET CURRENT_SCHEMA = {esquema}")

    if c.get("lib_dir"):                                   # Windows local: modo thick con Instant Client
        if oracledb.is_thin_mode():
            oracledb.init_oracle_client(lib_dir=c["lib_dir"], config_dir=carpeta)
        extra = {}
    else:                                                  # Mac o nube: modo thin, wallet como parámetros
        extra = dict(config_dir=carpeta, wallet_location=carpeta, wallet_password=c.get("wallet_password"))
    return oracledb.create_pool(user=c["user"], password=c["password"], dsn=c["dsn"], min=1, max=4,
                                increment=1, session_callback=preparar_sesion, **extra)


def solo_usados(sql, params):
    """Deja solo los parámetros que aparecen en el SQL (Oracle rechaza parámetros sobrantes)."""
    return {k: v for k, v in (params or {}).items() if re.search(rf":{k}\b", sql)}


@st.cache_data(ttl=300, show_spinner=False)
def consultar(sql, params=None):
    """SELECT -> DataFrame (columnas en minúscula). El resultado se guarda 5 minutos o hasta la próxima escritura."""
    with pool().acquire() as con:
        cur = con.cursor()
        cur.execute(sql, solo_usados(sql, params))
        columnas = [d[0].lower() for d in cur.description]
        return pd.DataFrame(cur.fetchmany(MAX_FILAS), columns=columnas)


class Tx:
    """Ayudante para escribir dentro de una transacción."""

    def __init__(self, con):
        self.con = con

    def run(self, sql, params=None):
        cur = self.con.cursor()
        cur.execute(sql, solo_usados(sql, params))
        return cur.rowcount

    def insert(self, sql, columna_id, params=None):
        """INSERT que devuelve el id generado por la columna IDENTITY (RETURNING ... INTO)."""
        cur = self.con.cursor()
        nuevo = cur.var(int)
        cur.execute(f"{sql} RETURNING {columna_id} INTO :nuevo_id", {**solo_usados(sql, params), "nuevo_id": nuevo})
        return int(nuevo.getvalue()[0])


@contextmanager
def transaccion():
    """Todo lo del bloque 'with' se guarda junto (COMMIT) o no se guarda nada (ROLLBACK)."""
    con = pool().acquire()
    try:
        yield Tx(con)
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()
        st.cache_data.clear()      # los datos cambiaron: se descartan las consultas guardadas


# =============================================== 2. INTERFAZ: ESTILO Y AYUDANTES ======================================
st.markdown(f"""
<style>
@import url('https://fonts.googleapis.com/css2?family=Instrument+Serif&family=Poppins:wght@300;400;500;600&display=swap');
.stApp, .stApp p, .stApp label, .stApp li, .stApp input, .stApp textarea, .stApp button p {{font-family: 'Poppins', sans-serif;}}
.stApp h1, .stApp h2, .stApp h3 {{font-family: 'Instrument Serif', serif !important; font-weight: 400 !important;
    text-transform: uppercase; letter-spacing: .02em; color: {VERDE_OSCURO};}}
.hero {{background: radial-gradient(circle at 82% 35%, #4A7A3C 0%, #23351F 38%, #142311 78%); border-radius: 28px;
    padding: 2rem 2.6rem; margin-bottom: 1.4rem;}}
.hero .eyebrow {{color: #E0DDD2; letter-spacing: .28em; font-size: .72rem; font-weight: 600; text-transform: uppercase;}}
.hero h1 {{color: {FONDO} !important; font-size: 2.9rem !important; margin: .2rem 0 0 !important; line-height: 1.05;}}
.hero p {{color: #E0DDD2 !important; margin: .5rem 0 0 !important; font-weight: 300;}}
div[data-testid="stMetric"] {{background: {BEIGE}; border-radius: 22px; padding: 1rem 1.2rem;}}
div[data-testid="stMetricValue"] {{font-family: 'Instrument Serif', serif; font-size: 1.85rem; color: {VERDE_OSCURO};}}
.stButton > button, .stDownloadButton > button, .stFormSubmitButton > button {{border-radius: 999px; font-weight: 500;
    padding: .4rem 1.4rem; border: 1.5px solid {VERDE};}}
section[data-testid="stSidebar"] {{background: {VERDE_OSCURO};}}
section[data-testid="stSidebar"] label, section[data-testid="stSidebar"] p,
section[data-testid="stSidebar"] small {{color: {CREMA} !important;}}
section[data-testid="stSidebar"] [data-baseweb="select"] * {{color: {VERDE_OSCURO} !important;}}
section[data-testid="stSidebar"] .stButton > button {{background: transparent; border-color: {CREMA};}}
.marca {{font-family: 'Instrument Serif', serif; font-size: 2rem; line-height: 1; color: {CREMA};}}
.marca small {{display: block; font-family: 'Poppins'; font-size: .62rem; letter-spacing: .35em; margin-top: .3rem;}}
.pregunta {{background: {BEIGE}; border-radius: 18px; padding: .9rem 1.2rem; margin: .3rem 0 1rem;}}
@media (max-width: 640px) {{
    .hero {{padding: 1.2rem 1.3rem; border-radius: 20px; margin-bottom: 1rem;}}
    .hero h1 {{font-size: 1.9rem !important;}}
    .hero .eyebrow {{font-size: .62rem;}}
    div[data-testid="stMetric"] {{padding: .7rem 1rem; border-radius: 16px;}}
    div[data-testid="stMetricValue"] {{font-size: 1.5rem;}}
    .stApp h2, .stApp h3 {{font-size: 1.35rem !important;}}
}}
</style>""", unsafe_allow_html=True)


def encabezado(titulo, subtitulo="", eyebrow="Action Sport Club"):
    st.markdown(f'<div class="hero"><div class="eyebrow">{eyebrow}</div><h1>{titulo}</h1>'
                f'<p>{subtitulo}</p></div>', unsafe_allow_html=True)


def es_dinero(c):
    return any(p in c for p in ("valor", "recaudo", "gasto", "ticket", "precio"))


def tabla(df, ocultar=()):
    """Muestra un DataFrame con títulos legibles, códigos traducidos, pesos y porcentajes con formato."""
    if df.empty:
        st.info("No hay datos para los filtros seleccionados.")
        return
    d = df.drop(columns=[c for c in ocultar if c in df.columns]).copy()
    for c in d.columns:
        if es_texto(d[c]):
            d[c] = d[c].map(etiqueta)
        elif pd.api.types.is_datetime64_any_dtype(d[c]) and (d[c].dropna().dt.time == datetime.min.time()).all():
            d[c] = d[c].dt.date                           # fechas sin hora: se muestran como AAAA-MM-DD
    formatos = {c: pesos for c in d.columns if es_dinero(c)}
    formatos.update({c: "{:.1f}%" for c in d.columns if c.endswith("pct") or c == "porcentaje"})
    d.columns = [titulo_col(c) for c in d.columns]
    formatos = {titulo_col(k): v for k, v in formatos.items()}
    st.dataframe(d.style.format(formatos, na_rep="—"), hide_index=True, width="stretch")


def boton_excel(df, nombre, clave):
    buffer = io.BytesIO()
    df.to_excel(buffer, index=False, sheet_name="datos")
    st.download_button("⬇ Descargar Excel", buffer.getvalue(), file_name=f"{nombre}.xlsx", key=clave,
                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


def grafica(df, g):
    """Gráficas con la paleta de la marca. g = dict(tipo, x, y, color, horizontal, valor)."""
    if df.empty or not g:
        return
    d = df.copy()
    for c in d.columns:
        if es_texto(d[c]):
            d[c] = d[c].map(etiqueta)
    x, y, color, tipo = g["x"], g["y"], g.get("color"), g.get("tipo", "barras")
    tip = [alt.Tooltip(c, title=titulo_col(c), format=",.0f") if es_dinero(c) and pd.api.types.is_numeric_dtype(d[c])
           else alt.Tooltip(c, title=titulo_col(c)) for c in d.columns]
    if tipo == "calor":
        dias = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]
        ch = alt.Chart(d).mark_rect(cornerRadius=4).encode(
            x=alt.X(f"{x}:O", title=titulo_col(x)), y=alt.Y(f"{y}:N", sort=dias, title=None),
            color=alt.Color(f"{g['valor']}:Q", scale=alt.Scale(range=[BEIGE, VERDE]), title=titulo_col(g["valor"])),
            tooltip=tip)
    elif tipo == "linea":
        ch = alt.Chart(d).mark_line(point=True, strokeWidth=3).encode(
            x=alt.X(f"{x}:N", title=None, axis=alt.Axis(labelAngle=-30)), y=alt.Y(f"{y}:Q", title=titulo_col(y)),
            tooltip=tip)
    elif g.get("horizontal"):
        ch = alt.Chart(d).mark_bar(cornerRadiusEnd=6).encode(
            y=alt.Y(f"{x}:N", sort="-x", title=None, axis=alt.Axis(labelLimit=260)),
            x=alt.X(f"{y}:Q", title=titulo_col(y)), tooltip=tip)
    else:
        ch = alt.Chart(d).mark_bar(cornerRadiusEnd=6).encode(
            x=alt.X(f"{x}:N", sort=None, title=None, axis=alt.Axis(labelAngle=0 if len(d) <= 6 else -30)),
            y=alt.Y(f"{y}:Q", title=titulo_col(y)), tooltip=tip)
    if tipo != "calor":
        ch = ch.encode(color=alt.Color(f"{color}:N", scale=alt.Scale(range=PALETA), title=titulo_col(color))
                       if color else alt.value(VERDE))
    st.altair_chart(ch.properties(height=g.get("alto", 320)).configure_view(stroke=None), width="stretch")


def flash(msg, tipo="ok"):
    """Mensaje que sobrevive al st.rerun()."""
    st.session_state["flash"] = (tipo, msg)
    st.rerun()


def ir_a(pagina):
    """Abre otra página de la app (PAGINAS se define en la sección 5)."""
    st.switch_page(PAGINAS[pagina])


@st.cache_data(ttl=3600, show_spinner=False)
def inicio_datos():
    r = consultar("SELECT MIN(t.fecha) AS f FROM fact_reserva r JOIN dim_tiempo t ON t.id_tiempo = r.id_tiempo")
    return r.iloc[0]["f"].date() if not r.empty and pd.notna(r.iloc[0]["f"]) else hoy() - timedelta(days=90)


def sedes():
    return consultar("SELECT DISTINCT sede FROM dim_cancha ORDER BY sede")["sede"].tolist()


# Consulta base: reservas con nombres legibles (JOIN de las 4 tablas del modelo en estrella)
BASE_RESERVAS = """
    SELECT r.id_reserva, r.estado_reserva AS estado, u.nombre || ' ' || u.apellido AS usuario, u.documento,
           c.nombre AS cancha, c.sede, c.deporte, t.fecha, TO_CHAR(t.hora_inicio, 'FM00') || ':00' AS inicio,
           r.duracion_horas AS horas, r.valor_total, r.metodo_pago, r.calificacion
    FROM fact_reserva r
    JOIN dim_usuario u ON u.id_usuario = r.id_usuario
    JOIN dim_cancha  c ON c.id_cancha  = r.id_cancha
    JOIN dim_tiempo  t ON t.id_tiempo  = r.id_tiempo
"""

# Filtros reutilizables dentro del WHERE: si el valor es 'TODAS'/'TODOS' la condición siempre se cumple
F_RANGO = "t.fecha BETWEEN :desde AND :hasta"
F_SEDE = "(:sede = 'TODAS' OR c.sede = :sede)"
F_DEPORTE = "(:deporte = 'TODOS' OR c.deporte = :deporte)"
ACTIVAS = "r.estado_reserva IN ('PENDIENTE', 'RESERVADA', 'PAGADA')"
JOIN_TC = "FROM fact_reserva r JOIN dim_tiempo t ON t.id_tiempo = r.id_tiempo JOIN dim_cancha c ON c.id_cancha = r.id_cancha"

# =============================================== 3. CATÁLOGO DE CONSULTAS GUIADAS =====================================
# Preguntas de negocio en lenguaje administrativo. Cada una tiene su SQL, sus filtros y su gráfica.
CONSULTAS = {
    "recaudo_mes": dict(
        area="Comercial", pregunta="¿Cuánto recaudamos cada mes en cada sede?",
        explicacion="Suma del valor de las reservas pagadas, agrupada por mes de juego y sede.",
        filtros=["rango", "sede"], grafica=dict(x="mes", y="recaudo", color="sede"),
        sql=f"""SELECT TO_CHAR(t.fecha, 'YYYY-MM') AS mes, c.sede, COUNT(*) AS reservas, SUM(r.valor_total) AS recaudo
                {JOIN_TC}
                WHERE r.estado_reserva = 'PAGADA' AND {F_RANGO} AND {F_SEDE}
                GROUP BY TO_CHAR(t.fecha, 'YYYY-MM'), c.sede
                ORDER BY mes, c.sede"""),
    "canchas_ingresos": dict(
        area="Comercial", pregunta="¿Qué canchas generan más ingresos?",
        explicacion="Ranking de canchas por dinero recaudado en reservas pagadas.",
        filtros=["rango", "sede", "deporte"], grafica=dict(x="cancha", y="recaudo", horizontal=True, alto=380),
        sql=f"""SELECT c.nombre || ' · ' || c.sede AS cancha, c.deporte, COUNT(*) AS reservas, SUM(r.valor_total) AS recaudo
                {JOIN_TC}
                WHERE r.estado_reserva = 'PAGADA' AND {F_RANGO} AND {F_SEDE} AND {F_DEPORTE}
                GROUP BY c.id_cancha, c.nombre, c.sede, c.deporte
                ORDER BY recaudo DESC"""),
    "ticket_deporte": dict(
        area="Comercial", pregunta="¿Cuál es el ticket promedio por deporte?",
        explicacion="Valor promedio pagado por reserva y duración promedio en horas, por deporte.",
        filtros=["rango", "sede"], grafica=dict(x="deporte", y="ticket_promedio"),
        sql=f"""SELECT c.deporte, COUNT(*) AS reservas, ROUND(AVG(r.valor_total)) AS ticket_promedio,
                       ROUND(AVG(r.duracion_horas), 2) AS horas_promedio
                {JOIN_TC}
                WHERE r.estado_reserva = 'PAGADA' AND {F_RANGO} AND {F_SEDE}
                GROUP BY c.deporte
                ORDER BY ticket_promedio DESC"""),
    "top_clientes": dict(
        area="Comercial", pregunta="¿Quiénes son los clientes que más gastan?",
        explicacion="Clientes ordenados por gasto en reservas pagadas. Útil para programas de fidelización.",
        filtros=["rango", "top"], grafica=dict(x="cliente", y="gasto", horizontal=True, alto=380),
        sql=f"""SELECT u.nombre || ' ' || u.apellido AS cliente, u.tipo_usuario AS tipo, COUNT(*) AS reservas_pagadas,
                       SUM(r.valor_total) AS gasto, MAX(t.fecha) AS ultima_reserva
                FROM fact_reserva r
                JOIN dim_usuario u ON u.id_usuario = r.id_usuario
                JOIN dim_tiempo  t ON t.id_tiempo  = r.id_tiempo
                WHERE r.estado_reserva = 'PAGADA' AND {F_RANGO}
                GROUP BY u.id_usuario, u.nombre, u.apellido, u.tipo_usuario
                ORDER BY gasto DESC
                FETCH FIRST :top ROWS ONLY"""),
    "medios_pago": dict(
        area="Comercial", pregunta="¿Qué medios de pago prefieren los clientes?",
        explicacion="Participación de cada medio de pago en las reservas pagadas.",
        filtros=["rango", "sede"], grafica=dict(x="metodo_pago", y="reservas"),
        sql=f"""SELECT r.metodo_pago, COUNT(*) AS reservas, SUM(r.valor_total) AS valor,
                       ROUND(100 * RATIO_TO_REPORT(COUNT(*)) OVER (), 1) AS porcentaje
                {JOIN_TC}
                WHERE r.estado_reserva = 'PAGADA' AND {F_RANGO} AND {F_SEDE}
                GROUP BY r.metodo_pago
                ORDER BY reservas DESC"""),
    "por_cobrar": dict(
        area="Comercial", pregunta="¿Cuánto dinero está pendiente de cobro?",
        explicacion="Reservas futuras que aún no se pagan (pendientes y reservadas): ingreso esperado.",
        filtros=["sede"], grafica=dict(x="sede", y="valor", color="estado"),
        sql=f"""SELECT c.sede, r.estado_reserva AS estado, COUNT(*) AS reservas, SUM(r.valor_total) AS valor
                {JOIN_TC}
                WHERE r.estado_reserva IN ('PENDIENTE', 'RESERVADA') AND t.fecha >= :hoy AND {F_SEDE}
                GROUP BY c.sede, r.estado_reserva
                ORDER BY c.sede, estado"""),
    "perdidas": dict(
        area="Comercial", pregunta="¿Cuánto dinero perdimos por reservas vencidas o canceladas?",
        explicacion="Valor de las reservas que no se cobraron, por mes. Mide el costo de la mora y las cancelaciones.",
        filtros=["rango", "sede"], grafica=dict(x="mes", y="valor", color="estado"),
        sql=f"""SELECT TO_CHAR(t.fecha, 'YYYY-MM') AS mes, r.estado_reserva AS estado, COUNT(*) AS reservas,
                       SUM(r.valor_total) AS valor
                {JOIN_TC}
                WHERE r.estado_reserva IN ('VENCIDA', 'CANCELADA') AND {F_RANGO} AND {F_SEDE}
                GROUP BY TO_CHAR(t.fecha, 'YYYY-MM'), r.estado_reserva
                ORDER BY mes, estado"""),
    "reactivar": dict(
        area="Comercial", pregunta="¿Qué clientes llevan tiempo sin reservar?",
        explicacion="Clientes activos cuya última reserva pagada fue hace más de N días. Base para campañas de reactivación.",
        filtros=["dias"], grafica=None,
        sql="""SELECT u.nombre || ' ' || u.apellido AS cliente, u.correo, u.telefono, u.tipo_usuario AS tipo,
                      COUNT(*) AS reservas_pagadas, MAX(t.fecha) AS ultima_reserva,
                      ROUND(:hoy - MAX(t.fecha)) AS dias_sin_reservar
               FROM dim_usuario u
               JOIN fact_reserva r ON r.id_usuario = u.id_usuario AND r.estado_reserva = 'PAGADA'
               JOIN dim_tiempo  t ON t.id_tiempo  = r.id_tiempo
               WHERE u.estado_usuario = 'ACTIVO'
               GROUP BY u.id_usuario, u.nombre, u.apellido, u.correo, u.telefono, u.tipo_usuario
               HAVING :hoy - MAX(t.fecha) >= :dias
               ORDER BY dias_sin_reservar DESC"""),
    "tipo_usuario": dict(
        area="Comercial", pregunta="¿Cómo se comporta cada tipo de usuario?",
        explicacion="Reservas, gasto y calificación promedio por tipo de usuario.",
        filtros=["rango", "sede"], grafica=dict(x="tipo", y="gasto"),
        sql=f"""SELECT u.tipo_usuario AS tipo, COUNT(*) AS reservas,
                       SUM(CASE WHEN r.estado_reserva = 'PAGADA' THEN r.valor_total ELSE 0 END) AS gasto,
                       ROUND(AVG(r.calificacion), 2) AS calificacion_promedio
                {JOIN_TC}
                JOIN dim_usuario u ON u.id_usuario = r.id_usuario
                WHERE {F_RANGO} AND {F_SEDE}
                GROUP BY u.tipo_usuario
                ORDER BY gasto DESC"""),
    "ocupacion": dict(
        area="Operativa", pregunta="¿Qué tan ocupada está cada cancha?",
        explicacion="Horas reservadas (pendientes, reservadas y pagadas) frente a las 16 horas diarias disponibles.",
        filtros=["rango", "sede", "deporte"], grafica=dict(x="cancha", y="ocupacion_pct", horizontal=True, alto=400),
        sql=f"""SELECT c.nombre || ' · ' || c.sede AS cancha, c.deporte, c.estado_cancha,
                       NVL(SUM(x.duracion_horas), 0) AS horas_reservadas, :ndias * 16 AS horas_disponibles,
                       ROUND(100 * NVL(SUM(x.duracion_horas), 0) / (:ndias * 16), 1) AS ocupacion_pct
                FROM dim_cancha c
                LEFT JOIN (SELECT r.id_cancha, r.duracion_horas
                           FROM fact_reserva r JOIN dim_tiempo t ON t.id_tiempo = r.id_tiempo
                           WHERE {ACTIVAS} AND {F_RANGO}) x ON x.id_cancha = c.id_cancha
                WHERE {F_SEDE} AND {F_DEPORTE}
                GROUP BY c.id_cancha, c.nombre, c.sede, c.deporte, c.estado_cancha
                ORDER BY ocupacion_pct DESC"""),
    "horas_pico": dict(
        area="Operativa", pregunta="¿Cuáles son las horas pico de la semana?",
        explicacion="Mapa de calor de reservas activas por día de la semana y hora de inicio.",
        filtros=["rango", "sede", "deporte"], grafica=dict(tipo="calor", x="hora_inicio", y="dia_semana", valor="reservas", alto=300),
        sql=f"""SELECT t.dia_semana, t.hora_inicio, COUNT(*) AS reservas
                {JOIN_TC}
                WHERE {ACTIVAS} AND {F_RANGO} AND {F_SEDE} AND {F_DEPORTE}
                GROUP BY t.dia_semana, t.hora_inicio
                ORDER BY t.hora_inicio"""),
    "agenda": dict(
        area="Operativa", pregunta="¿Qué reservas hay para un día?",
        explicacion="Agenda del día por sede y cancha, con datos de contacto del usuario.",
        filtros=["dia", "sede"], grafica=None,
        sql=f"""SELECT c.sede, c.nombre AS cancha,
                       TO_CHAR(t.hora_inicio, 'FM00') || ':00 - ' || TO_CHAR(t.hora_inicio + r.duracion_horas, 'FM00') || ':00' AS horario,
                       u.nombre || ' ' || u.apellido AS usuario, u.telefono, r.estado_reserva AS estado, r.valor_total
                {JOIN_TC}
                JOIN dim_usuario u ON u.id_usuario = r.id_usuario
                WHERE {ACTIVAS} AND t.fecha = :dia AND {F_SEDE}
                ORDER BY c.sede, c.nombre, t.hora_inicio"""),
    "demanda_deporte": dict(
        area="Operativa", pregunta="¿Qué deportes tienen más demanda?",
        explicacion="Reservas y horas solicitadas por deporte (sin contar canceladas).",
        filtros=["rango", "sede"], grafica=dict(x="deporte", y="horas"),
        sql=f"""SELECT c.deporte, COUNT(*) AS reservas, SUM(r.duracion_horas) AS horas
                {JOIN_TC}
                WHERE r.estado_reserva <> 'CANCELADA' AND {F_RANGO} AND {F_SEDE}
                GROUP BY c.deporte
                ORDER BY horas DESC"""),
    "tipo_dia": dict(
        area="Operativa", pregunta="¿Se juega más en festivos y fines de semana?",
        explicacion="Promedio de reservas por día según el tipo de día.",
        filtros=["rango", "sede"], grafica=dict(x="tipo_dia", y="reservas_por_dia"),
        sql=f"""SELECT CASE WHEN t.es_festivo = 'SI' THEN 'Festivo'
                            WHEN t.dia_semana IN ('SABADO', 'DOMINGO') THEN 'Fin de semana'
                            ELSE 'Entre semana' END AS tipo_dia,
                       COUNT(DISTINCT t.fecha) AS dias_con_reservas, COUNT(*) AS reservas,
                       ROUND(COUNT(*) / COUNT(DISTINCT t.fecha), 1) AS reservas_por_dia
                {JOIN_TC}
                WHERE {ACTIVAS} AND {F_RANGO} AND {F_SEDE}
                GROUP BY CASE WHEN t.es_festivo = 'SI' THEN 'Festivo'
                              WHEN t.dia_semana IN ('SABADO', 'DOMINGO') THEN 'Fin de semana'
                              ELSE 'Entre semana' END
                ORDER BY reservas_por_dia DESC"""),
    "calificacion_cancha": dict(
        area="Operativa", pregunta="¿Qué canchas tienen peor calificación?",
        explicacion="Calificación promedio (1 a 5) por cancha y cantidad de calificaciones bajas (1 o 2).",
        filtros=["rango", "sede"], grafica=dict(x="cancha", y="promedio", horizontal=True, alto=380),
        sql=f"""SELECT c.nombre || ' · ' || c.sede AS cancha, c.deporte, COUNT(r.calificacion) AS calificaciones,
                       ROUND(AVG(r.calificacion), 2) AS promedio,
                       SUM(CASE WHEN r.calificacion <= 2 THEN 1 ELSE 0 END) AS calificaciones_bajas
                {JOIN_TC}
                WHERE r.calificacion IS NOT NULL AND {F_RANGO} AND {F_SEDE}
                GROUP BY c.id_cancha, c.nombre, c.sede, c.deporte
                ORDER BY promedio"""),
    "mora": dict(
        area="Operativa", pregunta="¿Qué usuarios acumulan más reservas vencidas?",
        explicacion="Usuarios con reservas vencidas (no confirmadas o no pagadas a tiempo) y el valor que no se cobró.",
        filtros=["top"], grafica=dict(x="usuario", y="vencidas", horizontal=True, alto=380),
        sql="""SELECT u.nombre || ' ' || u.apellido AS usuario, u.estado_usuario AS estado, COUNT(*) AS reservas,
                      SUM(CASE WHEN r.estado_reserva = 'VENCIDA' THEN 1 ELSE 0 END) AS vencidas,
                      ROUND(100 * SUM(CASE WHEN r.estado_reserva = 'VENCIDA' THEN 1 ELSE 0 END) / COUNT(*), 1) AS vencidas_pct,
                      SUM(CASE WHEN r.estado_reserva = 'VENCIDA' THEN r.valor_total ELSE 0 END) AS valor_no_cobrado
               FROM fact_reserva r
               JOIN dim_usuario u ON u.id_usuario = r.id_usuario
               GROUP BY u.id_usuario, u.nombre, u.apellido, u.estado_usuario
               HAVING SUM(CASE WHEN r.estado_reserva = 'VENCIDA' THEN 1 ELSE 0 END) > 0
               ORDER BY vencidas DESC, vencidas_pct DESC
               FETCH FIRST :top ROWS ONLY"""),
    "estados": dict(
        area="Operativa", pregunta="¿Cómo se distribuyen las reservas por estado?",
        explicacion="Cantidad y porcentaje de reservas en cada estado del ciclo de vida.",
        filtros=["rango", "sede"], grafica=dict(x="estado", y="reservas"),
        sql=f"""SELECT r.estado_reserva AS estado, COUNT(*) AS reservas, SUM(r.valor_total) AS valor,
                       ROUND(100 * RATIO_TO_REPORT(COUNT(*)) OVER (), 1) AS porcentaje
                {JOIN_TC}
                WHERE {F_RANGO} AND {F_SEDE}
                GROUP BY r.estado_reserva
                ORDER BY reservas DESC"""),
    "tendencia": dict(
        area="Comercial", pregunta="¿Cómo evolucionan las reservas semana a semana?",
        explicacion="Reservas activas por semana de juego (lunes de cada semana).",
        filtros=["rango", "sede"], grafica=dict(tipo="linea", x="semana", y="reservas"),
        sql=f"""SELECT TO_CHAR(TRUNC(t.fecha, 'IW'), 'YYYY-MM-DD') AS semana, COUNT(*) AS reservas,
                       SUM(CASE WHEN r.estado_reserva = 'PAGADA' THEN r.valor_total ELSE 0 END) AS recaudo
                {JOIN_TC}
                WHERE {ACTIVAS} AND {F_RANGO} AND {F_SEDE}
                GROUP BY TRUNC(t.fecha, 'IW')
                ORDER BY semana"""),
}


def parametros_base(desde, hasta, sede="TODAS", deporte="TODOS", top=10, dias=30, dia=None):
    """Arma el diccionario de parámetros; consultar() descarta los que el SQL no usa."""
    return dict(desde=desde, hasta=hasta, ndias=(hasta - desde).days + 1, sede=sede, deporte=deporte,
                top=top, dias=dias, dia=dia or hoy(), hoy=hoy(), ahora=ahora())


def pedir_filtros(filtros, clave):
    """Dibuja solo los filtros que la consulta necesita."""
    valores = {}
    columnas = st.columns(len(filtros)) if filtros else []
    for col, f in zip(columnas, filtros):
        with col:
            if f == "rango":
                r = st.date_input("Periodo", value=(inicio_datos(), hoy()), key=f"{clave}_rango", format="DD/MM/YYYY")
                valores["desde"], valores["hasta"] = (r[0], r[-1]) if isinstance(r, tuple) and r else (inicio_datos(), hoy())
            elif f == "sede":
                valores["sede"] = st.selectbox("Sede", ["TODAS"] + sedes(), format_func=etiqueta, key=f"{clave}_sede")
            elif f == "deporte":
                valores["deporte"] = st.selectbox("Deporte", ["TODOS"] + DEPORTES, format_func=etiqueta, key=f"{clave}_dep")
            elif f == "top":
                valores["top"] = st.slider("Cantidad a mostrar", 5, 30, 10, key=f"{clave}_top")
            elif f == "dias":
                valores["dias"] = st.slider("Días sin reservar (mínimo)", 7, 90, 30, key=f"{clave}_dias")
            elif f == "dia":
                valores["dia"] = st.date_input("Día", value=hoy(), key=f"{clave}_dia", format="DD/MM/YYYY")
    return parametros_base(**{"desde": inicio_datos(), "hasta": hoy(), **valores})


def mostrar_consulta(clave, params, ver_sql=True):
    q = CONSULTAS[clave]
    df = consultar(q["sql"], params)
    grafica(df, q["grafica"])
    tabla(df)
    if not df.empty:
        boton_excel(df, clave, f"xls_{clave}")
    if ver_sql:
        with st.expander("Ver la consulta SQL"):
            st.code(re.sub(r"\n\s{8,}", "\n    ", q["sql"]).strip(), language="sql")


# =============================================== 4. PÁGINAS ===========================================================
# ------------------------------------------------- Inicio -------------------------------------------------------------
def pagina_inicio():
    encabezado("Panel del club", f"Resumen operativo y comercial · {hoy():%d/%m/%Y}")
    k = consultar("""
        SELECT (SELECT COUNT(*) FROM dim_cancha)                                       AS canchas,
               (SELECT COUNT(*) FROM dim_cancha WHERE estado_cancha = 'DISPONIBLE')    AS disponibles,
               (SELECT COUNT(*) FROM dim_cancha WHERE estado_cancha = 'MANTENIMIENTO') AS mantenimiento,
               (SELECT COUNT(*) FROM dim_cancha WHERE estado_cancha = 'CERRADA')       AS cerradas,
               (SELECT COUNT(*) FROM dim_usuario WHERE estado_usuario = 'ACTIVO')      AS usuarios_activos,
               (SELECT COUNT(*) FROM fact_reserva WHERE estado_reserva = 'PENDIENTE')  AS por_confirmar,
               (SELECT COUNT(*) FROM fact_reserva r JOIN dim_tiempo t ON t.id_tiempo = r.id_tiempo
                 WHERE r.estado_reserva IN ('PENDIENTE', 'RESERVADA', 'PAGADA')
                   AND t.fecha BETWEEN :hoy AND :hoy + 6)                              AS proximos_7_dias,
               (SELECT NVL(SUM(r.valor_total), 0) FROM fact_reserva r JOIN dim_tiempo t ON t.id_tiempo = r.id_tiempo
                 WHERE r.estado_reserva = 'PAGADA' AND t.fecha BETWEEN TRUNC(:hoy, 'MM') AND :hoy) AS recaudo_mes
        FROM dual""", {"hoy": hoy()}).iloc[0]

    c = st.columns(4)
    c[0].metric("Canchas disponibles", f"{int(k.disponibles)} / {int(k.canchas)}")
    c[1].metric("En mantenimiento / cerradas", f"{int(k.mantenimiento)} / {int(k.cerradas)}")
    c[2].metric("Usuarios activos", int(k.usuarios_activos))
    c[3].metric("Recaudo del mes", pesos(k.recaudo_mes))
    c = st.columns(4)
    c[0].metric("Reservas próximos 7 días", int(k.proximos_7_dias))
    c[1].metric("Solicitudes por confirmar", int(k.por_confirmar))
    with c[2]:
        st.write("")
        if st.button("➕ Solicitar una cancha", type="primary", width="stretch"):
            ir_a("Solicitar cancha")
    with c[3]:
        st.write("")
        if st.button("👤 Registrar usuario", width="stretch"):
            ir_a("Registrar usuario")

    izq, der = st.columns([5, 3])
    with izq:
        st.subheader("Usuarios solicitando canchas")
        st.caption("Solicitudes activas que aún no se pagan, de la más próxima a la más lejana.")
        sol = consultar(BASE_RESERVAS + """
            WHERE r.estado_reserva IN ('PENDIENTE', 'RESERVADA') AND t.fecha >= :hoy
            ORDER BY t.fecha, t.hora_inicio""", {"hoy": hoy()})
        tabla(sol, ocultar=["id_reserva", "documento", "calificacion", "metodo_pago", "deporte"])
    with der:
        st.subheader("Canchas más solicitadas")
        st.caption("Reservas de los últimos 30 días (sin canceladas).")
        top = consultar("""
            SELECT c.nombre || ' · ' || c.sede AS cancha, COUNT(*) AS reservas
            FROM fact_reserva r JOIN dim_tiempo t ON t.id_tiempo = r.id_tiempo JOIN dim_cancha c ON c.id_cancha = r.id_cancha
            WHERE r.estado_reserva <> 'CANCELADA' AND t.fecha BETWEEN :hoy - 30 AND :hoy
            GROUP BY c.id_cancha, c.nombre, c.sede
            ORDER BY reservas DESC
            FETCH FIRST 8 ROWS ONLY""", {"hoy": hoy()})
        grafica(top, dict(x="cancha", y="reservas", horizontal=True, alto=300))

    st.subheader("Estado de las canchas por sede")
    estado = consultar("""
        SELECT sede, deporte, COUNT(*) AS canchas,
               SUM(CASE WHEN estado_cancha = 'DISPONIBLE'    THEN 1 ELSE 0 END) AS disponibles,
               SUM(CASE WHEN estado_cancha = 'MANTENIMIENTO' THEN 1 ELSE 0 END) AS en_mantenimiento,
               SUM(CASE WHEN estado_cancha = 'CERRADA'       THEN 1 ELSE 0 END) AS cerradas,
               MIN(precio_hora) AS precio_desde
        FROM dim_cancha
        GROUP BY sede, deporte
        ORDER BY sede, canchas DESC""")
    tabla(estado)


# ------------------------------------------------- Solicitar cancha ---------------------------------------------------
def pagina_solicitar():
    encabezado("Solicitar una cancha", "Elige usuario, sede, deporte y horario. Solo se ofrecen horas libres.")

    with st.expander("🔎 Consultar solicitantes registrados (tabla dim_usuario)"):
        busca = st.text_input("Buscar por nombre, apellido o documento")
        solicitantes = consultar("""
            SELECT u.documento, u.nombre || ' ' || u.apellido AS usuario, u.tipo_usuario AS tipo,
                   u.estado_usuario AS estado, u.fecha_registro, COUNT(r.id_reserva) AS reservas,
                   SUM(CASE WHEN r.estado_reserva = 'VENCIDA' THEN 1 ELSE 0 END) AS vencidas
            FROM dim_usuario u
            LEFT JOIN fact_reserva r ON r.id_usuario = u.id_usuario
            WHERE UPPER(u.nombre || ' ' || u.apellido || ' ' || u.documento) LIKE UPPER(:q)
            GROUP BY u.id_usuario, u.documento, u.nombre, u.apellido, u.tipo_usuario, u.estado_usuario, u.fecha_registro
            ORDER BY usuario""", {"q": f"%{busca.strip()}%"})
        tabla(solicitantes)

    # 1. Usuario (regla: solo usuarios ACTIVO pueden solicitar)
    usuarios = consultar("""
        SELECT id_usuario, documento, nombre || ' ' || apellido AS nombre
        FROM dim_usuario WHERE estado_usuario = 'ACTIVO' ORDER BY nombre""")
    if usuarios.empty:
        st.warning("No hay usuarios activos. Registra uno primero.")
        return
    ids = [int(i) for i in usuarios.id_usuario]
    nombre_u = {int(r.id_usuario): f"{r.nombre} · {r.documento}" for r in usuarios.itertuples()}
    nuevo = st.session_state.get("usuario_nuevo")
    c1, c2, c3 = st.columns(3)
    id_usuario = c1.selectbox("Usuario que solicita", ids, index=ids.index(nuevo) if nuevo in ids else 0,
                              format_func=lambda i: nombre_u[i])

    # 2. Sede, deporte y cancha (regla: solo canchas DISPONIBLE)
    canchas = consultar("""
        SELECT id_cancha, nombre, deporte, sede, superficie, capacidad, precio_hora
        FROM dim_cancha WHERE estado_cancha = 'DISPONIBLE' ORDER BY sede, deporte, nombre""")
    if canchas.empty:
        st.warning("No hay canchas disponibles.")
        return
    sede = c2.selectbox("Sede", sorted(canchas.sede.unique()))
    deportes = [d for d in DEPORTES if d in canchas[canchas.sede == sede].deporte.values]
    deporte = c3.selectbox("Deporte", deportes, format_func=etiqueta)
    opciones = canchas[(canchas.sede == sede) & (canchas.deporte == deporte)]
    info = {int(r.id_cancha): r for r in opciones.itertuples()}

    c1, c2, c3 = st.columns(3)
    id_cancha = c1.selectbox("Cancha", list(info), format_func=lambda i: (
        f"{info[i].nombre} · {etiqueta(info[i].superficie)} · {pesos(info[i].precio_hora)}/hora"))
    cancha = info[id_cancha]

    # 3. Fecha (solo fechas del calendario dim_tiempo y no pasadas)
    rango = consultar("SELECT MIN(fecha) AS fmin, MAX(fecha) AS fmax FROM dim_tiempo").iloc[0]
    fecha_min, fecha_max = max(hoy(), rango.fmin.date()), rango.fmax.date()
    if fecha_min > fecha_max:
        st.warning("El calendario de dim_tiempo terminó. Hay que ampliarlo con el INSERT ... SELECT del DML.")
        return
    fecha = c2.date_input("Fecha", value=fecha_min, min_value=fecha_min, max_value=fecha_max, format="DD/MM/YYYY")
    duracion = c3.selectbox("Duración", DURACIONES, format_func=lambda h: f"{h} hora{'s' if h > 1 else ''}")

    # 4. Horas libres: todo se calcula en SQL (sin cruces con otras reservas activas de la cancha)
    params = {"fecha": fecha, "cancha": id_cancha, "dur": duracion, "abre": HORA_ABRE, "cierra": HORA_CIERRA,
              "hmin": ahora().hour + 1 if fecha == hoy() else 0}
    libres = consultar(f"""
        SELECT t.hora_inicio
        FROM dim_tiempo t
        WHERE t.fecha = :fecha
          AND t.hora_inicio >= GREATEST(:abre, :hmin)
          AND t.hora_inicio + :dur <= :cierra
          AND NOT EXISTS (SELECT 1
                          FROM fact_reserva r JOIN dim_tiempo tr ON tr.id_tiempo = r.id_tiempo
                          WHERE r.id_cancha = :cancha AND tr.fecha = t.fecha AND {ACTIVAS}
                            AND tr.hora_inicio < t.hora_inicio + :dur
                            AND tr.hora_inicio + r.duracion_horas > t.hora_inicio)
        ORDER BY t.hora_inicio""", params)
    horas = [int(h) for h in libres.hora_inicio]

    ocupadas = consultar(f"""
        SELECT TO_CHAR(t.hora_inicio, 'FM00') || ':00' AS desde,
               TO_CHAR(t.hora_inicio + r.duracion_horas, 'FM00') || ':00' AS hasta, r.estado_reserva AS estado
        FROM fact_reserva r JOIN dim_tiempo t ON t.id_tiempo = r.id_tiempo
        WHERE r.id_cancha = :cancha AND t.fecha = :fecha AND {ACTIVAS}
        ORDER BY t.hora_inicio""", {"cancha": id_cancha, "fecha": fecha})
    if not ocupadas.empty:
        with st.expander(f"Horarios ya ocupados en {cancha.nombre} el {fecha:%d/%m/%Y} ({len(ocupadas)})"):
            tabla(ocupadas)

    if not horas:
        st.info("No hay horarios libres para esa cancha, fecha y duración. Prueba otra combinación.")
        return
    c1, c2, c3 = st.columns(3)
    hora = c1.selectbox("Hora de inicio", horas, format_func=lambda h: f"{h:02d}:00 a {h + duracion:02d}:00")
    metodo = c2.selectbox("Método de pago", METODOS, format_func=etiqueta)
    c3.metric("Valor total", pesos(duracion * float(cancha.precio_hora)))

    if st.button("Solicitar cancha", type="primary"):
        # El INSERT vuelve a validar todas las reglas (usuario ACTIVO, cancha DISPONIBLE, horario futuro,
        # dentro del horario de atención y sin cruces) y calcula valor_total = horas x precio en SQL.
        # Si algo cambió mientras el usuario elegía, no inserta nada (0 filas).
        try:
            with transaccion() as tx:
                n = tx.run(f"""
                    INSERT INTO fact_reserva (id_usuario, id_cancha, id_tiempo, duracion_horas, valor_total,
                                              metodo_pago, estado_reserva, fecha_creacion)
                    SELECT :usuario, c.id_cancha, t.id_tiempo, :dur, :dur * c.precio_hora, :metodo, 'PENDIENTE', :ahora
                    FROM dim_cancha c CROSS JOIN dim_tiempo t
                    WHERE c.id_cancha = :cancha AND c.estado_cancha = 'DISPONIBLE'
                      AND t.fecha = :fecha AND t.hora_inicio = :hora
                      AND t.fecha + t.hora_inicio / 24 > :ahora
                      AND t.hora_inicio + :dur <= :cierra
                      AND EXISTS (SELECT 1 FROM dim_usuario u WHERE u.id_usuario = :usuario AND u.estado_usuario = 'ACTIVO')
                      AND NOT EXISTS (SELECT 1
                                      FROM fact_reserva r JOIN dim_tiempo tr ON tr.id_tiempo = r.id_tiempo
                                      WHERE r.id_cancha = c.id_cancha AND tr.fecha = t.fecha AND {ACTIVAS}
                                        AND tr.hora_inicio < t.hora_inicio + :dur
                                        AND tr.hora_inicio + r.duracion_horas > t.hora_inicio)""",
                           {"usuario": id_usuario, "cancha": id_cancha, "fecha": fecha, "hora": hora, "dur": duracion,
                            "metodo": metodo, "ahora": ahora(), "cierra": HORA_CIERRA})
            if n == 1:
                flash(f"Solicitud creada (PENDIENTE): {cancha.nombre} · {sede}, {fecha:%d/%m/%Y} "
                      f"de {hora:02d}:00 a {hora + duracion:02d}:00.")
            else:
                st.error("No se pudo crear la solicitud: el horario ya no está libre o los datos cambiaron.")
        except oracledb.IntegrityError:
            st.error("Otra reserva activa ya ocupa ese bloque. Elige otra hora.")

    st.subheader("Reservas de este usuario")
    mias = consultar(BASE_RESERVAS + " WHERE r.id_usuario = :usuario ORDER BY t.fecha DESC, t.hora_inicio DESC",
                     {"usuario": id_usuario})
    tabla(mias, ocultar=["id_reserva", "usuario", "documento"])


# ------------------------------------------------- Registrar usuario --------------------------------------------------
RE_DOC = re.compile(r"^[A-Za-z0-9]{5,20}$")
RE_CORREO = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
RE_TEL = re.compile(r"^\+?[0-9]{7,15}$")


def pagina_registro():
    encabezado("Registrar usuario", "El usuario queda ACTIVO y puede solicitar una cancha de inmediato.")
    if "registro_msg" in st.session_state:
        st.success(st.session_state["registro_msg"])
        if st.button("Solicitar una cancha para este usuario", type="primary"):
            st.session_state.pop("registro_msg", None)
            ir_a("Solicitar cancha")

    with st.form("form_usuario", clear_on_submit=True):
        c1, c2 = st.columns(2)
        documento = c1.text_input("Documento (5 a 20 letras o números)")
        tipo = c2.selectbox("Tipo de usuario", TIPOS_USUARIO, format_func=etiqueta)
        nombre = c1.text_input("Nombre")
        apellido = c2.text_input("Apellido")
        correo = c1.text_input("Correo")
        telefono = c2.text_input("Teléfono (opcional)")
        enviar = st.form_submit_button("Registrar usuario", type="primary")
    if not enviar:
        return

    # Validaciones en la app (la base de datos las repite con CHECK y UNIQUE)
    documento, nombre, apellido = documento.strip(), nombre.strip(), apellido.strip()
    correo, telefono = correo.strip().lower(), telefono.strip().replace(" ", "")
    errores = []
    if not RE_DOC.match(documento):
        errores.append("El documento debe tener entre 5 y 20 letras o números, sin espacios.")
    if not nombre or not apellido:
        errores.append("El nombre y el apellido son obligatorios.")
    if not RE_CORREO.match(correo):
        errores.append("El correo no tiene un formato válido (ejemplo: nombre@dominio.com).")
    if telefono and not RE_TEL.match(telefono):
        errores.append("El teléfono debe tener de 7 a 15 dígitos (puede iniciar con '+').")
    if errores:
        for e in errores:
            st.error(e)
        return

    repetidos = consultar("SELECT COUNT(*) AS n FROM dim_usuario WHERE documento = :d OR correo = :c",
                          {"d": documento, "c": correo}).iloc[0]["n"]
    if int(repetidos) > 0:
        st.error("Ya existe un usuario con ese documento o ese correo.")
        return
    try:
        with transaccion() as tx:
            nuevo_id = tx.insert("""
                INSERT INTO dim_usuario (documento, nombre, apellido, correo, telefono, tipo_usuario, fecha_registro, estado_usuario)
                VALUES (:documento, :nombre, :apellido, :correo, :telefono, :tipo, :ahora, 'ACTIVO')""", "id_usuario",
                                 {"documento": documento, "nombre": nombre, "apellido": apellido, "correo": correo,
                                  "telefono": telefono or None, "tipo": tipo, "ahora": ahora()})
        st.session_state["usuario_nuevo"] = nuevo_id
        st.session_state["registro_msg"] = f"Usuario #{nuevo_id} · {nombre} {apellido} registrado correctamente."
        st.rerun()
    except oracledb.IntegrityError:
        st.error("No se pudo registrar: documento o correo repetidos.")


# ------------------------------------------------- Reservas por estado ------------------------------------------------
def pagina_reservas():
    encabezado("Reservas por estado", "Pendientes, reservadas, pagadas, vencidas y canceladas.")

    resumen = consultar("SELECT estado_reserva AS estado, COUNT(*) AS n FROM fact_reserva GROUP BY estado_reserva")
    conteo = dict(zip(resumen.estado, resumen.n.astype(int)))
    for col, e in zip(st.columns(len(ESTADOS)), ESTADOS):
        col.metric(etiqueta(e) + "s", conteo.get(e, 0))

    # Regla: una reserva PENDIENTE o RESERVADA pasa a VENCIDA si llega su hora de inicio sin pago
    por_vencer = int(consultar("""
        SELECT COUNT(*) AS n FROM fact_reserva r JOIN dim_tiempo t ON t.id_tiempo = r.id_tiempo
        WHERE r.estado_reserva IN ('PENDIENTE', 'RESERVADA') AND t.fecha + t.hora_inicio / 24 <= :ahora""",
                               {"ahora": ahora()}).iloc[0]["n"])
    if por_vencer:
        st.warning(f"Hay {por_vencer} reservas sin pago cuya hora de inicio ya pasó.")
        if st.button(f"Marcar {por_vencer} reservas como VENCIDAS"):
            with transaccion() as tx:
                n = tx.run("""
                    UPDATE fact_reserva SET estado_reserva = 'VENCIDA'
                    WHERE estado_reserva IN ('PENDIENTE', 'RESERVADA')
                      AND id_tiempo IN (SELECT id_tiempo FROM dim_tiempo WHERE fecha + hora_inicio / 24 <= :ahora)""",
                           {"ahora": ahora()})
            flash(f"Reservas marcadas como vencidas: {n}.")

    c1, c2 = st.columns(2)
    sede = c1.selectbox("Sede", ["TODAS"] + sedes(), format_func=etiqueta)
    solo_futuras = c2.toggle("Solo reservas de hoy en adelante", value=False)
    for pestana, estado in zip(st.tabs([etiqueta(e) + "s" for e in ESTADOS]), ESTADOS):
        with pestana:
            df = consultar(BASE_RESERVAS + f"""
                WHERE r.estado_reserva = :estado AND {F_SEDE} AND (:futuras = 0 OR t.fecha >= :hoy)
                ORDER BY t.fecha DESC, t.hora_inicio DESC""",
                           {"estado": estado, "sede": sede, "futuras": int(solo_futuras), "hoy": hoy()})
            tabla(df, ocultar=["estado"])

    st.subheader("Cambiar el estado de una reserva")
    c1, c2, c3 = st.columns([1, 2, 1])
    actual = c1.selectbox("Estado actual", list(SIGUIENTE), format_func=etiqueta)
    abiertas = consultar(BASE_RESERVAS + """
        WHERE r.estado_reserva = :estado AND t.fecha >= :hoy - 7
        ORDER BY t.fecha, t.hora_inicio""", {"estado": actual, "hoy": hoy()})
    if abiertas.empty:
        st.caption(f"No hay reservas {etiqueta(actual).lower()}s recientes.")
    else:
        info = {int(r.id_reserva): r for r in abiertas.itertuples()}
        id_res = c2.selectbox("Reserva", list(info), format_func=lambda i: (
            f"#{i} · {info[i].usuario} · {info[i].cancha} ({info[i].sede}) · {info[i].fecha:%d/%m} {info[i].inicio}"))
        nuevo = c3.selectbox("Nuevo estado", SIGUIENTE[actual], format_func=etiqueta)
        if st.button("Cambiar estado", type="primary"):
            with transaccion() as tx:
                # WHERE estado_reserva = :actual evita cambiar una reserva que otro usuario ya modificó
                n = tx.run("""
                    UPDATE fact_reserva
                    SET estado_reserva = :nuevo,
                        calificacion = CASE WHEN :nuevo = 'PAGADA' THEN calificacion ELSE NULL END
                    WHERE id_reserva = :id AND estado_reserva = :actual""",
                           {"nuevo": nuevo, "id": id_res, "actual": actual})
            if n == 1:
                flash(f"La reserva #{id_res} ahora está {etiqueta(nuevo).lower()}.")
            else:
                flash("La reserva cambió de estado mientras tanto; vuelve a intentarlo.", "error")

    st.subheader("Calificar una reserva pagada")
    por_calificar = consultar(BASE_RESERVAS + """
        WHERE r.estado_reserva = 'PAGADA' AND r.calificacion IS NULL
          AND t.fecha + (t.hora_inicio + r.duracion_horas) / 24 <= :ahora AND t.fecha >= :hoy - 30
        ORDER BY t.fecha DESC, t.hora_inicio DESC""", {"ahora": ahora(), "hoy": hoy()})
    if por_calificar.empty:
        st.caption("No hay reservas pagadas y terminadas pendientes de calificar.")
    else:
        info = {int(r.id_reserva): r for r in por_calificar.itertuples()}
        c1, c2 = st.columns([3, 1])
        id_cal = c1.selectbox("Reserva a calificar", list(info),
                              format_func=lambda i: f"#{i} · {info[i].usuario} · {info[i].cancha} · {info[i].fecha:%d/%m}")
        nota = c2.select_slider("Calificación", options=[1, 2, 3, 4, 5], value=5, format_func=lambda n: "★" * n)
        if st.button("Guardar calificación"):
            with transaccion() as tx:
                n = tx.run("""UPDATE fact_reserva SET calificacion = :nota
                              WHERE id_reserva = :id AND estado_reserva = 'PAGADA' AND calificacion IS NULL""",
                           {"nota": nota, "id": id_cal})
            flash(f"Calificación guardada para la reserva #{id_cal}." if n == 1 else "No se pudo guardar.",
                  "ok" if n == 1 else "error")


# ------------------------------------------------- Canchas ------------------------------------------------------------
def pagina_canchas():
    encabezado("Gestión de canchas", "Estado operativo, precios y reservas afectadas por mantenimiento.")
    canchas = consultar(f"""
        SELECT c.id_cancha, c.sede, c.nombre AS cancha, c.deporte, c.superficie, c.capacidad, c.precio_hora,
               c.estado_cancha AS estado,
               (SELECT COUNT(*) FROM fact_reserva r JOIN dim_tiempo t ON t.id_tiempo = r.id_tiempo
                 WHERE r.id_cancha = c.id_cancha AND {ACTIVAS} AND t.fecha >= :hoy) AS reservas_futuras
        FROM dim_cancha c
        ORDER BY c.sede, c.deporte, c.nombre""", {"hoy": hoy()})
    tabla(canchas, ocultar=["id_cancha"])

    st.subheader("Actualizar una cancha")
    info = {int(r.id_cancha): r for r in canchas.itertuples()}
    c1, c2, c3 = st.columns([2, 1, 1])
    id_c = c1.selectbox("Cancha", list(info), format_func=lambda i: f"{info[i].cancha} · {info[i].sede} ({etiqueta(info[i].estado)})")
    actual = info[id_c]
    nuevo_estado = c2.selectbox("Estado", ESTADOS_CANCHA, index=ESTADOS_CANCHA.index(actual.estado) if actual.estado in ESTADOS_CANCHA else 0,
                                format_func=etiqueta)
    nuevo_precio = c3.number_input("Precio por hora", min_value=0, step=5000, value=int(actual.precio_hora))
    st.caption("Un cambio de precio solo aplica a reservas nuevas: las existentes conservan su valor histórico.")

    cancelar = False
    if nuevo_estado != "DISPONIBLE":
        # Regla: al sacar una cancha de servicio, se revisan las reservas activas futuras
        afectadas = consultar(BASE_RESERVAS + f"""
            WHERE r.id_cancha = :cancha AND {ACTIVAS} AND t.fecha + t.hora_inicio / 24 > :ahora
            ORDER BY t.fecha, t.hora_inicio""", {"cancha": id_c, "ahora": ahora()})
        if not afectadas.empty:
            st.warning(f"Esta cancha tiene {len(afectadas)} reservas activas futuras.")
            tabla(afectadas, ocultar=["id_reserva", "cancha", "sede", "deporte", "calificacion"])
            cancelar = st.checkbox(f"Cancelar también esas {len(afectadas)} reservas")

    if st.button("Guardar cambios", type="primary"):
        with transaccion() as tx:
            tx.run("UPDATE dim_cancha SET estado_cancha = :estado, precio_hora = :precio WHERE id_cancha = :cancha",
                   {"estado": nuevo_estado, "precio": nuevo_precio, "cancha": id_c})
            n = 0
            if cancelar:
                n = tx.run(f"""
                    UPDATE fact_reserva r SET estado_reserva = 'CANCELADA', calificacion = NULL
                    WHERE r.id_cancha = :cancha AND {ACTIVAS}
                      AND r.id_tiempo IN (SELECT id_tiempo FROM dim_tiempo WHERE fecha + hora_inicio / 24 > :ahora)""",
                           {"cancha": id_c, "ahora": ahora()})
        flash(f"{actual.cancha} · {actual.sede} actualizada a {etiqueta(nuevo_estado).lower()}."
              + (f" Reservas canceladas: {n}." if n else ""))


# ------------------------------------------------- Consultas guiadas --------------------------------------------------
def pagina_consultas():
    encabezado("Consultas guiadas", "Preguntas de negocio respondidas con SQL, sin escribir código.")
    area = st.radio("Área", ["Todas", "Comercial", "Operativa"], horizontal=True)
    claves = [k for k, q in CONSULTAS.items() if area in ("Todas", q["area"])]
    clave = st.selectbox("¿Qué quieres saber?", claves,
                         format_func=lambda k: f"{CONSULTAS[k]['pregunta']}  ·  {CONSULTAS[k]['area']}")
    q = CONSULTAS[clave]
    st.markdown(f'<div class="pregunta">💡 {q["explicacion"]}</div>', unsafe_allow_html=True)
    mostrar_consulta(clave, pedir_filtros(q["filtros"], clave))


# ------------------------------------------------- Análisis descriptivo -----------------------------------------------
def pagina_analisis():
    encabezado("Análisis descriptivo", "Comportamientos, frecuencias y cantidades de las reservas.")
    c1, c2 = st.columns([2, 1])
    r = c1.date_input("Periodo", value=(inicio_datos(), hoy()), format="DD/MM/YYYY", key="an_rango")
    desde, hasta = (r[0], r[-1]) if isinstance(r, tuple) and r else (inicio_datos(), hoy())
    sede = c2.selectbox("Sede", ["TODAS"] + sedes(), format_func=etiqueta, key="an_sede")
    p = parametros_base(desde, hasta, sede)

    k = consultar(f"""
        SELECT COUNT(*) AS reservas,
               ROUND(100 * SUM(CASE WHEN r.estado_reserva = 'PAGADA'    THEN 1 ELSE 0 END) / NULLIF(COUNT(*), 0), 1) AS pagadas_pct,
               ROUND(100 * SUM(CASE WHEN r.estado_reserva = 'VENCIDA'   THEN 1 ELSE 0 END) / NULLIF(COUNT(*), 0), 1) AS vencidas_pct,
               ROUND(100 * SUM(CASE WHEN r.estado_reserva = 'CANCELADA' THEN 1 ELSE 0 END) / NULLIF(COUNT(*), 0), 1) AS canceladas_pct,
               ROUND(AVG(CASE WHEN r.estado_reserva = 'PAGADA' THEN r.valor_total END)) AS ticket,
               ROUND(AVG(r.calificacion), 2) AS calificacion,
               COUNT(DISTINCT r.id_usuario) AS usuarios
        {JOIN_TC}
        WHERE {F_RANGO} AND {F_SEDE}""", p).iloc[0]
    if not int(k.reservas):
        st.info("No hay reservas en el periodo seleccionado.")
        return
    c = st.columns(6)
    c[0].metric("Reservas", int(k.reservas))
    c[1].metric("Usuarios", int(k.usuarios))
    c[2].metric("% pagadas", f"{float(k.pagadas_pct or 0):.1f}%")
    c[3].metric("% vencidas", f"{float(k.vencidas_pct or 0):.1f}%")
    c[4].metric("Ticket promedio", pesos(k.ticket))
    c[5].metric("Calificación", "—" if pd.isna(k.calificacion) else f"{float(k.calificacion):.2f} ★")

    t1, t2, t3, t4 = st.tabs(["Demanda", "Horarios", "Ingresos", "Clientes y calidad"])
    with t1:
        a, b = st.columns(2)
        with a:
            st.subheader("Horas por deporte")
            grafica(consultar(CONSULTAS["demanda_deporte"]["sql"], p), CONSULTAS["demanda_deporte"]["grafica"])
        with b:
            st.subheader("Estados de las reservas")
            grafica(consultar(CONSULTAS["estados"]["sql"], p), CONSULTAS["estados"]["grafica"])
        st.subheader("Ocupación por cancha")
        grafica(consultar(CONSULTAS["ocupacion"]["sql"], p), CONSULTAS["ocupacion"]["grafica"])
    with t2:
        st.subheader("Mapa de calor: horas pico")
        grafica(consultar(CONSULTAS["horas_pico"]["sql"], p), CONSULTAS["horas_pico"]["grafica"])
        a, b = st.columns(2)
        with a:
            st.subheader("Tipo de día")
            grafica(consultar(CONSULTAS["tipo_dia"]["sql"], p), CONSULTAS["tipo_dia"]["grafica"])
        with b:
            st.subheader("Tendencia semanal")
            grafica(consultar(CONSULTAS["tendencia"]["sql"], p), CONSULTAS["tendencia"]["grafica"])
    with t3:
        st.subheader("Recaudo por mes y sede")
        grafica(consultar(CONSULTAS["recaudo_mes"]["sql"], p), CONSULTAS["recaudo_mes"]["grafica"])
        a, b = st.columns(2)
        with a:
            st.subheader("Medios de pago")
            grafica(consultar(CONSULTAS["medios_pago"]["sql"], p), CONSULTAS["medios_pago"]["grafica"])
        with b:
            st.subheader("Dinero no cobrado")
            grafica(consultar(CONSULTAS["perdidas"]["sql"], p), CONSULTAS["perdidas"]["grafica"])
    with t4:
        a, b = st.columns(2)
        with a:
            st.subheader("Gasto por tipo de usuario")
            grafica(consultar(CONSULTAS["tipo_usuario"]["sql"], p), CONSULTAS["tipo_usuario"]["grafica"])
        with b:
            st.subheader("Calificación por cancha")
            grafica(consultar(CONSULTAS["calificacion_cancha"]["sql"], p), CONSULTAS["calificacion_cancha"]["grafica"])


# ------------------------------------------------- Segmentación -------------------------------------------------------
# Indicadores por usuario calculados en Oracle: Python recibe una fila por usuario, nunca las reservas
SQL_KPI_USUARIOS = """
    SELECT u.id_usuario, u.nombre || ' ' || u.apellido AS usuario, u.tipo_usuario AS tipo, u.estado_usuario AS estado,
           u.correo, u.telefono,
           COUNT(r.id_reserva) AS reservas,
           SUM(CASE WHEN r.estado_reserva = 'PAGADA'    THEN 1 ELSE 0 END) AS pagadas,
           SUM(CASE WHEN r.estado_reserva = 'VENCIDA'   THEN 1 ELSE 0 END) AS vencidas,
           SUM(CASE WHEN r.estado_reserva = 'CANCELADA' THEN 1 ELSE 0 END) AS canceladas,
           NVL(SUM(CASE WHEN r.estado_reserva = 'PAGADA' THEN r.valor_total END), 0) AS gasto,
           ROUND(AVG(r.calificacion), 2) AS calificacion,
           ROUND(:hoy - NVL(MAX(CASE WHEN t.fecha <= :hoy THEN t.fecha END), u.fecha_registro)) AS dias_sin_jugar,
           ROUND(:hoy - u.fecha_registro) AS antiguedad_dias
    FROM dim_usuario u
    LEFT JOIN fact_reserva r ON r.id_usuario = u.id_usuario
    LEFT JOIN dim_tiempo  t ON t.id_tiempo  = r.id_tiempo
    GROUP BY u.id_usuario, u.nombre, u.apellido, u.tipo_usuario, u.estado_usuario, u.correo, u.telefono, u.fecha_registro
"""
VARIABLES = {"dias_sin_jugar": "Días sin jugar", "pagadas": "Reservas pagadas", "gasto": "Gasto pagado",
             "tasa_mora": "% vencidas", "tasa_cancelacion": "% canceladas"}
PROMOCIONES = {
    "VIP frecuentes": "Programa de fidelidad: prioridad en horas pico y una hora gratis cada 10 reservas.",
    "Ocasionales": "Segunda hora con 20% de descuento para aumentar la frecuencia.",
    "Nuevos / bajo consumo": "Paquete de bienvenida: 3 reservas en horario valle a precio especial.",
    "Dormidos": "Campaña de reactivación: 25% en la próxima reserva si juegan este mes.",
    "En mora": "Sin promociones: exigir pago anticipado hasta regularizar las reservas vencidas.",
}


def nombrar_grupos(perfil):
    """Asigna un nombre de negocio a cada grupo según su centro (promedios)."""
    nombres, libres = {}, list(perfil.index)

    def tomar(columna, nombre, condicion=lambda v: True):
        if not libres:
            return
        g = perfil.loc[libres, columna].idxmax()
        if condicion(perfil.loc[g, columna]):
            nombres[g] = nombre
            libres.remove(g)

    tomar("tasa_mora", "En mora", lambda v: v >= 0.2)
    tomar("gasto", "VIP frecuentes")
    tomar("dias_sin_jugar", "Dormidos", lambda v: v >= 40)
    for g in sorted(libres, key=lambda g: -perfil.loc[g, "gasto"]):
        usados = set(nombres.values())
        nombres[g] = next((n for n in ("Ocasionales", "Nuevos / bajo consumo") if n not in usados), f"Grupo {g + 1}")
    return nombres


def pagina_segmentos():
    encabezado("Segmentación de usuarios", "Frecuencia, mora y consumo para dirigir promociones.")
    kpi = consultar(SQL_KPI_USUARIOS, {"hoy": hoy()})
    t_km, t_reglas = st.tabs(["Modelo K-means", "Reglas con SQL (WHERE / CASE)"])

    with t_km:
        st.caption("Oracle calcula los indicadores de cada usuario; K-means agrupa a los usuarios con comportamiento parecido.")
        solo_activos = st.toggle("Solo usuarios activos", value=True)
        base = kpi[(kpi.reservas > 0) & ((kpi.estado == "ACTIVO") | (not solo_activos))].copy()
        if len(base) < 10:
            st.info("Se necesitan al menos 10 usuarios con reservas para segmentar.")
            return
        base["tasa_mora"] = base.vencidas / base.reservas
        base["tasa_cancelacion"] = base.canceladas / base.reservas
        X = StandardScaler().fit_transform(base[list(VARIABLES)].astype(float))   # misma escala para todas las variables

        siluetas = {k: silhouette_score(X, KMeans(k, n_init=10, random_state=42).fit_predict(X)) for k in range(2, 7)}
        mejor = max(siluetas, key=siluetas.get)
        c1, c2 = st.columns([1, 2])
        with c1:
            k = st.slider("Número de segmentos (k)", 2, 6, mejor)
            st.caption(f"Sugerido por el coeficiente de silueta: **k = {mejor}**. Más alto = grupos mejor separados.")
        with c2:
            grafica(pd.DataFrame({"k": [str(i) for i in siluetas], "silueta": list(siluetas.values())}),
                    dict(tipo="linea", x="k", y="silueta", alto=180))

        base["grupo"] = KMeans(k, n_init=10, random_state=42).fit_predict(X)
        perfil = base.groupby("grupo")[list(VARIABLES) + ["calificacion"]].mean()
        perfil["usuarios"] = base.groupby("grupo").size()
        nombres = nombrar_grupos(perfil)
        base["segmento"] = base.grupo.map(nombres)

        resumen = perfil.rename(index=nombres).reset_index().rename(columns={"grupo": "segmento"})
        resumen["tasa_mora"] = (100 * resumen.tasa_mora).round(1)
        resumen["tasa_cancelacion"] = (100 * resumen.tasa_cancelacion).round(1)
        resumen = resumen.round(1).rename(columns={"tasa_mora": "vencidas_pct", "tasa_cancelacion": "canceladas_pct"})
        resumen["promocion_sugerida"] = resumen.segmento.map(PROMOCIONES).fillna("—")
        st.subheader("Perfil de cada segmento")
        tabla(resumen[["segmento", "usuarios", "pagadas", "gasto", "dias_sin_jugar", "vencidas_pct",
                       "canceladas_pct", "calificacion", "promocion_sugerida"]])

        st.subheader("Mapa de segmentos")
        st.altair_chart(alt.Chart(base).mark_circle(opacity=.85, stroke="white", strokeWidth=1).encode(
            x=alt.X("dias_sin_jugar:Q", title="Días sin jugar"), y=alt.Y("gasto:Q", title="Gasto pagado"),
            size=alt.Size("pagadas:Q", title="Reservas pagadas", scale=alt.Scale(range=[40, 600])),
            color=alt.Color("segmento:N", scale=alt.Scale(range=PALETA), title="Segmento"),
            tooltip=["usuario", "segmento", "pagadas", alt.Tooltip("gasto", format=",.0f"), "dias_sin_jugar", "vencidas"],
        ).properties(height=380).configure_view(stroke=None), width="stretch")

        seg = st.selectbox("Ver usuarios del segmento", sorted(base.segmento.unique()))
        st.info(f"Promoción sugerida: {PROMOCIONES.get(seg, '—')}")
        detalle = base[base.segmento == seg][["usuario", "tipo", "correo", "telefono", "reservas", "pagadas",
                                              "vencidas", "gasto", "dias_sin_jugar", "calificacion"]]
        tabla(detalle.sort_values("gasto", ascending=False))
        boton_excel(base.drop(columns=["grupo"]), "segmentos_kmeans", "xls_kmeans")

    with t_reglas:
        st.caption("Segmentos definidos con reglas fijas en SQL. Los umbrales se ajustan con los controles.")
        c1, c2, c3 = st.columns(3)
        frec = c1.slider("Pagadas mínimas para FRECUENTE", 1, 20, 8)
        gasto = c2.slider("Gasto mínimo para FRECUENTE", 0, 4_000_000, 1_500_000, 100_000, format="$%d")
        dias = c3.slider("Días sin jugar para DORMIDO", 15, 120, 45)
        reglas = consultar(f"""
            WITH m AS ({SQL_KPI_USUARIOS})
            SELECT usuario, tipo, reservas, pagadas, vencidas, gasto, dias_sin_jugar,
                   CASE WHEN vencidas >= 2 OR vencidas > 0.3 * reservas THEN 'EN MORA'
                        WHEN pagadas >= :frec AND gasto >= :gasto      THEN 'FRECUENTE'
                        WHEN reservas > 0 AND dias_sin_jugar >= :dias  THEN 'DORMIDO'
                        WHEN pagadas >= 1                              THEN 'OCASIONAL'
                        ELSE 'SIN CONSUMO' END AS segmento
            FROM m
            WHERE estado = 'ACTIVO'
            ORDER BY gasto DESC""", {"hoy": hoy(), "frec": frec, "gasto": gasto, "dias": dias})
        conteo = reglas.groupby("segmento").agg(usuarios=("usuario", "count"), gasto_promedio=("gasto", "mean")).reset_index()
        a, b = st.columns([1, 2])
        with a:
            tabla(conteo.round(0))
        with b:
            grafica(conteo, dict(x="segmento", y="usuarios", alto=240))
        tabla(reglas)


# ------------------------------------------------- SQL libre (solo lectura) -------------------------------------------
PROHIBIDO = re.compile(r"\b(INSERT|UPDATE|DELETE|MERGE|DROP|ALTER|CREATE|TRUNCATE|GRANT|REVOKE|BEGIN|DECLARE|EXEC|EXECUTE|CALL|LOCK)\b", re.I)


def pagina_sql():
    encabezado("Consultas SQL", "Modo experto: solo lectura (SELECT), con tope de filas.")
    with st.expander("Tablas disponibles"):
        st.markdown("""
- **dim_usuario**: id_usuario, documento, nombre, apellido, correo, telefono, tipo_usuario, fecha_registro, estado_usuario
- **dim_cancha**: id_cancha, nombre, deporte, sede, superficie, capacidad, precio_hora, estado_cancha
- **dim_tiempo**: id_tiempo, fecha, hora_inicio, anio, mes, dia_semana, franja, es_festivo
- **fact_reserva**: id_reserva, id_usuario, id_cancha, id_tiempo, duracion_horas, valor_total, metodo_pago, estado_reserva, calificacion, fecha_creacion
""")
    sql = st.text_area("Consulta (sin punto y coma)", height=160,
                       value="SELECT sede, deporte, COUNT(*) AS canchas\nFROM dim_cancha\nGROUP BY sede, deporte\nORDER BY sede")
    if st.button("Ejecutar", type="primary") and sql.strip():
        if not re.match(r"^\s*(SELECT|WITH)\b", sql, re.I) or ";" in sql or PROHIBIDO.search(sql):
            st.error("Solo se permiten consultas de lectura (SELECT o WITH), sin punto y coma.")
            return
        try:
            df = consultar(sql)
            st.caption(f"{len(df)} filas (máximo {MAX_FILAS}).")
            st.dataframe(df, hide_index=True, width="stretch")
            boton_excel(df, "consulta_sql", "xls_sql")
        except oracledb.Error as e:
            st.error(f"Error de Oracle: {e}")


# =============================================== 5. MENÚ Y ARRANQUE ===================================================
# Menú superior por secciones. En celular se convierte en un menú desplegable que se cierra al elegir.
PAGINAS = {
    "Inicio": st.Page(pagina_inicio, title="Inicio", icon="🏠", default=True),
    "Solicitar cancha": st.Page(pagina_solicitar, title="Solicitar cancha", icon="🎾"),
    "Registrar usuario": st.Page(pagina_registro, title="Registrar usuario", icon="👤"),
    "Reservas por estado": st.Page(pagina_reservas, title="Reservas por estado", icon="📋"),
    "Canchas": st.Page(pagina_canchas, title="Canchas", icon="🛠️"),
    "Consultas guiadas": st.Page(pagina_consultas, title="Consultas guiadas", icon="💡"),
    "Análisis": st.Page(pagina_analisis, title="Análisis", icon="📊"),
    "Segmentación": st.Page(pagina_segmentos, title="Segmentación", icon="🎯"),
    "Consultas SQL": st.Page(pagina_sql, title="Consultas SQL", icon="⌨️"),
}
SECCIONES = {
    "Operación": ["Inicio", "Solicitar cancha", "Registrar usuario", "Reservas por estado", "Canchas"],
    "Gerencia": ["Consultas guiadas", "Análisis", "Segmentación"],
    "Experto": ["Consultas SQL"],
}
navegacion = st.navigation({sec: [PAGINAS[p] for p in paginas] for sec, paginas in SECCIONES.items()}, position="top")

with st.sidebar:
    logo = Path(__file__).parent / "assets" / "logo.png"
    if logo.exists():
        st.image(str(logo), width=180)
    else:
        st.markdown('<div class="marca">Action<small>SPORT CLUB</small></div>', unsafe_allow_html=True)
    st.divider()
    st.caption(f"Hora Bogotá: {ahora():%d/%m/%Y %H:%M}")
    if st.button("↻ Actualizar datos", width="stretch"):
        st.cache_data.clear()
        st.rerun()

if "flash" in st.session_state:
    tipo, msg = st.session_state.pop("flash")
    (st.success if tipo == "ok" else st.error)(msg)

# Prueba de conexión con una consulta mínima. Si falla, se explica y se detiene la app.
try:
    consultar("SELECT COUNT(*) AS n FROM dim_cancha")
except Exception as e:
    st.error(f"No se pudo conectar a la base de datos: {e}")
    st.info("Revisa .streamlit/secrets.toml y que se hayan ejecutado 00_USUARIOS, 01_DDL y 02_DML.")
    st.stop()

try:
    navegacion.run()
except oracledb.Error as e:
    st.error(f"Error de base de datos: {e}")