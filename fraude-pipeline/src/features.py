"""
Módulo ÚNICO de atributos.

Corrige el error principal del proyecto original: el notebook de entrenamiento y el consumidor de
Kafka calculaban los atributos con código distinto, así que el modelo veía datos diferentes en
producción. Aquí la misma función se usa en batch (capa gold) y dentro del streaming (foreachBatch).

Todo atributo es "punto en el tiempo": para una transacción en el instante t solo se usa información
anterior a t. Las etiquetas de fraude, además, solo se usan si ya se conocían (30 días de retraso,
el tiempo típico de un contracargo).
"""
import numpy as np
import pandas as pd
from pyspark.sql import DataFrame, Window
from pyspark.sql import functions as F
from pyspark.sql.functions import pandas_udf
from pyspark.sql.types import DoubleType
from scipy.special import gammainc

from config import DIA_CONOCIMIENTO, RETRASO_ETIQUETA_DIAS

SEG_DIA = 86_400
RETRASO_SEG = RETRASO_ETIQUETA_DIAS * SEG_DIA

# Prior Gamma(α, β) para la tasa de transacciones de un cliente: equivale a "haber visto α
# transacciones en β segundos". Con α=1 y β=7 días, un cliente nuevo arranca con ~1 por semana.
ALFA_PRIOR, BETA_PRIOR = 1.0, 7 * SEG_DIA
# Suavizado bayesiano de tasas de fraude por entidad: m observaciones "virtuales" con la tasa global
M_SUAVIZADO, TASA_GLOBAL = 20.0, 0.035


@pandas_udf(DoubleType())
def sorpresa_poisson(k: pd.Series, mu: pd.Series) -> pd.Series:
    """-log10 P(N ≥ k) con N ~ Poisson(mu). P(N ≥ k) = P(k, mu), la gamma incompleta regularizada."""
    k = k.astype(float).values
    mu = np.maximum(mu.astype(float).values, 1e-9)
    p = np.where(k <= 0, 1.0, gammainc(np.maximum(k, 1), mu))
    return pd.Series(-np.log10(np.clip(p, 1e-300, 1.0)))


def marcar_etiqueta_conocida(df: DataFrame, dia_conocimiento: float = DIA_CONOCIMIENTO) -> DataFrame:
    """Solo se conoce la etiqueta de transacciones anteriores al corte. Después del corte es nula."""
    dia = F.col("TransactionDT") / SEG_DIA
    return df.withColumn("etiqueta_conocida",
                         F.when(dia <= dia_conocimiento, F.col("isFraud").cast("double")))


def construir_atributos(df: DataFrame) -> DataFrame:
    """Recibe transacciones con el esquema de la fuente + `etiqueta_conocida` y agrega atributos."""
    t = F.col("TransactionDT")

    # --- 1. Básicos ---
    df = (df
          .withColumn("dia", t / SEG_DIA)
          .withColumn("hora", ((t / 3600) % 24).cast("int"))
          .withColumn("dia_semana", (F.floor(t / SEG_DIA) % 7).cast("int"))
          .withColumn("log_monto", F.log1p("TransactionAmt"))
          .withColumn("centavos", F.round(F.col("TransactionAmt") - F.floor("TransactionAmt"), 3))
          # identificador aproximado de cliente: tarjeta + dirección + día de alta de la tarjeta
          .withColumn("uid", F.concat_ws("_", F.col("card1").cast("int"), F.coalesce(F.col("addr1").cast("int"), F.lit(-1)),
                                         F.coalesce(F.floor(F.col("dia") - F.col("D1")), F.lit(-1))))
          # huella de dispositivo: modelo + navegador + resolución + dos campos de identidad de Vesta.
          # "DeviceInfo" solo es el modelo del teléfono (miles de personas lo comparten); la huella es más fina.
          # Columnas D normalizadas (técnica del 1.er lugar en Kaggle): "días desde X" se convierte en
          # "día en que ocurrió X", que es constante para un mismo cliente y no crece con el tiempo.
          .withColumn("D4n", F.col("dia") - F.col("D4"))
          .withColumn("D10n", F.col("dia") - F.col("D10"))
          .withColumn("D11n", F.col("dia") - F.col("D11"))
          .withColumn("D15n", F.col("dia") - F.col("D15"))
          .withColumn("outsider15", (F.abs(F.col("D1") - F.col("D15")) > 3).cast("int"))
          .withColumn("dispositivo", F.when(F.col("DeviceInfo").isNotNull() & F.col("id_19").isNotNull() & F.col("id_20").isNotNull(),
                                            F.concat_ws("|", "DeviceInfo", "id_31", "id_33",
                                                        F.col("id_19").cast("int"), F.col("id_20").cast("int")))
                                     .otherwise(F.lit("desconocido"))))

    # --- 2. Historial y velocidad del cliente (ventanas hacia atrás) ---
    por_uid_tiempo = Window.partitionBy("uid").orderBy(t)
    por_uid_filas = Window.partitionBy("uid").orderBy(t, "TransactionID")
    previas = por_uid_filas.rowsBetween(Window.unboundedPreceding, -1)
    df = (df
          .withColumn("n_1h", F.count("*").over(por_uid_tiempo.rangeBetween(-3600, -1)))
          .withColumn("n_24h", F.count("*").over(por_uid_tiempo.rangeBetween(-SEG_DIA, -1)))
          .withColumn("n_7d", F.count("*").over(por_uid_tiempo.rangeBetween(-7 * SEG_DIA, -1)))
          .withColumn("monto_24h", F.coalesce(F.sum("TransactionAmt").over(por_uid_tiempo.rangeBetween(-SEG_DIA, -1)), F.lit(0.0)))
          .withColumn("n_previas", F.count("*").over(previas))
          .withColumn("monto_medio_previo", F.avg("TransactionAmt").over(previas))
          .withColumn("primera_vez", F.min(t).over(previas))
          .withColumn("seg_desde_anterior", t - F.lag(t).over(por_uid_filas))
          .withColumn("ratio_monto_cliente", F.col("TransactionAmt") / F.col("monto_medio_previo")))

    # --- 2b. Agregados del cliente "punto en el tiempo" ---
    # En Kaggle, el 1.er lugar promediaba cada columna sobre TODAS las transacciones del cliente, incluidas
    # las futuras. Eso no se puede hacer en producción. Aquí el promedio usa solo transacciones anteriores.
    agregar = ["TransactionAmt", "D4n", "D10n", "D15n"] + [f"C{i}" for i in (1, 2, 4, 5, 6, 8, 9, 10, 11, 12, 13, 14)]
    for c in agregar:
        df = df.withColumn(f"uid_{c}_media", F.avg(c).over(previas))
    for c in ["TransactionAmt", "D15n", "C14"]:
        df = df.withColumn(f"uid_{c}_desv", F.stddev(c).over(previas))
    df = df.withColumn("dif_monto_vs_cliente", F.col("TransactionAmt") - F.col("uid_TransactionAmt_media"))
    # Distintos correos usados por el cliente hasta ahora (aristas nuevas acumuladas, igual que en el grafo)
    par_email = Window.partitionBy("uid", "P_emaildomain").orderBy(t, "TransactionID")
    df = (df.withColumn("_email_nuevo", (F.row_number().over(par_email) == 1).cast("int"))
            .withColumn("uid_emails_distintos", F.sum("_email_nuevo").over(por_uid_filas.rowsBetween(Window.unboundedPreceding, 0)))
            .drop("_email_nuevo"))
    # Frecuencia acumulada (codificación por frecuencia, sin mirar el futuro)
    for c in ["card1", "addr1", "card2", "P_emaildomain"]:
        df = df.withColumn(f"frec_{c}", F.count("*").over(Window.partitionBy(c).orderBy(t, "TransactionID")
                                                         .rowsBetween(Window.unboundedPreceding, -1)))

    # --- 3. Modelo estocástico: proceso de Poisson con prior Gamma por cliente ---
    # Tasa posterior λ = (n_previas + α) / (antigüedad + β). Esperado en 24 h: μ = λ·86400.
    # Observado: k = transacciones en las últimas 24 h, contando la actual.
    antig = F.coalesce(t - F.col("primera_vez"), F.lit(0))
    df = (df
          .withColumn("tasa_poisson_dia", (F.col("n_previas") + ALFA_PRIOR) / (antig + BETA_PRIOR) * SEG_DIA)
          .withColumn("sorpresa_poisson", sorpresa_poisson(F.col("n_24h") + 1, F.col("tasa_poisson_dia"))))

    # --- 4. Matemáticas discretas: grafo bipartito cliente–dispositivo ---
    # Grado del dispositivo = clientes distintos que lo han usado hasta ahora (aristas nuevas acumuladas).
    par = Window.partitionBy("dispositivo", "uid").orderBy(t, "TransactionID")
    acumulado = lambda *k: Window.partitionBy(*k).orderBy(t, "TransactionID").rowsBetween(Window.unboundedPreceding, 0)
    df = (df
          .withColumn("_arista_nueva", (F.row_number().over(par) == 1).cast("int"))
          .withColumn("grado_dispositivo", F.when(F.col("dispositivo") != "desconocido",
                                                  F.sum("_arista_nueva").over(acumulado("dispositivo"))))
          .withColumn("grado_cliente", F.sum(F.when(F.col("dispositivo") != "desconocido", F.col("_arista_nueva")).otherwise(0))
                                        .over(acumulado("uid")))
          .drop("_arista_nueva"))

    # --- 5. Riesgo aprendido de etiquetas pasadas, con retraso de 30 días y suavizado bayesiano ---
    for entidad, nombre in [("uid", "cliente"), ("card1", "tarjeta"), ("dispositivo", "dispositivo"),
                            ("P_emaildomain", "email"), ("addr1", "direccion")]:
        w = Window.partitionBy(entidad).orderBy(t).rangeBetween(Window.unboundedPreceding, -RETRASO_SEG)
        fraudes = F.coalesce(F.sum("etiqueta_conocida").over(w), F.lit(0.0))
        vistas = F.coalesce(F.count("etiqueta_conocida").over(w), F.lit(0)).cast("double")
        df = df.withColumn(f"riesgo_{nombre}", (fraudes + M_SUAVIZADO * TASA_GLOBAL) / (vistas + M_SUAVIZADO))
        df = df.withColumn(f"historial_{nombre}", vistas)

    return df.drop("primera_vez")


# Grupos de atributos (para el modelo y para el análisis de ablación)
BASE_NUM = (["TransactionAmt", "log_monto", "centavos", "hora", "dia_semana", "card1", "card2", "card3", "card5",
             "addr1", "addr2", "dist1"] + [f"C{i}" for i in range(1, 15)]
            + ["D1", "D2", "D3", "D4", "D10", "D11", "D15"])
V_NUM = ["V258", "V257", "V294", "V243", "V189", "V45", "V87", "V83", "V70", "V91",
         "V283", "V317", "V310", "V307", "V127", "V62", "V44", "V201", "V187", "V156"]
D_NORM = ["D4n", "D10n", "D11n", "D15n", "outsider15"]
AGREGADOS_CLIENTE = ([f"uid_{c}_media" for c in ["TransactionAmt", "D4n", "D10n", "D15n"] + [f"C{i}" for i in (1, 2, 4, 5, 6, 8, 9, 10, 11, 12, 13, 14)]]
                     + [f"uid_{c}_desv" for c in ["TransactionAmt", "D15n", "C14"]]
                     + ["dif_monto_vs_cliente", "uid_emails_distintos", "frec_card1", "frec_addr1", "frec_card2", "frec_P_emaildomain"])
CATEGORICAS = ["ProductCD", "card4", "card6", "P_emaildomain", "R_emaildomain", "M4", "M5", "M6", "DeviceType", "id_31"]
VELOCIDAD = ["n_1h", "n_24h", "n_7d", "monto_24h", "n_previas", "monto_medio_previo", "seg_desde_anterior", "ratio_monto_cliente"]
ESTOCASTICOS = ["tasa_poisson_dia", "sorpresa_poisson"]
GRAFO = ["grado_dispositivo", "grado_cliente"]
RIESGO = [f"{p}_{n}" for n in ["cliente", "tarjeta", "dispositivo", "email", "direccion"] for p in ("riesgo", "historial")]
