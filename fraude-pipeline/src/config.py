"""Configuración central del proyecto. Todo lo que cambia entre máquinas sale de variables de entorno."""
import os
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]

# --- Datos crudos (CSV de Kaggle) y lakehouse en Delta ---
DATOS_CRUDOS = Path(os.environ.get("RUTA_IEEE", RAIZ / "datos" / "ieee-cis"))
LAGO = Path(os.environ.get("RUTA_LAGO", RAIZ / "lakehouse"))
BRONZE = str(LAGO / "bronze" / "transacciones")
BRONZE_ID = str(LAGO / "bronze" / "identidad")
SILVER = str(LAGO / "silver" / "transacciones")
CALIDAD = str(LAGO / "silver" / "calidad")
GOLD = str(LAGO / "gold" / "atributos")
PREDICCIONES = str(LAGO / "gold" / "predicciones")
BRONZE_STREAM = str(LAGO / "bronze" / "transacciones_stream")
ALERTAS_DELTA = str(LAGO / "gold" / "alertas")
CHECKPOINT = str(LAGO / "_checkpoints" / "consumidor")
MODELOS = RAIZ / "modelos"
REPORTE = RAIZ / "reporte"
FIGURAS = REPORTE / "figuras"

# --- Particiones en el tiempo (días desde la fecha de referencia de Vesta) ---
DIA_FIN_ENTRENO = 120      # entrenamiento: días 1-120
DIA_FIN_VALID = 150        # validación: 121-150 ; prueba (y streaming): 151-183
DIA_CONOCIMIENTO = DIA_FIN_VALID   # las etiquetas posteriores no se conocen al momento de operar
RETRASO_ETIQUETA_DIAS = 30         # un contracargo tarda ~30 días en confirmar el fraude

# --- Negocio ---
PRESUPUESTO_REVISION = 0.0335      # el equipo revisa 3.35% de las transacciones
COSTO_REVISION_USD = 3.0           # supuesto: ~10 min de analista
MINUTOS_POR_REVISION = 10          # para el modelo de colas

# --- Infraestructura ---
KAFKA_SERVIDOR = os.environ.get("KAFKA_SERVIDOR", "localhost:9092")
KAFKA_TOPICO = os.environ.get("KAFKA_TOPICO", "transacciones")
PG_HOST = os.environ.get("PG_HOST", "localhost")
PG_PUERTO = os.environ.get("PG_PUERTO", "5432")
PG_BD = os.environ.get("PG_BD", "fraude")
PG_USUARIO = os.environ.get("PG_USUARIO", "postgres")
PG_CLAVE = os.environ.get("PG_CLAVE", "fraude")
PG_JDBC = f"jdbc:postgresql://{PG_HOST}:{PG_PUERTO}/{PG_BD}"
PG_PROPS = {"user": PG_USUARIO, "password": PG_CLAVE, "driver": "org.postgresql.Driver"}

def pg_conexion():
    import psycopg2
    return psycopg2.connect(host=PG_HOST, port=PG_PUERTO, dbname=PG_BD, user=PG_USUARIO, password=PG_CLAVE)

# --- Columnas que entran desde la fuente (se usan igual en batch y en streaming) ---
V_ELEGIDAS = ["V258", "V257", "V294", "V243", "V189", "V45", "V87", "V83", "V70", "V91",
              "V283", "V317", "V310", "V307", "V127", "V62", "V44", "V201", "V187", "V156"]
COLUMNAS_FUENTE = (["TransactionID", "isFraud", "TransactionDT", "TransactionAmt", "ProductCD",
                    "card1", "card2", "card3", "card4", "card5", "card6", "addr1", "addr2", "dist1",
                    "P_emaildomain", "R_emaildomain"]
                   + [f"C{i}" for i in range(1, 15)]
                   + ["D1", "D2", "D3", "D4", "D10", "D11", "D15", "M4", "M5", "M6", "DeviceType", "DeviceInfo", "id_31", "id_33", "id_19", "id_20"]
                   + V_ELEGIDAS)

for carpeta in (MODELOS, FIGURAS):
    carpeta.mkdir(parents=True, exist_ok=True)
