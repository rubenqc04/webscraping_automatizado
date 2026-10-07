---
title: WebHarvest — corrida en vivo
emoji: 🕸️
colorFrom: blue
colorTo: green
sdk: static
app_file: index.html
pinned: false
---

Panel en vivo de las corridas de WebHarvest (scraping de fuentes autorizadas
para LatamGPT). El cluster sube `state.json` cada ~2 min
(`slurm/dashboard_push.sbatch`); la página lo relee sola. Código:
`webharvest/scripts/dashboard.py` en `latam-gpt/webscraping-automatizado`.
