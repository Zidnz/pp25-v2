"""Paso 9 · Prueba de paridad: el streaming debe dar EXACTAMENTE lo mismo que el batch.

Es la prueba que le faltaba al proyecto original: allá el consumidor de Kafka calculaba los atributos
con otro código y el modelo recibía datos distintos a los de entrenamiento.
"""
import json
import _comun
from _comun import Cronometro
from pyspark.sql import functions as F
from spark_session import crear_spark
import config as C

spark = crear_spark("09_paridad")
with Cronometro("Comparar puntajes batch vs streaming"):
    batch = spark.read.format("delta").load(C.PREDICCIONES).filter("particion = 'prueba'").select("TransactionID", F.col("prob").alias("p_batch"), "isFraud", "TransactionAmt")
    stream = spark.read.format("delta").load(str(C.LAGO / "gold" / "puntajes_stream")).select("TransactionID", F.col("prob").alias("p_stream"), "alerta")
    j = batch.join(stream, "TransactionID", "inner")
    umbral = json.load(open(C.MODELOS / "decision.json"))["umbral_perdida_esperada_usd"]
    r = (j.withColumn("dif", F.abs(F.col("p_batch") - F.col("p_stream")))
          .withColumn("alerta_batch", F.col("p_batch") * F.col("TransactionAmt") >= umbral)
          .agg(F.count("*").alias("comparadas"), F.max("dif").alias("dif_max"), F.avg("dif").alias("dif_media"),
               F.avg((F.col("alerta_batch") == F.col("alerta")).cast("int")).alias("decisiones_iguales"),
               F.sum(F.col("alerta").cast("int")).alias("alertas"),
               F.sum((F.col("alerta") & (F.col("isFraud") == 1)).cast("int")).alias("alertas_fraude"),
               F.sum("isFraud").alias("fraudes")).first().asDict())
    r["eventos_batch"] = batch.count(); r["eventos_stream"] = stream.count()
    r["precision_alertas"] = r["alertas_fraude"] / max(r["alertas"], 1); r["recall_alertas"] = r["alertas_fraude"] / r["fraudes"]
    for k, v in r.items():
        print(f"  {k}: {v:.3g}" if isinstance(v, float) else f"  {k}: {v:,}")
    json.dump(r, open(C.REPORTE / "09_paridad.json", "w"), indent=2)
    if r["dif_max"] > 1e-6:
        raise SystemExit("❌ El streaming no reproduce al batch")
    print("  ✅ Streaming y batch coinciden")
spark.stop()
