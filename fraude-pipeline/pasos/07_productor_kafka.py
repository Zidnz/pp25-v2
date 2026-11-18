"""Paso 7 · Productor: publica en Kafka el último mes (prueba) como eventos, SIN la etiqueta de fraude.

Corrige el productor original, que inventaba campos con otro formato. Aquí el evento tiene exactamente
las columnas de la fuente (config.COLUMNAS_FUENTE menos isFraud), en orden cronológico.
"""
import os
import _comun
from _comun import Cronometro
from pyspark.sql import functions as F
from spark_session import crear_spark
import config as C

LIMITE = int(os.environ.get("EVENTOS_MAX", "0"))   # 0 = todo el mes de prueba
spark = crear_spark("07_productor")
with Cronometro(f"Publicar eventos en el tópico '{C.KAFKA_TOPICO}'"):
    columnas = [c for c in C.COLUMNAS_FUENTE if c != "isFraud"]
    ev = (spark.read.format("delta").load(C.SILVER).filter("particion = 'prueba'")
          .select(*columnas).orderBy("TransactionDT", "TransactionID"))
    if LIMITE:
        ev = ev.limit(LIMITE)
    n = ev.count()
    (ev.coalesce(1)    # una sola partición conserva el orden cronológico en el tópico
       .select(F.col("TransactionID").cast("string").alias("key"), F.to_json(F.struct(*columnas)).alias("value"))
       .write.format("kafka").option("kafka.bootstrap.servers", C.KAFKA_SERVIDOR).option("topic", C.KAFKA_TOPICO).save())
    print(f"  eventos publicados: {n:,}")
    muestra = ev.limit(1).toPandas().iloc[0].dropna().to_dict()
    print("  ejemplo de evento:", {k: muestra[k] for k in list(muestra)[:8]}, "…")
spark.stop()
