#!/usr/bin/env bash
# CI entrypoint for the ProblemFinder backend API test suite.
#
# Uses `python -m pip` / `python -m pytest` so dependency installation and test
# execution always share the same interpreter.
set -euo pipefail

python -m pip install -r requirements-test.txt
python -m pytest tests/ -v --tb=short
