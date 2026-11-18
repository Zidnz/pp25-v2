"""Paso 2 · Silver: unir transacción + identidad, tipar, quitar duplicados y validar calidad."""
import json
import _comun
from _comun import Cronometro
from pyspark.sql import functions as F
from spark_session import crear_spark
import config as C

spark = crear_spark("02_silver")
with Cronometro("Unión, tipos y duplicados"):
    tx = spark.read.format("delta").load(C.BRONZE)
    ident = spark.read.format("delta").load(C.BRONZE_ID).drop("_archivo_origen", "_ingestado_en")
    df = tx.join(ident, "TransactionID", "left")
    df = df.select([c for c in C.COLUMNAS_FUENTE if c in df.columns])
    texto = {"ProductCD", "card4", "card6", "P_emaildomain", "R_emaildomain", "M4", "M5", "M6", "DeviceType", "DeviceInfo", "id_31", "id_33"}
    for c in df.columns:
        if c in ("TransactionID", "TransactionDT", "isFraud"):
            df = df.withColumn(c, F.col(c).cast("long" if c != "isFraud" else "int"))
        elif c in texto:
            df = df.withColumn(c, F.col(c).cast("string"))
        else:
            df = df.withColumn(c, F.col(c).cast("double"))
    antes = df.count()
    df = df.dropDuplicates(["TransactionID"])
    dia = F.col("TransactionDT") / 86400
    df = df.withColumn("particion", F.when(dia <= C.DIA_FIN_ENTRENO, "entreno")
                                     .when(dia <= C.DIA_FIN_VALID, "validacion").otherwise("prueba"))

with Cronometro("Pruebas de calidad"):
    pruebas = {
        "id_unico": df.count() == antes,
        "id_no_nulo": df.filter(F.col("TransactionID").isNull()).count() == 0,
        "monto_positivo": df.filter(F.col("TransactionAmt") <= 0).count() == 0,
        "etiqueta_binaria": df.filter(~F.col("isFraud").isin(0, 1)).count() == 0,
        "tiempo_no_nulo": df.filter(F.col("TransactionDT").isNull()).count() == 0,
        "producto_valido": df.filter(~F.col("ProductCD").isin("W", "C", "R", "H", "S")).count() == 0,
    }
    nulos = df.select([F.avg(F.col(c).isNull().cast("int")).alias(c) for c in df.columns]).first().asDict()
    filas = [(k, bool(v), None) for k, v in pruebas.items()] + [(f"nulos_{k}", True, float(v)) for k, v in nulos.items()]
    spark.createDataFrame(filas, "prueba string, aprobada boolean, valor double") \
         .withColumn("ejecutada_en", F.current_timestamp()) \
         .write.format("delta").mode("append").save(C.CALIDAD)
    for k, v in pruebas.items():
        print(f"  {'OK ' if v else 'FALLA'} {k}")
    if not all(pruebas.values()):
        raise SystemExit("La capa silver no pasó las pruebas de calidad")

with Cronometro("Escritura silver"):
    df.write.format("delta").mode("overwrite").option("overwriteSchema", True).partitionBy("particion").save(C.SILVER)
    resumen = (spark.read.format("delta").load(C.SILVER).groupBy("particion")
               .agg(F.count("*").alias("filas"), F.avg("isFraud").alias("tasa_fraude"),
                    F.min(F.col("TransactionDT") / 86400).alias("dia_min"), F.max(F.col("TransactionDT") / 86400).alias("dia_max"))
               .orderBy("dia_min").toPandas())
    print(resumen.to_string(index=False))
    resumen.to_json(C.REPORTE / "02_silver_resumen.json", orient="records")
spark.stop()
