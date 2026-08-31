"""
Regression tests for the three pieces of logic the whole bot rests on.

These are deliberately narrow. They cover batch normalisation, dedup key
stability, and the register lookup, because those are the parts where a
silent mistake produces a confident wrong answer rather than a crash.

The batch tests exist because of a real defect: the YC directory
publishes 'Fall 2026' while founders write 'YC F26', and comparing those
strings directly meant every already-listed company was reported as an
early signal.
"""

import pytest

from app.models import (
    STATUS_EARLY_SIGNAL,
    Candidate,
    canonical_batch,
)
from app.state import Store


@pytest.fixture
def store(tmp_path):
    """A throwaway database, rebuilt for every test."""
    return Store(tmp_path / "test.db")


# ---------- batch normalisation ----------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # The YC directory format and the three ways founders write it.
        ("Fall 2026", "ycf26"),
        ("YC F26", "ycf26"),
        ("F26", "ycf26"),
        ("YC Fall 2026", "ycf26"),

        # Spring is X, not S, because S was already Summer.
        ("Spring 2026", "ycx26"),
        ("YC X27", "ycx27"),
        ("Summer 2026", "ycs26"),
        ("Winter 2027", "ycw27"),

        # a16z Speedrun cohorts, padded so SR7 and SR007 agree.
        ("Speedrun SR007", "sr007"),
        ("SR007", "sr007"),
        ("a16z Speedrun SR008", "sr008"),

        # No cohort stated, so only the programme is known.
        ("a16z speedrun", "speedrun"),

        # Unrecognisable input must return empty, never a guess.
        ("YC P26", ""),
        ("Unspecified", ""),
        ("", ""),
    ],
)
def test_canonical_batch(raw, expected):
    assert canonical_batch(raw) == expected


def test_batch_variants_agree():
    """The specific failure this fix was written for."""
    directory_form = canonical_batch("Fall 2026")
    founder_form = canonical_batch("YC F26")

    assert directory_form == founder_form


# ---------- deduplication ----------


def _candidate(name: str, **overrides) -> Candidate:
    fields = {
        "company_name": name,
        "source": "x_twitter",
        "status": STATUS_EARLY_SIGNAL,
    }
    fields.update(overrides)

    return Candidate(**fields)


def test_dedup_key_ignores_punctuation_and_case():
    """'Acme AI' and 'acme.ai' are the same company."""
    assert _candidate("Acme AI").dedup_key == _candidate("acme.ai").dedup_key


def test_dedup_key_separates_distinct_companies():
    assert _candidate("Acme AI").dedup_key != _candidate(
        "Acme Robotics"
    ).dedup_key


def test_dedup_key_survives_source_differences():
    """Found on X first, then LinkedIn, is still one company."""
    on_x = _candidate("Acme AI", source="x_twitter")
    on_linkedin = _candidate("Acme AI", source="linkedin")

    assert on_x.dedup_key == on_linkedin.dedup_key


def test_filter_new_suppresses_repeats(store):
    first_run = store.filter_new([_candidate("Acme AI")])

    assert len(first_run) == 1

    store.record(first_run[0])

    second_run = store.filter_new([_candidate("Acme AI")])

    assert second_run == []


def test_filter_new_deduplicates_within_one_run(store):
    """The same company often appears in several queries per run."""
    fresh = store.filter_new(
        [_candidate("Acme AI"), _candidate("acme.ai")]
    )

    assert len(fresh) == 1


# ---------- official register ----------


def test_register_matches_across_batch_spellings(store):
    store.record_official(
        "Lightfield",
        "Fall 2026",
        "https://example.com",
    )

    assert store.is_officially_listed("Lightfield", "YC F26")
    assert store.is_officially_listed("Lightfield", "Fall 2026")
    assert store.is_officially_listed("Lightfield", "F26")


def test_register_rejects_wrong_batch(store):
    """Reused names across years must stay distinct."""
    store.record_official(
        "Remix",
        "Summer 2015",
        "https://example.com",
    )

    assert not store.is_officially_listed("Remix", "YC W27")


def test_register_rejects_unknown_company(store):
    store.record_official(
        "Lightfield",
        "Fall 2026",
        "https://example.com",
    )

    assert not store.is_officially_listed(
        "NotARealCompany",
        "YC F26",
    )


def test_unrecognised_batch_falls_back_to_name_only(store):
    """
    An unparseable batch label must not silently query a key that
    cannot exist. It falls back to matching the name against any batch.
    """
    store.record_official(
        "Lightfield",
        "Fall 2026",
        "https://example.com",
    )

    assert store.is_officially_listed("Lightfield", "YC P26")
