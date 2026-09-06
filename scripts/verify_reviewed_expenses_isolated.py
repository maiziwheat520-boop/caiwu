"""Run an exact-fact correction rehearsal in an isolated ephemeral production clone.

Run on the authorized host with Docker access. Database dumps stay in pipes;
the temporary PostgreSQL has no network and stores its data on tmpfs only.
No credentials, financial fields, or dump content are printed.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess  # nosec B404
import time
from pathlib import Path


def run(args: list[str], *, data: bytes | None = None) -> bytes:
    if args[0] == "docker":
        args = ["/usr/bin/docker", *args[1:]]
    result = subprocess.run(args, input=data, capture_output=True, check=False)  # nosec B603
    if result.returncode:
        # Fixed operation names only; never echo argv, input, stderr or a dump.
        operation = next(
            (
                value
                for value in ("pg_dumpall", "pg_dump", "pg_restore", "createdb", "psql", "python")
                if value in args
            ),
            args[1],
        )
        print("FAILED_OPERATION=" + operation)
        if operation == "python" and result.stdout.startswith(b'{"diagnostic":'):
            print(result.stdout.decode().strip())
        raise RuntimeError("ISOLATED_COMMAND_FAILED")
    return result.stdout


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", type=Path, required=True)
    parser.add_argument("--plan-sha256", required=True)
    args = parser.parse_args()
    stage = args.stage.resolve(strict=True)
    if not stage.is_dir() or stage.is_symlink():
        raise ValueError("STAGE_INVALID")
    name = "ledgerbridge-monthly-check-" + secrets.token_hex(6)
    source = json.loads(run(["docker", "inspect", "ledgerbridge-postgres-1"]))[0]
    image = source["Config"]["Image"]
    app = json.loads(run(["docker", "inspect", "ledgerbridge-api-1"]))[0]["Config"]["Image"]
    password = secrets.token_urlsafe(32)
    os.environ["POSTGRES_PASSWORD"] = password
    os.environ["TEST_DB_PASSWORD"] = password
    created = False
    try:
        run(
            [
                "docker",
                "run",
                "-d",
                "--name",
                name,
                "--network",
                "none",
                "--tmpfs",
                "/var/lib/postgresql/data:rw,size=1073741824",
                "--shm-size",
                "128m",
                "--memory",
                "1536m",
                "--pids-limit",
                "128",
                "--security-opt",
                "no-new-privileges",
                "-e",
                "POSTGRES_PASSWORD",
                image,
            ]
        )
        created = True
        for _ in range(60):
            result = subprocess.run(
                ["/usr/bin/docker", "exec", name, "pg_isready", "-U", "postgres"],
                capture_output=True,
                check=False,
            )  # nosec B603
            if result.returncode == 0:
                break
            time.sleep(1)
        else:
            raise RuntimeError("ISOLATED_DATABASE_NOT_READY")
        roles = run(
            [
                "docker",
                "exec",
                "ledgerbridge-postgres-1",
                "pg_dumpall",
                "-U",
                "ledgerbridge",
                "--roles-only",
                "--no-role-passwords",
            ]
        )
        roles = b"\n".join(line for line in roles.splitlines() if line != b"CREATE ROLE postgres;")
        run(
            ["docker", "exec", "-i", name, "psql", "-X", "-U", "postgres", "-v", "ON_ERROR_STOP=1"],
            data=roles,
        )
        run(["docker", "exec", name, "createdb", "-U", "postgres", "ledgerbridge"])
        dump = run(
            [
                "docker",
                "exec",
                "ledgerbridge-postgres-1",
                "pg_dump",
                "-U",
                "ledgerbridge",
                "-Fc",
                "ledgerbridge",
            ]
        )
        run(
            [
                "docker",
                "exec",
                "-i",
                name,
                "pg_restore",
                "-U",
                "postgres",
                "-d",
                "ledgerbridge",
                "--exit-on-error",
            ],
            data=dump,
        )
        del dump, roles
        code = r"""
import hashlib,json,os
from datetime import date
from uuid import UUID,uuid4
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from sqlalchemy import create_engine,text,URL
from scripts.correct_reviewed_company_expenses import read_plan,apply_correction
engine=create_engine(URL.create('postgresql+psycopg',username='postgres',password=os.environ['TEST_DB_PASSWORD'],host='127.0.0.1',database='ledgerbridge'),hide_parameters=True)
plans=read_plan(Path('/work/monthly-reviewed-expenses-20260906.json'),os.environ['PLAN_SHA256'])
def snapshot(c):
    return c.execute(text('''SELECT (SELECT count(*) FROM journal_entry),
        (SELECT count(*) FROM posting),
        (SELECT md5(string_agg(to_jsonb(t)::text,',' ORDER BY transaction_ref))
         FROM bank_statement_transaction t)''')).one()
with engine.begin() as c:
    before=snapshot(c)
    c.execute(text('SET LOCAL ROLE ledgerbridge_owner'))
    # Preflight writes roll back: the original classification must remain current.
    with c.begin_nested() as save:
        assert sum(apply_correction(c,p) for p in plans)==len(plans)
        save.rollback()
    assert snapshot(c)==before
    # Exact source mismatch, stale revision and runtime role cannot write.
    for change in ({'amount_minor':plans[0].amount_minor-1},{'expected_revision':999},
                   {'managed_account_ref':UUID(int=999)}, {'occurred_on':date(2000,1,1)},
                   {'item':'NONEXISTENT_REPORTING_ITEM'}):
        with c.begin_nested() as save:
            try: apply_correction(c,plans[0].model_copy(update=change))
            except ValueError: pass
            else: raise AssertionError('expected refusal')
            save.rollback()
    # A later rejection must roll back earlier corrections in the same batch.
    try:
        with c.begin_nested():
            assert apply_correction(c,plans[0])
            apply_correction(c,plans[1].model_copy(update={'amount_minor':-1}))
    except ValueError: pass
    else: raise AssertionError('expected batch refusal')
    assert sum(apply_correction(c,p) for p in plans)==len(plans)
    assert sum(apply_correction(c,p) for p in plans)==0
    try: apply_correction(c,plans[0].model_copy(update={'reason':'different reviewed command'}))
    except ValueError: pass
    else: raise AssertionError('conflicting replay accepted')
    assert snapshot(c)==before
    c.execute(text('RESET ROLE'))
    c.execute(text('SET LOCAL ROLE ledgerbridge_reader'))
    try: apply_correction(c,plans[0])
    except ValueError: pass
    else: raise AssertionError('runtime role accepted')
def concurrent_correction(_):
    plan=plans[0].model_copy(update={'expected_revision':plans[0].expected_revision+1,
        'expected_category':plans[0].category,'expected_item':plans[0].item,
        'category':plans[0].expected_category,'item':plans[0].expected_item,'operation_id':uuid4()})
    try:
        with engine.begin() as c:
            c.execute(text('SET LOCAL ROLE ledgerbridge_owner'))
            return apply_correction(c,plan)
    except ValueError: return False
with ThreadPoolExecutor(max_workers=2) as pool:
    assert sum(pool.map(concurrent_correction,range(2)))==1
with engine.connect() as c: assert snapshot(c)==before
print(json.dumps({'status':'ISOLATED_PASSED','corrections':len(plans),'replay_delta':0,'source_and_postings_unchanged':True,'mid_batch_rollback':True,'concurrent_winners':1}))
"""
        os.environ["PLAN_SHA256"] = args.plan_sha256
        wrapped = (
            "import traceback,json\ntry:\n exec("
            + repr(code)
            + ")\nexcept Exception as e:\n print(json.dumps({'diagnostic':type(e).__name__,"
            "'sqlstate':getattr(getattr(e,'orig',None),'sqlstate',None),"
            "'frames':[(f.name,f.lineno) for f in traceback.extract_tb(e.__traceback__)]}))"
            "\n raise SystemExit(1)\n"
        )
        output = run(
            [
                "docker",
                "run",
                "--rm",
                "-i",
                "--read-only",
                "--user",
                "0:0",
                "--network",
                "container:" + name,
                "-e",
                "TEST_DB_PASSWORD",
                "-e",
                "PLAN_SHA256",
                "-v",
                f"{stage}:/work:ro",
                "-v",
                f"{stage}/correct_reviewed_company_expenses.py:/app/scripts/correct_reviewed_company_expenses.py:ro",
                "-w",
                "/app",
                app,
                "python",
                "-",
            ],
            data=wrapped.encode(),
        )
        print(output.decode().strip())
    except (RuntimeError, ValueError, OSError):
        print("ISOLATED_VERIFICATION_FAILED")
        return 2
    finally:
        if created:
            run(["docker", "rm", "-f", "-v", name])
        os.environ.pop("POSTGRES_PASSWORD", None)
        os.environ.pop("TEST_DB_PASSWORD", None)
        os.environ.pop("PLAN_SHA256", None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
