"""Paso 3 · Gold: atributos punto en el tiempo con el módulo único `features.py`."""
import _comun
from _comun import Cronometro
from pyspark.sql import functions as F
from spark_session import crear_spark
from features import marcar_etiqueta_conocida, construir_atributos, VELOCIDAD, ESTOCASTICOS, GRAFO, RIESGO
import config as C

spark = crear_spark("03_gold")
with Cronometro("Construcción de atributos"):
    silver = spark.read.format("delta").load(C.SILVER)
    gold = construir_atributos(marcar_etiqueta_conocida(silver))
    gold.write.format("delta").mode("overwrite").option("overwriteSchema", True).partitionBy("particion").save(C.GOLD)

with Cronometro("Revisión rápida"):
    g = spark.read.format("delta").load(C.GOLD)
    print(f"  filas: {g.count():,} · columnas: {len(g.columns)}")
    nuevos = VELOCIDAD + ESTOCASTICOS + GRAFO + RIESGO
    resumen = (g.filter("particion = 'entreno'").groupBy("isFraud").agg(*[F.avg(c).alias(c) for c in nuevos])
               .orderBy("isFraud").toPandas().set_index("isFraud").T)
    resumen = resumen.rename(columns={0: "normal", 1: "fraude"})
    print(resumen.round(3).to_string())
    resumen.to_csv(C.REPORTE / "03_gold_medias_por_clase.csv")
    # Comprobación de que no hay fuga: ninguna transacción de prueba usa etiquetas posteriores al corte
    fuga = g.filter("particion = 'prueba'").filter(F.col("etiqueta_conocida").isNotNull()).count()
    print(f"  etiquetas de prueba visibles para el modelo: {fuga} (debe ser 0)")
spark.stop()
