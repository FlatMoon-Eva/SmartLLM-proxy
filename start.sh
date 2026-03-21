#!/bin/bash
set -a
source .env
set +a

litellm --config litellm_config.yaml --port 4000 --plugins middleware.py
