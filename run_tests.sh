#!/usr/bin/env bash
# CI entrypoint for the ProblemFinder backend API test suite.
set -euo pipefail

pip install -r requirements-test.txt
pytest tests/ -v --tb=short
