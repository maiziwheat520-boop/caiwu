"""Atomically materialize a private batch of bank-statement cutover plans."""

from ledgerbridge.bank_statement_plan_batch import run_bank_statement_plan_batch_builder

if __name__ == "__main__":
    raise SystemExit(run_bank_statement_plan_batch_builder())
