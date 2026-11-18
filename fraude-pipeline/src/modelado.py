"""Piezas de modelado en Spark MLlib compartidas por el entrenamiento y el streaming."""
import json
import numpy as np
from pyspark.ml import Pipeline, PipelineModel
from pyspark.ml.feature import StringIndexer, VectorAssembler, StandardScaler, OneHotEncoder
from pyspark.ml.classification import GBTClassifier, LogisticRegression
from pyspark.ml.functions import vector_to_array
from pyspark.sql import functions as F

import os
RELLENO = -999.0
# Spark para cuando la pérdida en validación mejora menos que tol·pérdida; 0.0005 cortaba a los ~20 árboles
TOLERANCIA = float(os.environ.get("GBT_TOL", "1e-5"))   # los árboles separan bien un valor centinela; la logística usa imputación por mediana

def preparar(df, numericas):
    """Rellena nulos numéricos y agrega el peso de clase (los fraudes pesan más al entrenar)."""
    df = df.fillna(RELLENO, subset=numericas)
    return df.withColumn("peso", F.when(F.col("isFraud") == 1, 10.0).otherwise(1.0))

def pipeline_gbt(numericas, categoricas, iteraciones=150):
    idx = [StringIndexer(inputCol=c, outputCol=f"{c}_idx", handleInvalid="keep") for c in categoricas]
    ensamble = VectorAssembler(inputCols=numericas + [f"{c}_idx" for c in categoricas], outputCol="x", handleInvalid="keep")
    gbt = GBTClassifier(featuresCol="x", labelCol="isFraud", weightCol="peso", maxIter=iteraciones, maxDepth=6,
                        stepSize=0.1, subsamplingRate=0.8, featureSubsetStrategy="0.5", maxBins=160,
                        validationIndicatorCol="es_validacion", validationTol=TOLERANCIA, seed=42)
    return Pipeline(stages=idx + [ensamble, gbt])

def pipeline_logistica(numericas, categoricas):
    idx = [StringIndexer(inputCol=c, outputCol=f"{c}_idx", handleInvalid="keep") for c in categoricas]
    ohe = OneHotEncoder(inputCols=[f"{c}_idx" for c in categoricas], outputCols=[f"{c}_ohe" for c in categoricas], handleInvalid="keep")
    ensamble = VectorAssembler(inputCols=numericas + [f"{c}_ohe" for c in categoricas], outputCol="x0", handleInvalid="keep")
    escala = StandardScaler(inputCol="x0", outputCol="x", withMean=True, withStd=True)
    lr = LogisticRegression(featuresCol="x", labelCol="isFraud", weightCol="peso", maxIter=200, regParam=0.001, elasticNetParam=0.0)
    return Pipeline(stages=idx + [ohe, ensamble, escala, lr])

def probabilidad(pred):
    return pred.withColumn("score", vector_to_array("probability")[1])

def platt_ajustar(score, y):
    """Calibración de Platt: p = 1 / (1 + exp(-(a·logit(score) + b))), ajustada por máxima verosimilitud."""
    from sklearn.linear_model import LogisticRegression as LR
    s = np.clip(np.asarray(score), 1e-6, 1 - 1e-6)
    z = np.log(s / (1 - s)).reshape(-1, 1)
    m = LR(C=1e6).fit(z, y)
    return {"a": float(m.coef_[0][0]), "b": float(m.intercept_[0])}

def platt_spark(col_score, cal):
    s = F.least(F.greatest(F.col(col_score), F.lit(1e-6)), F.lit(1 - 1e-6))
    z = F.log(s / (1 - s))
    return 1 / (1 + F.exp(-(F.lit(cal["a"]) * z + F.lit(cal["b"]))))

def metricas(y, p, presupuesto):
    from sklearn.metrics import average_precision_score, roc_auc_score
    y, p = np.asarray(y), np.asarray(p)
    k = int(round(presupuesto * len(y)))
    top = np.argsort(-p)[:k]
    return {"PR-AUC": float(average_precision_score(y, p)), "ROC-AUC": float(roc_auc_score(y, p)),
            "recall_presupuesto": float(y[top].sum() / y.sum()), "precision_presupuesto": float(y[top].mean())}

def preparar_logistica(df, numericas):
    """La logística necesita escalas parecidas: log con signo y nulos en 0."""
    for c in numericas:
        df = df.withColumn(c, F.when(F.col(c).isNull(), 0.0)
                              .otherwise(F.signum(c) * F.log1p(F.abs(F.col(c)))))
    return df.withColumn("peso", F.when(F.col("isFraud") == 1, 10.0).otherwise(1.0))


def preparar_segun_tipo(df, numericas, tipo):
    """El streaming usa esto para preparar los datos igual que el modelo final, sea árbol o logística."""
    return preparar_logistica(df, numericas) if tipo == "logistica" else preparar(df, numericas)
