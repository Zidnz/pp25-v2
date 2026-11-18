"""Paso 8 · Consumidor: Kafka → Spark Structured Streaming → atributos → modelo → alertas.

En cada micro-lote:
  1. guarda los eventos crudos en Delta (bronze/transacciones_stream)
  2. calcula atributos con el MISMO `features.py` del entrenamiento, sobre historial + eventos ya vistos
  3. puntúa con el modelo de Spark guardado y calibra la probabilidad
  4. decide con la regla de pérdida esperada (paso 6)
  5. escribe todos los puntajes en Delta y las alertas en PostgreSQL (tabla operativa) y Delta
"""
import json, os
import _comun
from _comun import Cronometro
from pyspark.ml import PipelineModel
from pyspark.sql import functions as F
from pyspark.sql.types import StructType, StructField, StringType, DoubleType, LongType
from spark_session import crear_spark
from features import marcar_etiqueta_conocida, construir_atributos
from modelado import preparar_segun_tipo, probabilidad, platt_spark
import config as C

POR_LOTE = int(os.environ.get("EVENTOS_POR_LOTE", "20000"))
spark = crear_spark("08_consumidor", memoria="4g")
silver = spark.read.format("delta").load(C.SILVER)
texto = {f.name for f in silver.schema.fields if isinstance(f.dataType, StringType)}
esquema = StructType([StructField(c, StringType() if c in texto else (LongType() if c in ("TransactionID", "TransactionDT") else DoubleType()))
                      for c in C.COLUMNAS_FUENTE if c != "isFraud"])

# Historial conocido al momento de operar: entrenamiento + validación (con etiquetas)
historial = silver.filter("particion IN ('entreno', 'validacion')").drop("particion")
modelo = PipelineModel.load(str(C.MODELOS / "modelo_final"))
meta = json.load(open(C.MODELOS / "modelo_final.json"))
umbral = json.load(open(C.MODELOS / "decision.json"))["umbral_perdida_esperada_usd"]
PUNTAJES = str(C.LAGO / "gold" / "puntajes_stream")
COLS_ALERTA = ["TransactionID", "TransactionDT", "TransactionAmt", "ProductCD", "card4", "card6", "uid", "prob", "perdida_esperada",
               "n_1h", "n_24h", "sorpresa_poisson", "ratio_monto_cliente", "riesgo_tarjeta", "riesgo_cliente", "riesgo_direccion",
               "grado_dispositivo", "grado_cliente", "uid_emails_distintos", "C1", "C13", "dist1", "D15n", "frec_card1"]

def procesar(lote, id_lote):
    n = lote.count()
    if n == 0:
        return
    lote.write.format("delta").mode("append").save(C.BRONZE_STREAM)
    vistos = spark.read.format("delta").load(C.BRONZE_STREAM).withColumn("isFraud", F.lit(None).cast("int"))
    todo = historial.unionByName(vistos)                       # el modelo no conoce etiquetas de eventos nuevos
    feats = construir_atributos(marcar_etiqueta_conocida(todo))
    feats = feats.join(lote.select("TransactionID"), "TransactionID", "inner")
    preparado = preparar_segun_tipo(feats.withColumn("monto_real", F.col("TransactionAmt")), meta["numericas"], meta["tipo"])
    pred = probabilidad(modelo.transform(preparado.withColumn("es_validacion", F.lit(False))))
    pred = pred.withColumn("TransactionAmt", F.col("monto_real"))   # la decisión se toma en dólares reales
    pred = (pred.withColumn("prob", platt_spark("score", meta["calibracion"]))
                .withColumn("perdida_esperada", F.col("prob") * F.col("TransactionAmt"))
                .withColumn("alerta", F.col("perdida_esperada") >= F.lit(umbral))
                .withColumn("id_lote", F.lit(id_lote)).withColumn("procesado_en", F.current_timestamp())).cache()
    # restaurar valores reales (preparar() rellenó nulos con -999 para el modelo)
    pred.select("TransactionID", "TransactionDT", "TransactionAmt", "score", "prob", "perdida_esperada", "alerta", "id_lote") \
        .write.format("delta").mode("append").save(PUNTAJES)
    alertas = (pred.filter("alerta").select(*COLS_ALERTA)
               .join(feats.select("TransactionID", *[F.col(c).alias(c + "_real") for c in COLS_ALERTA[9:]]), "TransactionID"))
    for c in COLS_ALERTA[9:]:
        alertas = alertas.withColumn(c, F.col(c + "_real")).drop(c + "_real")
    alertas = alertas.withColumn("id_lote", F.lit(id_lote))
    alertas.write.format("delta").mode("append").option("mergeSchema", True).save(C.ALERTAS_DELTA)
    (alertas.select(F.col("TransactionID").alias("transaction_id"), (F.col("TransactionDT") / 86400).alias("dia"),
                    F.col("TransactionAmt").alias("monto"), F.col("ProductCD").alias("producto"), F.col("uid").alias("cliente"),
                    "prob", "perdida_esperada", F.lit("pendiente").alias("estado"), F.col("id_lote").cast("int"))
            .write.jdbc(C.PG_JDBC, "operacion.alertas", mode="append", properties=C.PG_PROPS))
    k = alertas.count()
    print(f"  lote {id_lote}: {n:,} eventos · {k:,} alertas ({k / n:.2%})", flush=True)
    pred.unpersist()

with Cronometro("Tabla operativa en PostgreSQL"):
    con = C.pg_conexion(); cur = con.cursor()
    cur.execute(open(C.RAIZ / "sql" / "01_operacion.sql").read()); con.commit(); con.close()

with Cronometro(f"Streaming desde Kafka ({POR_LOTE:,} eventos por micro-lote)"):
    flujo = (spark.readStream.format("kafka").option("kafka.bootstrap.servers", C.KAFKA_SERVIDOR)
             .option("subscribe", C.KAFKA_TOPICO).option("startingOffsets", "earliest")
             .option("maxOffsetsPerTrigger", POR_LOTE).load()
             .select(F.from_json(F.col("value").cast("string"), esquema).alias("e")).select("e.*"))
    consulta = (flujo.writeStream.foreachBatch(procesar).option("checkpointLocation", C.CHECKPOINT)
                .trigger(availableNow=True).start())
    consulta.awaitTermination()
spark.stop()
