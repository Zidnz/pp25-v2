"""Paso 10 · Data warehouse en estrella (PostgreSQL) y consultas OLAP para el negocio."""
import warnings
warnings.filterwarnings("ignore", message="pandas only supports SQLAlchemy")
import json
import _comun
from _comun import Cronometro
import pandas as pd
from pyspark.sql import functions as F
from spark_session import crear_spark
import config as C

spark = crear_spark("10_dw")
con = C.pg_conexion(); cur = con.cursor()
with Cronometro("Esquema dw"):
    cur.execute(open(C.RAIZ / "sql" / "02_data_warehouse.sql").read()); con.commit()

def a_pg(df, tabla):
    df.write.jdbc(C.PG_JDBC, tabla, mode="append", properties=C.PG_PROPS)

with Cronometro("Dimensiones"):
    s = spark.read.format("delta").load(C.SILVER).withColumn("dia_id", F.floor(F.col("TransactionDT") / 86400).cast("int"))
    tiempo = (s.groupBy("dia_id").agg(F.first("particion").alias("periodo"))
               .withColumn("semana", (F.col("dia_id") / 7).cast("int")).withColumn("dia_semana", F.col("dia_id") % 7)
               .withColumn("mes_relativo", (F.col("dia_id") / 30).cast("int")))
    a_pg(tiempo.select("dia_id", "semana", "dia_semana", "mes_relativo", "periodo"), "dw.dim_tiempo")
    a_pg(s.select(F.col("ProductCD").alias("codigo")).distinct(), "dw.dim_producto")
    a_pg(s.select(F.coalesce("card4", F.lit("desconocida")).alias("red"), F.coalesce("card6", F.lit("desconocido")).alias("tipo")).distinct(), "dw.dim_tarjeta")
    a_pg(s.select(F.coalesce("DeviceType", F.lit("desconocido")).alias("dispositivo"), F.coalesce("id_31", F.lit("desconocido")).alias("navegador")).distinct(), "dw.dim_canal")
    a_pg(s.select(F.coalesce("P_emaildomain", F.lit("desconocido")).alias("dominio")).distinct(), "dw.dim_email")

with Cronometro("Tabla de hechos"):
    leer = lambda t: spark.read.jdbc(C.PG_JDBC, t, properties=C.PG_PROPS)
    pred = spark.read.format("delta").load(C.PREDICCIONES).select("TransactionID", "prob")
    umbral = json.load(open(C.MODELOS / "decision.json"))["umbral_perdida_esperada_usd"]
    f = (s.join(pred, "TransactionID", "left")
          .withColumn("red", F.coalesce("card4", F.lit("desconocida"))).withColumn("tipo", F.coalesce("card6", F.lit("desconocido")))
          .withColumn("dispositivo", F.coalesce("DeviceType", F.lit("desconocido"))).withColumn("navegador", F.coalesce("id_31", F.lit("desconocido")))
          .withColumn("dominio", F.coalesce("P_emaildomain", F.lit("desconocido")))
          .join(leer("dw.dim_producto"), F.col("ProductCD") == F.col("codigo"))
          .join(leer("dw.dim_tarjeta"), ["red", "tipo"]).join(leer("dw.dim_canal"), ["dispositivo", "navegador"])
          .join(leer("dw.dim_email"), "dominio")
          .select(F.col("TransactionID").alias("transaction_id"), "dia_id", "producto_id", "tarjeta_id", "canal_id", "email_id",
                  ((F.col("TransactionDT") / 3600) % 24).cast("short").alias("hora"), F.col("TransactionAmt").alias("monto"),
                  F.col("isFraud").cast("short").alias("es_fraude"), F.col("prob").alias("prob_modelo"),
                  (F.col("prob") * F.col("TransactionAmt")).alias("perdida_esperada"),
                  F.when(F.col("prob").isNotNull(), F.col("prob") * F.col("TransactionAmt") >= umbral).alias("alerta")))
    a_pg(f.repartition(4), "dw.fact_transaccion")
    cur.execute("SELECT count(*) FROM dw.fact_transaccion"); print(f"  hechos cargados: {cur.fetchone()[0]:,}")
spark.stop()

CONSULTAS = {
 "fraude_por_mes_y_producto (ROLLUP)": """
    SELECT t.mes_relativo, p.codigo AS producto, count(*) AS transacciones, sum(f.es_fraude) AS fraudes,
           round(avg(f.es_fraude)::numeric, 4) AS tasa, round(sum(f.monto * f.es_fraude), 0) AS monto_defraudado
    FROM dw.fact_transaccion f JOIN dw.dim_tiempo t USING (dia_id) JOIN dw.dim_producto p USING (producto_id)
    GROUP BY ROLLUP (t.mes_relativo, p.codigo) ORDER BY t.mes_relativo NULLS LAST, p.codigo NULLS LAST""",
 "desempeno_del_modelo_por_semana": """
    SELECT t.semana, t.periodo, count(*) FILTER (WHERE f.alerta) AS alertas, sum(f.es_fraude) AS fraudes,
           sum(f.es_fraude) FILTER (WHERE f.alerta) AS fraudes_atrapados,
           round((sum(f.es_fraude) FILTER (WHERE f.alerta))::numeric / NULLIF(count(*) FILTER (WHERE f.alerta), 0), 3) AS precision,
           round((sum(f.es_fraude) FILTER (WHERE f.alerta))::numeric / NULLIF(sum(f.es_fraude), 0), 3) AS recall,
           round(sum(f.monto) FILTER (WHERE f.alerta AND f.es_fraude = 1), 0) AS monto_recuperado
    FROM dw.fact_transaccion f JOIN dw.dim_tiempo t USING (dia_id)
    WHERE t.periodo IN ('validacion', 'prueba') GROUP BY t.semana, t.periodo ORDER BY t.semana""",
 "tarjeta_x_dispositivo (GROUPING SETS)": """
    SELECT tj.tipo, c.dispositivo, count(*) AS transacciones, round(avg(f.es_fraude)::numeric, 4) AS tasa,
           GROUPING(tj.tipo, c.dispositivo) AS nivel
    FROM dw.fact_transaccion f JOIN dw.dim_tarjeta tj USING (tarjeta_id) JOIN dw.dim_canal c USING (canal_id)
    WHERE tj.tipo IN ('credit', 'debit')
    GROUP BY GROUPING SETS ((tj.tipo, c.dispositivo), (tj.tipo), ()) ORDER BY nivel, tasa DESC""",
 "top_dominios_de_correo_por_riesgo": """
    SELECT e.dominio, count(*) AS transacciones, round(avg(f.es_fraude)::numeric, 4) AS tasa,
           rank() OVER (ORDER BY avg(f.es_fraude) DESC) AS posicion
    FROM dw.fact_transaccion f JOIN dw.dim_email e USING (email_id)
    GROUP BY e.dominio HAVING count(*) >= 2000 ORDER BY tasa DESC LIMIT 8""",
}
with Cronometro("Consultas OLAP"):
    salida = {}
    for nombre, q in CONSULTAS.items():
        df = pd.read_sql(q, con)
        salida[nombre] = df
        print(f"\n  ── {nombre}\n" + df.head(14).to_string(index=False))
        df.to_csv(C.REPORTE / f"10_{nombre.split(' ')[0]}.csv", index=False)
    open(C.REPORTE / "10_consultas.sql", "w").write("\n\n".join(f"-- {k}\n{v.strip()};" for k, v in CONSULTAS.items()))
con.close()
