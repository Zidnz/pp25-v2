"""Paso 5 · Modelos en Spark MLlib con partición temporal, ablación y calibración."""
import json, os, time
import _comun
from _comun import Cronometro
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from pyspark.sql import functions as F
from spark_session import crear_spark
from features import (BASE_NUM, V_NUM, CATEGORICAS, VELOCIDAD, ESTOCASTICOS, GRAFO, RIESGO, D_NORM, AGREGADOS_CLIENTE)
from modelado import preparar, preparar_logistica, preparar_segun_tipo, pipeline_gbt, pipeline_logistica, probabilidad, platt_ajustar, platt_spark, metricas
import config as C

ITER = int(os.environ.get("GBT_ITER", "300"))
spark = crear_spark("05_modelos", memoria="4g")
gold = spark.read.format("delta").load(C.GOLD)

CONJUNTOS = {
    "1 · Base (columnas originales)": BASE_NUM + V_NUM,
    "2 · + historial del cliente en el tiempo": BASE_NUM + V_NUM + D_NORM + AGREGADOS_CLIENTE + VELOCIDAD,
    "3 · + Poisson, grafo y riesgo con retraso": BASE_NUM + V_NUM + D_NORM + AGREGADOS_CLIENTE + VELOCIDAD + ESTOCASTICOS + GRAFO + RIESGO,
}
COMPLETO = list(CONJUNTOS)[-1]

def datos(numericas, extra=None):
    df = gold if extra is None else extra
    df = preparar(df, numericas).withColumn("es_validacion", F.col("particion") == "validacion")
    ent = df.filter("particion IN ('entreno', 'validacion')")
    pru = df.filter("particion = 'prueba'")
    return ent, pru

def evaluar(nombre, modelo, pru):
    pred = probabilidad(modelo.transform(pru)).select("TransactionID", "isFraud", "score", "TransactionAmt").toPandas()
    m = metricas(pred["isFraud"], pred["score"], C.PRESUPUESTO_REVISION)
    m["modelo"] = nombre
    print(f"  {nombre}: PR-AUC {m['PR-AUC']:.3f} · ROC-AUC {m['ROC-AUC']:.3f} · recall al {C.PRESUPUESTO_REVISION:.2%}: {m['recall_presupuesto']:.1%}")
    return m, pred

def pr_validacion(modelo, df):
    """PR-AUC en el mes de validación: es lo único que se mira para ELEGIR modelo (prueba queda intacta)."""
    v = probabilidad(modelo.transform(df.filter("particion = 'validacion'"))).select("isFraud", "score").toPandas()
    return metricas(v["isFraud"], v["score"], C.PRESUPUESTO_REVISION)["PR-AUC"]

resultados, modelos = [], {}
for nombre, numericas in CONJUNTOS.items():
    with Cronometro(f"GBT {nombre} ({len(numericas)} numéricas + {len(CATEGORICAS)} categóricas)"):
        ent, pru = datos(numericas)
        t = time.time()
        modelo = pipeline_gbt(numericas, CATEGORICAS, ITER).fit(ent)
        m, pred = evaluar(f"GBT {nombre}", modelo, pru)
        m["segundos"] = round(time.time() - t); m["arboles"] = modelo.stages[-1].getNumTrees
        m["PR-AUC_validacion"] = pr_validacion(modelo, ent)
        resultados.append(m); modelos[nombre] = (modelo, numericas)

with Cronometro("Regresión logística con el conjunto completo"):
    num = CONJUNTOS[COMPLETO]
    # la logística no entiende el centinela -999: usa log(1+|x|) con signo y nulos en 0
    base_lg = preparar_logistica(gold, num)
    ent = base_lg.filter("particion = 'entreno'")
    pru = base_lg.filter("particion = 'prueba'")
    lg = pipeline_logistica(num, CATEGORICAS).fit(ent)
    m, _ = evaluar("Regresión logística (conjunto 3)", lg, pru)
    m["PR-AUC_validacion"] = pr_validacion(lg, base_lg)
    resultados.append(m)

with Cronometro("Versión estilo Kaggle: agregados con TODO el historial del cliente (incluye el futuro)"):
    # Así lo hacía el 1.er lugar: promedio sobre todas las transacciones del cliente, también las de prueba.
    cols = ["TransactionAmt", "D4n", "D10n", "D15n"] + [f"C{i}" for i in (1, 2, 4, 5, 6, 8, 9, 10, 11, 12, 13, 14)]
    agg = gold.groupBy("uid").agg(*[F.avg(c).alias(f"k_{c}_media") for c in cols], F.count("*").alias("k_uid_frec"),
                                   F.stddev("TransactionAmt").alias("k_monto_desv"))
    kag = gold.join(agg, "uid", "left")
    num_k = BASE_NUM + V_NUM + D_NORM + [f"k_{c}_media" for c in cols] + ["k_uid_frec", "k_monto_desv"]
    ent, pru = datos(num_k, kag)
    mk = pipeline_gbt(num_k, CATEGORICAS, ITER).fit(ent)
    m, _ = evaluar("GBT estilo Kaggle (no desplegable)", mk, pru)
    resultados.append(m)

tabla = pd.DataFrame(resultados).set_index("modelo")
print(tabla.round(3).to_string())
tabla.to_csv(C.REPORTE / "05_modelos.csv")

with Cronometro("Elección del modelo final (por validación), calibración de Platt y predicciones"):
    pr_gbt = tabla.loc[f"GBT {COMPLETO}", "PR-AUC_validacion"]
    pr_lg = tabla.loc["Regresión logística (conjunto 3)", "PR-AUC_validacion"]
    tipo = "logistica" if pr_lg > pr_gbt else "gbt"
    print(f"  validación PR-AUC: GBT {pr_gbt:.3f} · logística {pr_lg:.3f} → modelo final: {tipo}")
    num = CONJUNTOS[COMPLETO]
    modelo = lg if tipo == "logistica" else modelos[COMPLETO][0]
    # la logística transforma TransactionAmt con log: guardamos el monto real aparte para las decisiones en dólares
    base = (preparar_segun_tipo(gold.withColumn("monto_real", F.col("TransactionAmt")), num, tipo)
            .withColumn("es_validacion", F.col("particion") == "validacion"))
    pred = probabilidad(modelo.transform(base.filter("particion IN ('validacion', 'prueba')")))
    val = pred.filter("particion = 'validacion'").select("score", "isFraud").toPandas()
    cal = platt_ajustar(val["score"], val["isFraud"])
    pred = pred.withColumn("prob", platt_spark("score", cal))
    (pred.select("TransactionID", "TransactionDT", "particion", "uid", "isFraud", F.col("monto_real").alias("TransactionAmt"),
                 "ProductCD", "score", "prob")
         .write.format("delta").mode("overwrite").option("overwriteSchema", True).save(C.PREDICCIONES))
    modelo.write().overwrite().save(str(C.MODELOS / "modelo_final"))
    json.dump({"tipo": tipo, "calibracion": cal, "numericas": num, "categoricas": CATEGORICAS},
              open(C.MODELOS / "modelo_final.json", "w"), indent=2)
    p = spark.read.format("delta").load(C.PREDICCIONES).filter("particion = 'prueba'").select("isFraud", "prob").toPandas()
    print(f"  calibración: tasa real {p['isFraud'].mean():.4f} · probabilidad media {p['prob'].mean():.4f}")

with Cronometro("Importancia de atributos y figura"):
    gbt = modelos[COMPLETO][0].stages[-1]   # importancia del GBT completo (la logística se resume aparte)
    nombres = num + [f"{c}_idx" for c in CATEGORICAS]
    imp = pd.Series(gbt.featureImportances.toArray(), index=nombres)
    familia = {**{c: "Columnas originales" for c in BASE_NUM + V_NUM + [f"{c}_idx" for c in CATEGORICAS]},
               **{c: "Historial del cliente" for c in D_NORM + AGREGADOS_CLIENTE + VELOCIDAD},
               **{c: "Poisson (estocástico)" for c in ESTOCASTICOS}, **{c: "Grafo (discretas)" for c in GRAFO},
               **{c: "Riesgo con retraso" for c in RIESGO}}
    por_familia = imp.groupby(imp.index.map(familia)).sum().sort_values()
    imp.sort_values(ascending=False).to_csv(C.REPORTE / "05_importancia.csv")
    print(por_familia.round(3).to_string())
    fig, axes = plt.subplots(1, 2, figsize=(12, 4))
    t = tabla.drop(columns=["segundos", "arboles"], errors="ignore")
    orden = t["PR-AUC"].sort_values()
    axes[0].barh(orden.index, orden.values, color=["#9aa3b2" if "Kaggle" in i else "#3d5fa8" for i in orden.index])
    for i, v in enumerate(orden.values): axes[0].text(v + .005, i, f"{v:.3f}", va="center", fontsize=8)
    axes[0].set_xlabel("PR-AUC en el último mes"); axes[0].set_title("Aporte de cada familia de atributos")
    axes[1].barh(por_familia.index, por_familia.values, color="#2f7f6c")
    axes[1].set_xlabel("Importancia en el GBT completo"); axes[1].set_title("¿De dónde aprende el modelo?")
    for a in axes: a.spines[["top", "right"]].set_visible(False)
    plt.tight_layout(); plt.savefig(C.FIGURAS / "05_modelos.png", dpi=130)
spark.stop()
