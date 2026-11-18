-- fraude_por_mes_y_producto (ROLLUP)
SELECT t.mes_relativo, p.codigo AS producto, count(*) AS transacciones, sum(f.es_fraude) AS fraudes,
           round(avg(f.es_fraude)::numeric, 4) AS tasa, round(sum(f.monto * f.es_fraude), 0) AS monto_defraudado
    FROM dw.fact_transaccion f JOIN dw.dim_tiempo t USING (dia_id) JOIN dw.dim_producto p USING (producto_id)
    GROUP BY ROLLUP (t.mes_relativo, p.codigo) ORDER BY t.mes_relativo NULLS LAST, p.codigo NULLS LAST;

-- desempeno_del_modelo_por_semana
SELECT t.semana, t.periodo, count(*) FILTER (WHERE f.alerta) AS alertas, sum(f.es_fraude) AS fraudes,
           sum(f.es_fraude) FILTER (WHERE f.alerta) AS fraudes_atrapados,
           round((sum(f.es_fraude) FILTER (WHERE f.alerta))::numeric / NULLIF(count(*) FILTER (WHERE f.alerta), 0), 3) AS precision,
           round((sum(f.es_fraude) FILTER (WHERE f.alerta))::numeric / NULLIF(sum(f.es_fraude), 0), 3) AS recall,
           round(sum(f.monto) FILTER (WHERE f.alerta AND f.es_fraude = 1), 0) AS monto_recuperado
    FROM dw.fact_transaccion f JOIN dw.dim_tiempo t USING (dia_id)
    WHERE t.periodo IN ('validacion', 'prueba') GROUP BY t.semana, t.periodo ORDER BY t.semana;

-- tarjeta_x_dispositivo (GROUPING SETS)
SELECT tj.tipo, c.dispositivo, count(*) AS transacciones, round(avg(f.es_fraude)::numeric, 4) AS tasa,
           GROUPING(tj.tipo, c.dispositivo) AS nivel
    FROM dw.fact_transaccion f JOIN dw.dim_tarjeta tj USING (tarjeta_id) JOIN dw.dim_canal c USING (canal_id)
    WHERE tj.tipo IN ('credit', 'debit')
    GROUP BY GROUPING SETS ((tj.tipo, c.dispositivo), (tj.tipo), ()) ORDER BY nivel, tasa DESC;

-- top_dominios_de_correo_por_riesgo
SELECT e.dominio, count(*) AS transacciones, round(avg(f.es_fraude)::numeric, 4) AS tasa,
           rank() OVER (ORDER BY avg(f.es_fraude) DESC) AS posicion
    FROM dw.fact_transaccion f JOIN dw.dim_email e USING (email_id)
    GROUP BY e.dominio HAVING count(*) >= 2000 ORDER BY tasa DESC LIMIT 8;