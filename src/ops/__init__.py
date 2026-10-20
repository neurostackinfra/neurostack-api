"""Operational one-off commands shipped in the production image.

Each module runs as ``python -m src.ops.<name>`` (for example a one-off ECS
task on the service's own task definition), so credentials stay inside the
task and never reach an operator shell.
"""
