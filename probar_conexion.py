"""
Prueba de conexión a Oracle usando las mismas credenciales de la app (.streamlit/secrets.toml).
Ejecutar desde la carpeta del proyecto:  python probar_conexion.py
"""
import tomllib
from pathlib import Path

import oracledb

c = tomllib.loads((Path(__file__).parent / ".streamlit" / "secrets.toml").read_text(encoding="utf-8"))["oracle"]

if c.get("lib_dir"):      # Windows: modo thick con Instant Client
    oracledb.init_oracle_client(lib_dir=c["lib_dir"], config_dir=c["config_dir"])
    extra = {}
else:                     # Mac: modo thin
    extra = dict(config_dir=c["config_dir"], wallet_location=c["config_dir"], wallet_password=c.get("wallet_password"))

with oracledb.connect(user=c["user"], password=c["password"], dsn=c["dsn"], **extra) as con:
    cur = con.cursor()
    if c.get("schema"):
        cur.execute(f"ALTER SESSION SET CURRENT_SCHEMA = {c['schema']}")
    cur.execute("""
        SELECT 'dim_usuario', COUNT(*) FROM dim_usuario
        UNION ALL SELECT 'dim_cancha',   COUNT(*) FROM dim_cancha
        UNION ALL SELECT 'dim_tiempo',   COUNT(*) FROM dim_tiempo
        UNION ALL SELECT 'fact_reserva', COUNT(*) FROM fact_reserva""")
    print(f"Conexión exitosa como {c['user']}.")
    for tabla, filas in cur:
        print(f"  {tabla:<13} {filas:>6} filas")
