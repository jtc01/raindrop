#!/bin/bash
PATH=$PATH:$LAMBDA_TASK_ROOT/bin PYTHONPATH=$PYTHONPATH:/opt/python:$LAMBDA_RUNTIME_DIR exec python -m uvicorn --host 0.0.0.0 --port=$PORT app:app
