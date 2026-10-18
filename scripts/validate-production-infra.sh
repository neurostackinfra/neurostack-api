#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"
python_bin="${PYTHON:-python3}"

"$python_bin" scripts/validate_asaas_producer_release_readbacks.py --check-template
"$python_bin" scripts/validate_asaas_producer_deploy_readback_policy.py --check-template
"$python_bin" scripts/asaas_production_wiring.py check-manifest --params infra/production/asaas-production-wiring.params.yaml
"$python_bin" -m pytest -q \
  tests/scripts/test_production_infra.py \
  tests/scripts/test_asaas_producer_release_readbacks.py \
  tests/scripts/test_asaas_bootstrap_slot.py \
  tests/scripts/test_asaas_backend_trust_readback.py \
  tests/scripts/test_asaas_build_workflow.py \
  tests/scripts/test_sign_asaas_build_attestation.py \
  tests/scripts/test_asaas_release_preflight_workflow.py \
  tests/scripts/test_asaas_producer_deploy_readback_policy.py \
  tests/scripts/test_asaas_producer_deploy_workflow.py \
  tests/scripts/test_asaas_producer_rollout_gate.py \
  tests/scripts/test_asaas_producer_deploy_executor.py \
  tests/scripts/test_asaas_database_secret_reference.py \
  tests/scripts/test_asaas_registration_database_readiness_sql.py \
  tests/scripts/test_verify_asaas_producer_image_rollout.py \
  tests/scripts/test_package_asaas_build_ui_evidence.py \
  tests/scripts/test_asaas_production_wiring.py \
  tests/scripts/test_production_migration_window.py \
  tests/api/test_billing_producer_build_trust.py
