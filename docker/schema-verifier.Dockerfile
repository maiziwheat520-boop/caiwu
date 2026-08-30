ARG BASE_IMAGE=ledgerbridge-app:dev
FROM ${BASE_IMAGE}

COPY --chown=10001:10001 scripts/backup_restore.py scripts/verify_schema_contracts.py ./scripts/

ENTRYPOINT ["python", "-m", "scripts.verify_schema_contracts"]
