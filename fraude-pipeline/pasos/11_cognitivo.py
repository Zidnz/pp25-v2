"""Paso 11 · Cómputo cognitivo: explicar cada alerta en español y razonar con casos parecidos.

Tres piezas clásicas de un sistema cognitivo de apoyo a analistas:
  1. Explicación: qué rasgos de la transacción son raros y cuánto se asocian con fraude en el pasado.
  2. Razonamiento basado en casos (CBR): los k casos ya resueltos más parecidos y cómo terminaron.
  3. Reglas de experto: una acción sugerida a partir de la probabilidad y de los casos.
Todo se guarda en la tabla operativa de PostgreSQL para que el analista lo vea junto a la alerta.
"""
import json
import _comun
from _comun import Cronometro
import numpy as np
import pandas as pd
from sklearn.neighbors import NearestNeighbors
from spark_session import crear_spark
import config as C

RASGOS = {   # rasgo: plantilla en español
    "n_1h": "hizo {v:.0f} transacciones en la última hora",
    "n_24h": "hizo {v:.0f} transacciones en las últimas 24 horas",
    "sorpresa_poisson": "su ritmo de compras es muy raro para su historial (probabilidad Poisson ≈ {p})",
    "ratio_monto_cliente": "el monto es {v:.1f} veces su promedio",
    "riesgo_tarjeta": "su grupo de tarjeta (card1) tiene {v:.0%} de fraude ya confirmado",
    "riesgo_cliente": "el cliente ya tuvo fraude confirmado (riesgo {v:.0%})",
    "riesgo_direccion": "su zona (addr1) tiene {v:.0%} de fraude ya confirmado",
    "grado_dispositivo": "su dispositivo lo han usado {v:.0f} clientes distintos",
    "grado_cliente": "el cliente ha usado {v:.0f} dispositivos distintos",
    "uid_emails_distintos": "el cliente ha usado {v:.0f} correos distintos",
    "C1": "el contador C1 de Vesta vale {v:.0f}",
    "C13": "el contador C13 de Vesta vale {v:.0f}",
    "TransactionAmt": "el monto es de ${v:,.2f}",
    "frec_card1": "su grupo de tarjeta solo aparece {v:.0f} veces antes",
}
BAJOS_SON_RAROS = {"frec_card1"}   # en estos, lo raro es tener valores bajos

spark = crear_spark("11_cognitivo")
with Cronometro("Referencia: transacciones de entrenamiento con etiqueta"):
    ref = (spark.read.format("delta").load(C.GOLD).filter("particion = 'entreno'")
           .select("TransactionID", "isFraud", *RASGOS).sample(fraction=0.4, seed=7).toPandas())
    alertas = spark.read.format("delta").load(C.ALERTAS_DELTA).toPandas()
    pru = spark.read.format("delta").load(C.PREDICCIONES).filter("particion = 'prueba'").select("TransactionID", "isFraud").toPandas()
spark.stop()
alertas = alertas.merge(pru, on="TransactionID", how="left")   # la etiqueta solo se usa para EVALUAR, no para explicar
base = ref["isFraud"].mean()
print(f"  referencia: {len(ref):,} casos · alertas a explicar: {len(alertas):,}")

# --- 1. Explicación: rareza (percentil) y "lift" de fraude en la cola de cada rasgo ---
ordenados = {r: np.sort(ref[r].dropna().values) for r in RASGOS}
def lift_y_percentil(rasgo, v):
    x = ordenados[rasgo]
    pct = np.searchsorted(x, v, side="right") / len(x)
    serie = ref[rasgo]
    cola = ref[serie <= v] if rasgo in BAJOS_SON_RAROS else ref[serie >= v]
    if len(cola) < 30:
        return None, None
    return (1 - pct if rasgo in BAJOS_SON_RAROS else pct), cola["isFraud"].mean() / base

def explicar(fila):
    razones = []
    for r, plantilla in RASGOS.items():
        v = fila[r]
        if pd.isna(v) or v == -999:
            continue
        pct, lift = lift_y_percentil(r, v)
        if pct is not None and pct >= 0.90 and lift >= 1.5:
            texto = plantilla.format(v=v, p=f"{10 ** -v:.0e}" if r == "sorpresa_poisson" else "")
            razones.append((lift, f"{texto} (supera al {min(pct, 0.99):.0%} de las transacciones; en ese grupo el fraude es {lift:.1f} veces más común)"))
    razones.sort(reverse=True)
    if not razones:
        return "Ningún rasgo aislado destaca; la alerta viene de la combinación de muchos atributos."
    return "Alerta porque " + "; ".join(t for _, t in razones[:3]) + "."

with Cronometro("Explicaciones"):
    alertas["explicacion"] = alertas.apply(explicar, axis=1)

# --- 2. Razonamiento basado en casos: k vecinos más cercanos en el espacio de percentiles ---
with Cronometro("Casos parecidos (k-NN)"):
    def a_percentiles(df):
        return np.column_stack([np.searchsorted(ordenados[r], df[r].fillna(np.nanmedian(ordenados[r])).values) / len(ordenados[r])
                                for r in RASGOS])
    K = 7
    nn = NearestNeighbors(n_neighbors=K).fit(a_percentiles(ref))
    _, idx = nn.kneighbors(a_percentiles(alertas))
    vecinos_fraude = ref["isFraud"].values[idx].sum(axis=1)
    alertas["vecinos_fraude"] = vecinos_fraude
    alertas["casos_similares"] = [f"De {K} casos parecidos ya resueltos, {f} fueron fraude (IDs: {', '.join(map(str, ref['TransactionID'].values[i][:3]))}…)"
                                  for f, i in zip(vecinos_fraude, idx)]

# --- 3. Reglas de experto para la acción sugerida ---
def accion(f):
    if f["prob"] >= 0.9 or f["vecinos_fraude"] >= 5:
        return "Bloquear la tarjeta y contactar al titular"
    if f["prob"] >= 0.5 or f["vecinos_fraude"] >= 3:
        return "Revisar de inmediato"
    return "Revisar en la cola normal"
alertas["accion_sugerida"] = alertas.apply(accion, axis=1)

with Cronometro("Guardar en PostgreSQL"):
    con = C.pg_conexion(); cur = con.cursor()
    cur.executemany("UPDATE operacion.alertas SET explicacion=%s, casos_similares=%s, accion_sugerida=%s, actualizada_en=now() WHERE transaction_id=%s",
                    list(zip(alertas["explicacion"], alertas["casos_similares"], alertas["accion_sugerida"], alertas["TransactionID"].astype(int))))
    con.commit()
    cur.execute("SELECT accion_sugerida, count(*) FROM operacion.alertas GROUP BY 1 ORDER BY 2 DESC"); print("  ", cur.fetchall())
    con.close()

with Cronometro("¿Ayudan los casos parecidos? (evaluación con las etiquetas reales del mes de prueba)"):
    ev = alertas.dropna(subset=["isFraud"])
    tabla = ev.groupby("accion_sugerida")["isFraud"].agg(alertas="count", tasa_fraude_real="mean").sort_values("tasa_fraude_real", ascending=False)
    voto = ev.groupby(ev["vecinos_fraude"] >= 4)["isFraud"].mean()
    # ¿los vecinos aportan algo que la probabilidad del modelo no tenga? (ROC-AUC dentro de las alertas)
    from sklearn.metrics import roc_auc_score
    from scipy.stats import rankdata
    auc = {"solo_modelo": roc_auc_score(ev["isFraud"], ev["prob"]),
           "solo_vecinos": roc_auc_score(ev["isFraud"], ev["vecinos_fraude"]),
           "modelo_mas_vecinos": roc_auc_score(ev["isFraud"], rankdata(ev["prob"]) + rankdata(ev["vecinos_fraude"]))}
    print("  ROC-AUC dentro de las alertas:", {k: round(v, 3) for k, v in auc.items()})
    print(tabla.round(3).to_string())
    print(f"  fraude real cuando ≥4 de 7 vecinos fueron fraude: {voto.get(True, float('nan')):.1%} · cuando no: {voto.get(False, float('nan')):.1%}")
    ejemplos = ev.sort_values("prob", ascending=False).groupby("accion_sugerida").head(1)
    salida = {"por_accion": tabla.reset_index().to_dict("records"),
              "auc_dentro_de_alertas": auc, "fraude_si_vecinos_mayoria": float(voto.get(True, np.nan)), "fraude_si_no": float(voto.get(False, np.nan)),
              "ejemplos": ejemplos[["TransactionID", "TransactionAmt", "prob", "accion_sugerida", "explicacion", "casos_similares", "isFraud"]].to_dict("records")}
    for e in salida["ejemplos"]:
        print(f"\n  #{e['TransactionID']} · ${e['TransactionAmt']:.2f} · prob {e['prob']:.2f} · {e['accion_sugerida']} · fraude real: {int(e['isFraud'])}\n    {e['explicacion']}\n    {e['casos_similares']}")
    json.dump(salida, open(C.REPORTE / "11_cognitivo.json", "w"), indent=2, ensure_ascii=False, default=float)
