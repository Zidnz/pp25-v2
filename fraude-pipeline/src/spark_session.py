"""Sesión de Spark con Delta Lake, conector de Kafka y driver de PostgreSQL."""
import os
from pyspark.sql import SparkSession
from delta import configure_spark_with_delta_pip

PAQUETES_EXTRA = [
    "org.apache.spark:spark-sql-kafka-0-10_2.13:4.0.1",
    "org.postgresql:postgresql:42.7.4",
]

def crear_spark(nombre="fraude", memoria=os.environ.get("SPARK_MEM", "4g")):
    builder = (SparkSession.builder.appName(nombre)
               .master(os.environ.get("SPARK_MASTER", "local[*]"))
               .config("spark.driver.memory", memoria)
               .config("spark.sql.shuffle.partitions", os.environ.get("SPARK_PARTICIONES", "8"))
               .config("spark.sql.extensions", "io.delta.sql.DeltaSparkSessionExtension")
               .config("spark.sql.catalog.spark_catalog", "org.apache.spark.sql.delta.catalog.DeltaCatalog")
               .config("spark.sql.session.timeZone", "UTC")
               .config("spark.ui.enabled", "false")
               .config("spark.ui.showConsoleProgress", "false"))
    spark = configure_spark_with_delta_pip(builder, extra_packages=PAQUETES_EXTRA).getOrCreate()
    spark.sparkContext.setLogLevel("ERROR")
    return spark
