from ledgerbridge.counterparty import (
    CounterpartyClass,
    CounterpartyMapping,
    CounterpartyRegistry,
)


def test_explicit_aliases_reuse_one_stable_counterparty_identity() -> None:
    registry = CounterpartyRegistry(
        (
            CounterpartyMapping(
                counterparty_ref="cp_supplier_one",
                counterparty_class=CounterpartyClass.KNOWN_BUSINESS,
                aliases=("合成供应商", " 合成　供应商 "),
                account_identifiers=("acct-001",),
            ),
        )
    )

    first = registry.resolve(label="合成供应商")
    second = registry.resolve(label="不同显示名", account_identifier="acct-001")

    assert first == second
    assert first is not None
    assert first.counterparty_class is CounterpartyClass.KNOWN_BUSINESS


def test_keywords_never_promote_an_unknown_counterparty() -> None:
    resolved = CounterpartyRegistry(()).resolve(label="某银行账户投资理财")

    assert resolved is not None
    assert resolved.counterparty_class is CounterpartyClass.UNKNOWN
    assert resolved.counterparty_ref.startswith("cp_")


def test_unknown_identity_is_stable_but_transactions_remain_separate() -> None:
    registry = CounterpartyRegistry(())

    assert registry.resolve(label=" 同一　对象 ") == registry.resolve(label="同一 对象")


def test_platform_internal_subaccount_is_not_an_external_counterparty() -> None:
    registry = CounterpartyRegistry(())

    assert registry.resolve(label="余额宝", platform_internal=True) is None
