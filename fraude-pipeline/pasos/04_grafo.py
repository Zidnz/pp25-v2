"""Paso 4 · Matemáticas discretas: grafo bipartito cliente–dispositivo y anillos de fraude."""
import json
import _comun
from _comun import Cronometro
import networkx as nx
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from spark_session import crear_spark
import config as C

spark = crear_spark("04_grafo")
with Cronometro("Aristas cliente–dispositivo del periodo de entrenamiento"):
    aristas = (spark.read.format("delta").load(C.GOLD)
               .filter("particion = 'entreno' AND dispositivo != 'desconocido'")
               .groupBy("uid", "dispositivo").agg({"isFraud": "max", "*": "count"})
               .withColumnRenamed("max(isFraud)", "fraude").withColumnRenamed("count(1)", "n").toPandas())
spark.stop()

with Cronometro("Grafo y componentes conexas"):
    grado_disp = aristas.groupby("dispositivo")["uid"].nunique()
    # Algunas huellas siguen siendo muy comunes (configuraciones de fábrica idénticas) y conectan a muchos
    # clientes sin relación. Se quitan las de grado > 30.
    HUB = 30
    poco_comunes = grado_disp[grado_disp <= HUB].index
    a = aristas[aristas["dispositivo"].isin(poco_comunes)]
    G = nx.Graph()
    G.add_nodes_from(("c:" + a["uid"]).unique(), tipo="cliente")
    G.add_nodes_from(("d:" + a["dispositivo"]).unique(), tipo="dispositivo")
    G.add_edges_from(zip("c:" + a["uid"], "d:" + a["dispositivo"]))
    fraude_cliente = a.groupby("uid")["fraude"].max()
    filas = []
    for comp in nx.connected_components(G):
        clientes = [n[2:] for n in comp if n.startswith("c:")]
        if len(clientes) < 2:
            continue
        f = int(fraude_cliente.loc[clientes].sum())
        filas.append({"clientes": len(clientes), "dispositivos": len(comp) - len(clientes), "clientes_fraude": f})
    import pandas as pd
    comps = pd.DataFrame(filas)
    tasa_base = fraude_cliente.mean()
    anillos = comps[comps["clientes_fraude"] >= 2]
    # ¿Cuántos anillos con ≥2 defraudadores esperaríamos si el fraude se repartiera al azar entre clientes?
    rng = np.random.default_rng(0)
    simulados = []
    for _ in range(200):
        f = rng.binomial(comps["clientes"].values, tasa_base)
        simulados.append(int((f >= 2).sum()))
    res = {
        "aristas": int(len(aristas)), "dispositivos": int(aristas["dispositivo"].nunique()),
        "dispositivos_hub_quitados": int((grado_disp > HUB).sum()),
        "clientes_en_grafo": int(a["uid"].nunique()), "tasa_fraude_clientes": float(tasa_base),
        "componentes_multicliente": int(len(comps)), "anillos_2mas_fraudes": int(len(anillos)),
        "anillos_esperados_al_azar": float(np.mean(simulados)), "anillos_esperados_p95": float(np.percentile(simulados, 95)),
        "clientes_fraude_en_anillos": int(anillos["clientes_fraude"].sum()),
        "mayor_componente_clientes": int(comps["clientes"].max()),
        "anillos_de_2_a_20_clientes": int(((anillos["clientes"] >= 2) & (anillos["clientes"] <= 20)).sum()),
        "tasa_fraude_en_anillos": float(anillos["clientes_fraude"].sum() / anillos["clientes"].sum()) if len(anillos) else 0,
    }
    for k, v in res.items():
        print(f"  {k}: {v:,.4f}" if isinstance(v, float) else f"  {k}: {v:,}")
    json.dump(res, open(C.REPORTE / "04_grafo.json", "w"), indent=2)

with Cronometro("Figura de un anillo"):
    mejor = None
    for comp in nx.connected_components(G):
        cl = [n for n in comp if n.startswith("c:")]
        f = sum(fraude_cliente.get(n[2:], 0) for n in cl)
        if f >= 3 and len(comp) <= 40 and (mejor is None or f > mejor[1]):
            mejor = (comp, f)
    if mejor:
        H = G.subgraph(mejor[0])
        pos = nx.spring_layout(H, seed=3)
        col = ["#9aa3b2" if n.startswith("d:") else ("#c4566b" if fraude_cliente.get(n[2:], 0) else "#3d5fa8") for n in H.nodes]
        forma = [180 if n.startswith("c:") else 90 for n in H.nodes]
        fig, ax = plt.subplots(figsize=(6, 4.4))
        nx.draw_networkx_edges(H, pos, ax=ax, edge_color="#c9ced8")
        nx.draw_networkx_nodes(H, pos, ax=ax, node_color=col, node_size=forma)
        ax.set_title(f"Un anillo: {mejor[1]} clientes con fraude que comparten dispositivos\n(rojo: cliente con fraude · azul: cliente sin fraude · gris: dispositivo)", fontsize=9)
        ax.axis("off"); plt.tight_layout(); plt.savefig(C.FIGURAS / "04_anillo.png", dpi=130)
