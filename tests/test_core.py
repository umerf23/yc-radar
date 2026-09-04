"""
Regression tests for the core state and source-health behavior.

These cover batch normalisation, deduplication, official-register lookup,
failure-safe incremental windows, and X provider error classification.
"""

import pytest

from app.classifier import Classifier
from app.models import (
    STATUS_CONFIRMED_SPEEDRUN,
    STATUS_CONFIRMED_YC,
    STATUS_EARLY_SIGNAL,
    STATUS_LINKEDIN_COMPANY_SIGNAL,
    Candidate,
    canonical_batch,
    programme_from_batch,
)
from app.sources.linkedin import LinkedInSource
from app.sources.x_twitter import BATCH_PATTERN, XTwitterSource
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


def test_x_regex_rejects_invalid_p_batch():
    """The X pre-parser must agree with canonical_batch."""
    assert BATCH_PATTERN.search("We got into YC P26") is None
    assert BATCH_PATTERN.search("We got into YC F26") is not None


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


# ---------- source run state ----------


def test_failed_run_does_not_advance_last_success(store):
    """
    A provider failure must never become the next incremental cursor.
    """
    store.mark_run("x_twitter", 3)
    successful = store.last_run("x_twitter")

    assert successful is not None

    store.mark_run(
        "x_twitter",
        0,
        error="provider_credit_exhausted",
    )

    assert store.last_run("x_twitter") == successful

    health = store.source_health()["x_twitter"]
    assert health["status"] == "degraded"
    assert health["error"] == "provider_credit_exhausted"
    assert health["last_success_at"] == successful.isoformat()


def test_first_failed_run_has_no_success_cursor(store):
    store.mark_run(
        "x_twitter",
        0,
        error="provider_auth_failed",
    )

    assert store.last_run("x_twitter") is None
    health = store.source_health()["x_twitter"]
    assert health["status"] == "degraded"
    assert health["last_success_at"] is None


# ---------- X provider errors ----------


class _FakeResponse:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


def _bare_x_source():
    """
    Build only the attributes _request_with_retry needs.

    This avoids coupling the provider-error unit tests to Source.__init__
    configuration details.
    """
    source = object.__new__(XTwitterSource)
    source._last_request_at = 0.0
    source.last_error = None
    return source


def test_x_402_is_reported_as_credit_exhausted(monkeypatch):
    source = _bare_x_source()

    monkeypatch.setattr(
        "app.sources.x_twitter.requests.get",
        lambda *args, **kwargs: _FakeResponse(402),
    )

    result = source._request_with_retry({}, {}, "test query")

    assert result is None
    assert source.last_error == "provider_credit_exhausted"


def test_x_401_is_reported_as_auth_failure(monkeypatch):
    source = _bare_x_source()

    monkeypatch.setattr(
        "app.sources.x_twitter.requests.get",
        lambda *args, **kwargs: _FakeResponse(401),
    )

    result = source._request_with_retry({}, {}, "test query")

    assert result is None
    assert source.last_error == "provider_auth_failed"


def test_x_200_returns_payload(monkeypatch):
    source = _bare_x_source()
    payload = {"tweets": [{"id": "1"}]}

    monkeypatch.setattr(
        "app.sources.x_twitter.requests.get",
        lambda *args, **kwargs: _FakeResponse(200, payload),
    )

    result = source._request_with_retry({}, {}, "test query")

    assert result == payload
    assert source.last_error is None


def test_x_timeout_retries_then_succeeds(monkeypatch):
    """A transient read timeout should not degrade the source."""
    source = _bare_x_source()
    payload = {"tweets": [{"id": "1"}]}
    calls = {"count": 0}

    def fake_get(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise __import__("requests").Timeout("timed out")
        return _FakeResponse(200, payload)

    monkeypatch.setattr(
        "app.sources.x_twitter.requests.get",
        fake_get,
    )
    monkeypatch.setattr(
        "app.sources.x_twitter.time.sleep",
        lambda _seconds: None,
    )

    result = source._request_with_retry({}, {}, "test query")

    assert result == payload
    assert calls["count"] == 2
    assert source.last_error is None


def test_x_timeout_exhaustion_is_reported(monkeypatch):
    """Three consecutive timeouts should mark X degraded."""
    source = _bare_x_source()
    calls = {"count": 0}

    def fake_get(*args, **kwargs):
        calls["count"] += 1
        raise __import__("requests").Timeout("timed out")

    monkeypatch.setattr(
        "app.sources.x_twitter.requests.get",
        fake_get,
    )
    monkeypatch.setattr(
        "app.sources.x_twitter.time.sleep",
        lambda _seconds: None,
    )

    result = source._request_with_retry({}, {}, "test query")

    assert result is None
    assert calls["count"] == 3
    assert source.last_error == "provider_timeout"


# ---------- programme identification ----------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("YC F26", "yc"),
        ("Fall 2026", "yc"),
        ("Speedrun SR007", "speedrun"),
        ("SR008", "speedrun"),
        ("a16z Speedrun", "speedrun"),
        ("YC P26", ""),
        ("", ""),
    ],
)
def test_programme_from_batch(raw, expected):
    assert programme_from_batch(raw) == expected


# ---------- official matching hardening ----------


def test_generic_speedrun_matches_specific_official_cohort(store):
    store.record_official(
        "Acme AI",
        "Speedrun SR007",
        "https://example.com/acme",
    )

    match = store.official_match(
        "Acme AI",
        "Speedrun",
        programme="speedrun",
    )

    assert match is not None
    assert match["programme"] == "speedrun"
    assert match["match_type"] == "programme_name"


def test_specific_speedrun_wrong_cohort_does_not_match(store):
    store.record_official(
        "Acme AI",
        "Speedrun SR007",
        "https://example.com/acme",
    )

    assert (
        store.official_match(
            "Acme AI",
            "Speedrun SR008",
            programme="speedrun",
        )
        is None
    )


def test_generic_yc_matches_same_company_any_yc_batch(store):
    store.record_official(
        "Acme AI",
        "Fall 2026",
        "https://example.com/acme",
    )

    match = store.official_match(
        "Acme AI",
        "",
        programme="yc",
    )

    assert match is not None
    assert match["programme"] == "yc"


# ---------- classifier evidence hardening ----------


def _classifier_with_store(store):
    classifier = object.__new__(Classifier)
    classifier.store = store
    classifier.official_cutoff = None
    return classifier


def test_classifier_rejects_placeholder_company_name(store):
    classifier = _classifier_with_store(store)
    candidate = _candidate(
        "",
        post_text="Our company just got into YC F26.",
        batch="YC F26",
    )

    result = classifier._apply_verdict(
        candidate,
        {
            "company_name": "our company",
            "batch": "YC F26",
            "description": "",
            "reason": "announcement",
        },
        0.95,
    )

    assert result.extra["validation_error"] == "invalid_company_name"
    assert result.extra["register_checked"] is False


def test_classifier_rejects_invalid_yc_batch_in_post(store):
    classifier = _classifier_with_store(store)
    candidate = _candidate(
        "",
        post_text="Acme AI just got into YC P26.",
    )

    result = classifier._apply_verdict(
        candidate,
        {
            "company_name": "Acme AI",
            "batch": "",
            "description": "",
            "reason": "announcement",
        },
        0.95,
    )

    assert result.extra["validation_error"] == "invalid_yc_batch"
    assert result.extra["register_checked"] is False


def test_classifier_requires_programme_evidence(store):
    classifier = _classifier_with_store(store)
    candidate = _candidate(
        "",
        post_text="Acme AI is launching today.",
    )

    result = classifier._apply_verdict(
        candidate,
        {
            "company_name": "Acme AI",
            "batch": "",
            "description": "",
            "reason": "announcement",
        },
        0.95,
    )

    assert result.extra["validation_error"] == "missing_programme_evidence"


def test_classifier_confirms_official_yc_company(store):
    store.record_official(
        "Acme AI",
        "Fall 2026",
        "https://example.com/acme",
    )

    classifier = _classifier_with_store(store)
    candidate = _candidate(
        "",
        post_text="Acme AI was accepted into YC F26.",
        batch="YC F26",
    )

    result = classifier._apply_verdict(
        candidate,
        {
            "company_name": "Acme AI",
            "batch": "YC F26",
            "description": "",
            "reason": "announcement",
        },
        0.95,
    )

    assert result.status == STATUS_CONFIRMED_YC
    assert result.extra["already_listed"] is True
    assert result.extra["register_checked"] is True


def test_classifier_marks_unlisted_valid_yc_as_early(store):
    classifier = _classifier_with_store(store)
    candidate = _candidate(
        "",
        post_text="Acme AI was accepted into YC F26.",
        batch="YC F26",
    )

    result = classifier._apply_verdict(
        candidate,
        {
            "company_name": "Acme AI",
            "batch": "YC F26",
            "description": "",
            "reason": "announcement",
        },
        0.95,
    )

    assert result.status == STATUS_EARLY_SIGNAL
    assert result.extra["already_listed"] is False
    assert result.extra["register_checked"] is True


def test_classifier_confirms_generic_speedrun_against_cohort(store):
    store.record_official(
        "Acme AI",
        "Speedrun SR007",
        "https://example.com/acme",
    )

    classifier = _classifier_with_store(store)
    candidate = _candidate(
        "",
        post_text="Acme AI is joining a16z Speedrun.",
        batch="",
    )

    result = classifier._apply_verdict(
        candidate,
        {
            "company_name": "Acme AI",
            "batch": "Speedrun",
            "description": "",
            "reason": "announcement",
        },
        0.95,
    )

    assert result.status == STATUS_CONFIRMED_SPEEDRUN
    assert result.extra["already_listed"] is True

# ---------- same-cycle early-signal semantics ----------


def test_classifier_marks_same_cycle_official_yc_as_early(store):
    """
    An official entry first observed after the cycle cutoff must not suppress
    a founder signal collected during that same monitoring cycle.
    """
    classifier = _classifier_with_store(store)
    classifier.official_cutoff = "2000-01-01T00:00:00+00:00"

    store.record_official(
        "Acme AI",
        "Fall 2026",
        "https://example.com/acme",
    )

    candidate = _candidate(
        "",
        post_text="Acme AI was accepted into YC F26.",
        batch="YC F26",
    )

    result = classifier._apply_verdict(
        candidate,
        {
            "company_name": "Acme AI",
            "batch": "YC F26",
            "description": "",
            "reason": "announcement",
        },
        0.95,
    )

    assert result.status == STATUS_EARLY_SIGNAL
    assert result.extra["already_listed"] is False
    assert result.extra["register_checked"] is True
    assert result.extra["official_seen_same_cycle"] is True
    assert result.extra["official_first_seen_at"]
    assert result.extra["official_match"]["recorded_at"]


def test_classifier_confirms_company_known_before_cycle(store):
    """A pre-existing official entry must remain confirmed."""
    store.record_official(
        "Acme AI",
        "Fall 2026",
        "https://example.com/acme",
    )

    classifier = _classifier_with_store(store)
    classifier.official_cutoff = "9999-12-31T23:59:59+00:00"

    candidate = _candidate(
        "",
        post_text="Acme AI was accepted into YC F26.",
        batch="YC F26",
    )

    result = classifier._apply_verdict(
        candidate,
        {
            "company_name": "Acme AI",
            "batch": "YC F26",
            "description": "",
            "reason": "announcement",
        },
        0.95,
    )

    assert result.status == STATUS_CONFIRMED_YC
    assert result.extra["already_listed"] is True
    assert result.extra["register_checked"] is True
    assert result.extra["official_seen_same_cycle"] is False
    assert result.extra["official_match"]["recorded_at"]

# ---------- LinkedIn company-page detection ----------


def _bare_linkedin_source():
    """Construct helper-only LinkedInSource without external credentials."""
    return object.__new__(LinkedInSource)


def test_linkedin_company_url_normalizes_subpages_and_querystrings():
    source = _bare_linkedin_source()

    assert (
        source._canonical_company_url(
            "https://www.linkedin.com/company/acme-ai/about/?trk=foo"
        )
        == "https://www.linkedin.com/company/acme-ai/"
    )


def test_linkedin_company_url_rejects_posts_and_people():
    source = _bare_linkedin_source()

    assert source._canonical_company_url(
        "https://www.linkedin.com/posts/jane_example-activity-1"
    ) == ""
    assert source._canonical_company_url(
        "https://www.linkedin.com/in/jane-doe/"
    ) == ""


def test_linkedin_company_page_requires_direct_programme_evidence():
    source = _bare_linkedin_source()

    assert source._looks_like_company_page(
        "Acme AI (YC F26) | LinkedIn",
        "Acme AI builds developer tools.",
        "Acme AI",
    )
    assert source._looks_like_company_page(
        "Acme AI | LinkedIn",
        "Founder & CEO of Acme AI (YC F26).",
        "Acme AI",
    )
    assert source._looks_like_company_page(
        "Acme AI | LinkedIn",
        "Acme AI is backed by a16z Speedrun.",
        "Acme AI",
    )


def test_linkedin_company_page_rejects_related_company_noise():
    source = _bare_linkedin_source()

    assert not source._looks_like_company_page(
        "SoloTech Solutions, Inc. | LinkedIn",
        "Chromie (YC S26) is partnering with SoloTech Solutions, Inc.",
        "SoloTech Solutions, Inc.",
    )
    assert not source._looks_like_company_page(
        "Hyper | LinkedIn",
        "Callbook AI (YC S26). Technology, Information and Internet.",
        "Hyper",
    )
    assert not source._looks_like_company_page(
        "Synthrun | LinkedIn",
        "Today I got a notification from a16z Speedrun.",
        "Synthrun",
    )


def test_linkedin_company_page_rejects_invalid_yc_batch():
    source = _bare_linkedin_source()

    assert not source._looks_like_company_page(
        "Huscarl (YC P26) | LinkedIn",
        "Huscarl is connected to Y Combinator.",
        "Huscarl",
    )


def test_linkedin_company_name_from_search_title():
    source = _bare_linkedin_source()

    assert source._company_name_from_title(
        "Acme AI | LinkedIn"
    ) == "Acme AI"
    assert source._company_name_from_title(
        "Acme AI: Overview | LinkedIn"
    ) == "Acme AI"
    assert source._company_name_from_title(
        "Bullet (YC S26) | LinkedIn"
    ) == "Bullet"
    assert source._company_name_from_title(
        "Athena (a16z Speedrun SR007) | LinkedIn"
    ) == "Athena"


def test_linkedin_company_candidate_status_is_not_founder_early_signal():
    candidate = Candidate(
        company_name="Acme AI",
        source="linkedin",
        status=STATUS_LINKEDIN_COMPANY_SIGNAL,
        url="https://www.linkedin.com/company/acme-ai/",
        batch="YC F26",
    )

    assert candidate.status == STATUS_LINKEDIN_COMPANY_SIGNAL
    assert candidate.is_early_signal is False
