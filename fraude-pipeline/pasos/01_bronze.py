"""Paso 1 · Bronze: copiar los CSV de Kaggle a Delta Lake tal cual llegan, con metadatos de ingesta."""
import _comun
from _comun import Cronometro
from pyspark.sql import functions as F
from spark_session import crear_spark
import config as C

def ubicar_datos():
    """Usa RUTA_IEEE si existe; si no, descarga la competencia con kagglehub (requiere aceptar las reglas en Kaggle)."""
    if (C.DATOS_CRUDOS / "train_transaction.csv").exists():
        return C.DATOS_CRUDOS
    import zipfile, kagglehub
    from pathlib import Path
    ruta = Path(kagglehub.competition_download("ieee-fraud-detection"))
    for z in ruta.glob("*.zip"):
        zipfile.ZipFile(z).extractall(ruta)
    return ruta

ORIGEN = ubicar_datos()
spark = crear_spark("01_bronze")
for archivo, destino in [("train_transaction.csv", C.BRONZE), ("train_identity.csv", C.BRONZE_ID)]:
    with Cronometro(f"{archivo} → {destino}"):
        df = (spark.read.option("header", True).option("inferSchema", True)
              .csv(str(ORIGEN / archivo))
              .withColumn("_archivo_origen", F.lit(archivo))
              .withColumn("_ingestado_en", F.current_timestamp()))
        df.write.format("delta").mode("overwrite").option("overwriteSchema", True).save(destino)
        print(f"  filas: {spark.read.format('delta').load(destino).count():,} · columnas: {len(df.columns)}")
spark.stop()
