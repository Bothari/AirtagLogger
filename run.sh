#!/bin/bash
docker build -t airtag-logger . && \
docker run --rm -d \
  -p 8743:8080 \
  -v "$(pwd)/data:/data" \
  airtag-logger
