from __future__ import annotations

from pgdevkit.db.complex_types import ComplexHelper


def test_complex_types_cache_is_per_instance_not_shared():
    # Regression: complex_types used to be a mutable class attribute, so a
    # CompositeInfo/EnumInfo (which carries OIDs from one specific
    # connection/database) fetched by one ComplexHelper would leak into any
    # other ComplexHelper that happens to look up the same type NAME —
    # exactly what happens across pgdevkit's per-worktree isolated test
    # databases, which legitimately reuse type names like "locale_labels"
    # with different OIDs per database.
    helper_a = ComplexHelper(con=None)  # con is unused by this path
    helper_b = ComplexHelper(con=None)

    helper_a.complex_types[("app", "dimensions")] = object()  # type: ignore[assignment]

    assert helper_b.complex_types == {}
    assert helper_a.complex_types is not helper_b.complex_types
