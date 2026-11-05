#!/bin/sh
set -e

echo "Starting application on port ${PORT:-7860}..."
exec gunicorn app:app --bind "0.0.0.0:${PORT:-7860}" --workers 1 --threads 4
