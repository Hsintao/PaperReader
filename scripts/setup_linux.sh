#!/usr/bin/env bash
set -euo pipefail

# shellcheck disable=SC1091
source "$(conda info --base)/etc/profile.d/conda.sh"
conda activate pt

python -m pip install -r requirements.txt
cd frontend && npm install

echo "Linux setup done. For CUDA, ensure torch with matching cuda wheel is installed."
