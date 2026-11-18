#!/usr/bin/env bash
# Corre el proyecto completo, paso por paso. Requiere: docker compose up -d  y  pip install -r requirements.txt
# Opcional: export RUTA_IEEE=/carpeta/con/train_transaction.csv  (si no, se descarga con kagglehub)
set -euo pipefail
cd "$(dirname "$0")/pasos"
for paso in 01_bronze 02_silver 03_gold 04_grafo 05_modelos 06_decision \
            07_productor_kafka 08_consumidor_streaming 09_paridad 10_data_warehouse 11_cognitivo; do
  echo "=================== $paso ==================="
  python -u "$paso.py"
done
