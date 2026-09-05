"""The shared horizon cache, at the edges its two callers do not reach.

Both adopters pin invalidation through their own service. What is only visible
here is the bound: the cache must not grow with the number of principals that
ever asked, because it lives at module scope for the life of the process.
"""

from __future__ import annotations

from uuid import UUID

from ledgerbridge.horizon_cache import HorizonCache, horizon_cache_key
from ledgerbridge.internal_read_contract import Capability, EntityGrant, WorkloadPrincipal

ENTITY = UUID("10000000-0000-4000-8000-000000000001")
UNIT = UUID("11000000-0000-4000-8000-00000000000a")
HASH = b"h" * 32


def _principal(
    *,
    principal_ref: str = "workload:cache-test",
    policy_generation: int = 1,
    unit_ref: str = "unit-a",
) -> WorkloadPrincipal:
    return WorkloadPrincipal(
        principal_ref=principal_ref,
        san_uri="spiffe://ledgerbridge.test/cache-test",
        policy_generation=policy_generation,
        capabilities=frozenset({Capability.CANDIDATE_READ}),
        grants=(
            EntityGrant(
                entity_ref=ENTITY,
                business_unit_refs=frozenset({unit_ref}),
                business_unit_ids=frozenset({UNIT}),
                business_unit_bindings=((unit_ref, UNIT),),
            ),
        ),
    )


def test_a_value_is_returned_only_at_the_horizon_it_was_stored_at() -> None:
    cache: HorizonCache[str] = HorizonCache()
    cache.store("k", 7, HASH, "built at 7")

    assert cache.get("k", 7, HASH) == "built at 7"
    assert cache.get("k", 8, HASH) is None


def test_a_rewritten_horizon_at_the_same_sequence_invalidates() -> None:
    # The sequence alone is not the identity of a horizon; a restored or
    # rewritten history could reuse it over different facts.
    cache: HorizonCache[str] = HorizonCache()
    cache.store("k", 7, HASH, "built at 7")

    assert cache.get("k", 7, b"i" * 32) is None


def test_each_thing_that_changes_what_a_principal_may_read_changes_the_key() -> None:
    base = horizon_cache_key(_principal())

    assert horizon_cache_key(_principal()) == base
    assert horizon_cache_key(_principal(principal_ref="workload:other")) != base
    assert horizon_cache_key(_principal(policy_generation=2)) != base
    assert horizon_cache_key(_principal(unit_ref="unit-b")) != base


def test_the_cache_stays_bounded_as_new_principals_arrive() -> None:
    cache: HorizonCache[int] = HorizonCache(max_principals=4)
    for index in range(9):
        cache.store(f"k{index}", 7, HASH, index)

    live = sum(1 for index in range(9) if cache.get(f"k{index}", 7, HASH) is not None)
    assert live <= 4, "an unbounded cache would hold every principal it ever saw"
    assert cache.get("k8", 7, HASH) == 8, "the newest entry survives"


def test_clearing_drops_every_entry() -> None:
    cache: HorizonCache[str] = HorizonCache()
    cache.store("a", 7, HASH, "one")
    cache.store("b", 7, HASH, "two")

    cache.clear()

    assert cache.get("a", 7, HASH) is None
    assert cache.get("b", 7, HASH) is None
