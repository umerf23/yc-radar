import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.classifier import Classifier
from app.config import load_config
from app.models import STATUS_EARLY_SIGNAL, Candidate
from app.state import Store

config = load_config()
store = Store(config.db_path)
classifier = Classifier(config, store)

# Real posts from your own collector runs, plus the noise that fooled
# the keyword filters. Expected verdicts are in the comments.
SAMPLES = [
    # Genuine early signals.
    (
        "Andre Beukers",
        "Thrilled to share that Redoubt Insurance is joining "
        "Y Combinator's Fall 2026 batch! We're building a "
        "commercial insurance company that automates everything.",
    ),
    (
        "Amith Bysani",
        "Antropi Robotics is joining Y Combinator (F26). "
        "We build autonomous CNC factories. Most factories "
        "today still run on manual effort.",
    ),
    (
        "Kazi Farabi",
        "I'm thrilled to announce that I'm part of a16z "
        "speedrun SR007! Out of 30,000+ applicants, we were "
        "selected as one of only two space companies.",
    ),
    (
        "Arnav Bhalla",
        "I'm leaving college to build a startup focusing on "
        "robotics for hospitals. I'm still 18, and I just got "
        "backed by a16z speedrun.",
    ),
    # Third-party coverage.
    (
        "Edith Yeung",
        "Part of YC P26, InstaAgent is an AI-native marketing "
        "company that helps B2C companies turn a single "
        "campaign brief into hundreds of personalized social posts.",
    ),
    (
        "SheetVenture",
        "Part of YC's Winter 2025 batch, Mundo builds what it "
        "calls perceptual intelligence, datasets and evaluations "
        "across audio, video, and emerging modalities.",
    ),
    # Rejection.
    (
        "cdonoyan",
        "It's official: we didn't get into YC F26, our 2nd try. "
        "Third time's the charm!",
    ),
    # Already-announced company posting a product update.
    (
        "Ananth Sankaralingam",
        "Excited to announce that Chromie (YC S26) is partnering "
        "with SoloTech Solutions, Inc. and Founder & CEO Al Niles!",
    ),
    # Satire.
    (
        "bellicose_bestie",
        "Excited to announce I was accepted into YC P26 to build "
        "@trylightning. Where we use Indian visa geniuses as human "
        "lightning rods to collect better information about "
        "dangerous storms.",
    ),
]

candidates = [
    Candidate(
        company_name="",
        source="x_twitter",
        status=STATUS_EARLY_SIGNAL,
        url=f"https://example.com/{index}",
        founder_name=name,
        post_text=text,
    )
    for index, (name, text) in enumerate(SAMPLES)
]

print(f"Classifying {len(candidates)} sample posts...\n")

kept = classifier.classify_all(candidates)

print(f"\n{'=' * 70}")

for candidate in kept:
    print(
        f"\n{candidate.company_name or '(no name)'} "
        f"[{candidate.batch or 'no batch'}]"
    )
    print(f"  status:     {candidate.status}")
    print(f"  confidence: {candidate.confidence}")
    print(f"  founder:    {candidate.founder_name}")
    print(
        f"  reason:     "
        f"{candidate.extra.get('classifier_reason', '')}"
    )