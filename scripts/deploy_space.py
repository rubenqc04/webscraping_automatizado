#!/usr/bin/env python3
"""Crea o actualiza el Space privado (estático) del panel en vivo.

    python scripts/deploy_space.py [--space latam-gpt/webharvest-dashboard]

Crea el Space si no existe (privado, estático: el único tipo gratuito en la
organización) y sube la página (scripts/dashboard.html como index.html) y
space/README.md. El estado (state.json) no lo sube este script: lo publica
cada ~2 min slurm/dashboard_push.sbatch.
Requiere huggingface_hub y un token con escritura en la organización.
"""
import argparse
from pathlib import Path

from huggingface_hub import CommitOperationAdd, HfApi

ROOT = Path(__file__).resolve().parents[1]

ap = argparse.ArgumentParser(description=__doc__,
                             formatter_class=argparse.RawDescriptionHelpFormatter)
ap.add_argument("--space", default="latam-gpt/webharvest-dashboard")
a = ap.parse_args()

api = HfApi()
api.create_repo(a.space, repo_type="space", space_sdk="static", private=True, exist_ok=True)
files = {"index.html": ROOT / "scripts/dashboard.html", "README.md": ROOT / "space/README.md"}
api.create_commit(a.space, repo_type="space", commit_message="Actualizar página del panel",
                  operations=[CommitOperationAdd(k, str(v)) for k, v in files.items()])
print(f"Space: https://huggingface.co/spaces/{a.space}")
