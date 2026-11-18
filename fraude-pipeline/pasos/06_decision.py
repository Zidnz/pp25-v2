"""Paso 6 · Modelos matemáticos y estocásticos para decidir qué revisar y cuántos analistas poner."""
import json, math
import _comun
from _comun import Cronometro
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from spark_session import crear_spark
import config as C

spark = crear_spark("06_decision")
pred = spark.read.format("delta").load(C.PREDICCIONES).toPandas()
spark.stop()
val, pru = pred[pred["particion"] == "validacion"].copy(), pred[pred["particion"] == "prueba"].copy()
for d in (val, pru):
    d["perdida_esperada"] = d["prob"] * d["TransactionAmt"]
y, monto, prob, pe = pru["isFraud"].values, pru["TransactionAmt"].values, pru["prob"].values, pru["perdida_esperada"].values
fraude_total = monto[y == 1].sum()
res = {"fraude_total_prueba_usd": float(fraude_total), "transacciones_prueba": int(len(pru))}

# ---------------------------------------------------------------------------------------------
# 1. Regla de Bayes: revisar si la pérdida esperada supera el costo de revisar
#    E[pérdida | no revisar] = p·monto   vs   costo de revisar = c   →   revisar si p·monto ≥ c
# ---------------------------------------------------------------------------------------------
with Cronometro("Regla de decisión de Bayes (sin límite de capacidad)"):
    revisa = pe >= C.COSTO_REVISION_USD
    costo = revisa.sum() * C.COSTO_REVISION_USD + monto[(y == 1) & ~revisa].sum()
    res["bayes"] = {"revisadas_pct": float(revisa.mean()), "costo_total": float(costo),
                    "monto_recuperado_pct": float(monto[(y == 1) & revisa].sum() / fraude_total),
                    "ahorro_vs_no_revisar_pct": float(1 - costo / fraude_total)}
    print(f"  revisa {revisa.mean():.1%} · recupera {res['bayes']['monto_recuperado_pct']:.1%} del monto · ahorro {res['bayes']['ahorro_vs_no_revisar_pct']:.1%}")

# ---------------------------------------------------------------------------------------------
# 2. Con capacidad fija: problema de la mochila con pesos iguales.
#    max Σ p_i·monto_i·x_i  s.a.  Σ x_i ≤ k,  x_i ∈ {0,1}
#    Con pesos iguales, ordenar por p_i·monto_i es la solución óptima exacta.
# ---------------------------------------------------------------------------------------------
with Cronometro("Asignación óptima con capacidad (mochila)"):
    k = int(round(C.PRESUPUESTO_REVISION * len(pru)))
    filas = []
    for nombre, criterio in [("Por probabilidad", prob), ("Por pérdida esperada (óptimo)", pe), ("Por monto", monto)]:
        top = np.argsort(-criterio)[:k]
        filas.append({"criterio": nombre, "fraudes_atrapados": int(y[top].sum()),
                      "recall": float(y[top].sum() / y.sum()),
                      "monto_recuperado_usd": float(monto[top][y[top] == 1].sum()),
                      "monto_recuperado_pct": float(monto[top][y[top] == 1].sum() / fraude_total)})
    mochila = pd.DataFrame(filas).set_index("criterio")
    print(mochila.round(3).to_string())
    res["mochila"] = mochila.reset_index().to_dict("records")
    # Umbral para el streaming: la pérdida esperada que deja pasar el 3.35% en VALIDACIÓN
    umbral = float(np.quantile(val["perdida_esperada"], 1 - C.PRESUPUESTO_REVISION))
    json.dump({"umbral_perdida_esperada_usd": umbral, "presupuesto": C.PRESUPUESTO_REVISION},
              open(C.MODELOS / "decision.json", "w"), indent=2)
    res["umbral_perdida_esperada_usd"] = umbral
    print(f"  umbral para producción (de validación): pérdida esperada ≥ ${umbral:.2f}")

# ---------------------------------------------------------------------------------------------
# 3. Teoría de colas M/M/c (Erlang C) para dimensionar el equipo por hora del día
#    Llegadas de alertas ~ Poisson(λ_h), tiempo de revisión ~ Exponencial(μ), c analistas.
# ---------------------------------------------------------------------------------------------
def erlang_c(c, a):
    """Probabilidad de que una alerta tenga que esperar (a = λ/μ, carga ofrecida)."""
    if a >= c:
        return 1.0
    suma = sum(a ** n / math.factorial(n) for n in range(c))
    ultimo = a ** c / math.factorial(c) * c / (c - a)
    return ultimo / (suma + ultimo)

def p_espera_mayor(c, lam, mu, t):
    return erlang_c(c, lam / mu) * math.exp(-(c * mu - lam) * t) if c * mu > lam else 1.0

with Cronometro("Colas M/M/c por hora"):
    pru["hora"] = ((pru["TransactionDT"] // 3600) % 24).astype(int)
    alertas = pru[pru["perdida_esperada"] >= umbral]
    dias = (pru["TransactionDT"].max() - pru["TransactionDT"].min()) / 86400
    lam_h = alertas.groupby("hora").size().reindex(range(24), fill_value=0) / dias   # alertas por hora
    mu = 60 / C.MINUTOS_POR_REVISION                                                 # revisiones por hora por analista
    META_T, META_P = 15 / 60, 0.20                                                   # ≤20% espera más de 15 min
    plan = []
    for h, lam in lam_h.items():
        c = max(1, math.ceil(lam / mu))
        while p_espera_mayor(c, lam, mu, META_T) > META_P:
            c += 1
        wq = erlang_c(c, lam / mu) / (c * mu - lam) * 60 if lam > 0 else 0
        plan.append({"hora": h, "alertas_por_hora": lam, "analistas": c, "espera_media_min": wq,
                     "p_espera_15min": p_espera_mayor(c, lam, mu, META_T), "ocupacion": lam / (c * mu)})
    plan = pd.DataFrame(plan)
    print(plan.round(2).to_string(index=False))
    res["colas"] = {"alertas_por_dia": float(lam_h.sum()), "horas_analista_por_dia": int(plan["analistas"].sum()),
                    "pico_analistas": int(plan["analistas"].max()), "hora_pico": int(plan.loc[plan["alertas_por_hora"].idxmax(), "hora"]),
                    "plantilla_fija_necesaria": int(plan["analistas"].max())}

    # Validación por simulación de Monte Carlo (eventos discretos) en la hora pico
    hp = plan.loc[plan["alertas_por_hora"].idxmax()]
    rng = np.random.default_rng(42)
    lam, c = hp["alertas_por_hora"], int(hp["analistas"])
    esperas = []
    for _ in range(300):
        llegadas = np.cumsum(rng.exponential(1 / lam, 2000))
        servicio = rng.exponential(1 / mu, 2000)
        libres = np.zeros(c)
        for t, s in zip(llegadas, servicio):
            i = libres.argmin(); inicio = max(t, libres[i]); esperas.append(inicio - t); libres[i] = inicio + s
    esperas = np.array(esperas) * 60
    res["colas"]["simulacion_hora_pico"] = {"espera_media_min_sim": float(esperas.mean()), "espera_media_min_erlang": float(hp["espera_media_min"]),
                                            "p_espera_15_sim": float((esperas > 15).mean()), "p_espera_15_erlang": float(hp["p_espera_15min"])}
    print(f"  hora pico: Erlang C espera media {hp['espera_media_min']:.3f} min · simulación {esperas.mean():.3f} min · P(espera>15) {hp['p_espera_15min']:.3f} vs {(esperas > 15).mean():.3f}")

with Cronometro("Figuras"):
    fig, axes = plt.subplots(1, 3, figsize=(15, 3.8))
    u = np.linspace(0, 1, 401)
    costos = [(prob >= x).sum() * C.COSTO_REVISION_USD + monto[(y == 1) & (prob < x)].sum() for x in u]
    axes[0].plot(u, np.array(costos) / 1e3, color="#3d5fa8", lw=2)
    axes[0].axhline(fraude_total / 1e3, color="#9aa3b2", ls="--"); axes[0].text(.98, fraude_total / 1e3 * 1.02, "sin revisar", ha="right", fontsize=8)
    axes[0].set_xlabel("Umbral de probabilidad calibrada"); axes[0].set_ylabel("Costo total (miles USD)"); axes[0].set_title("Costo esperado según el umbral")
    m = mochila["monto_recuperado_pct"]
    axes[1].barh(m.index, m.values, color=["#c4566b" if "óptimo" in i else "#9aa3b2" for i in m.index])
    for i, v in enumerate(m.values): axes[1].text(v + .005, i, f"{v:.0%}", va="center", fontsize=9)
    axes[1].set_xlabel(f"Monto de fraude recuperado revisando {C.PRESUPUESTO_REVISION:.2%}"); axes[1].set_title("Mismo equipo, distinto criterio")
    axes[2].bar(plan["hora"], plan["analistas"], color="#2f7f6c", label="analistas")
    ax2 = axes[2].twinx(); ax2.plot(plan["hora"], plan["alertas_por_hora"], color="#c4566b", marker="o", ms=3, label="alertas/hora")
    axes[2].set_xlabel("Hora (aprox.)"); axes[2].set_ylabel("Analistas necesarios"); ax2.set_ylabel("Alertas por hora", color="#c4566b")
    axes[2].set_title("Plantilla por hora (M/M/c, ≤20% espera >15 min)")
    for a in axes: a.spines[["top"]].set_visible(False)
    plt.tight_layout(); plt.savefig(C.FIGURAS / "06_decision.png", dpi=130)
    plan.to_csv(C.REPORTE / "06_plantilla_por_hora.csv", index=False)
    json.dump(res, open(C.REPORTE / "06_decision.json", "w"), indent=2, ensure_ascii=False)
