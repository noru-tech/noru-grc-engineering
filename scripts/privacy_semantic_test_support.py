"""Evidence-record fixtures for consistency tests, not a semantic classifier."""
def semantic_review(engine, evidence_ids, finding):
    return {key: {"finding": finding, "evidence_ids": list(evidence_ids)}
            for key in engine.SEMANTIC_REVIEW_QUESTIONS}
