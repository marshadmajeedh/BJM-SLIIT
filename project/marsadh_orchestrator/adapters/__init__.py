"""Agent integration adapters for ChangeGuard AI Module 02."""

from .codebase_adapter import CodebaseAdapter
from .patch_adapter import (
    DEFAULT_OFFLINE_PATCH_FIXTURE_LABEL,
    DeterministicPersistentRegressionPatchFixture,
    DeterministicPremiumDiscountPatchFixture,
    DeterministicRegressiveThenFixedPatchFixture,
    PatchGenerationAdapter,
    PatchGeneratorProvider,
)
from .review_adapter import (
    DEFAULT_OFFLINE_REVIEW_FIXTURE_LABEL,
    DeterministicIndependentReviewerFixture,
    IndependentReviewAdapter,
    IndependentReviewerProvider,
    build_known_artifact_registry,
    validate_review_evidence_references,
)
from .testing_adapter import TestingAdapter
from .verification_adapter import VerificationAdapter, build_approved_test_specs

__all__ = [
    "CodebaseAdapter",
    "DEFAULT_OFFLINE_PATCH_FIXTURE_LABEL",
    "DEFAULT_OFFLINE_REVIEW_FIXTURE_LABEL",
    "DeterministicIndependentReviewerFixture",
    "DeterministicPersistentRegressionPatchFixture",
    "DeterministicPremiumDiscountPatchFixture",
    "DeterministicRegressiveThenFixedPatchFixture",
    "IndependentReviewAdapter",
    "IndependentReviewerProvider",
    "PatchGenerationAdapter",
    "PatchGeneratorProvider",
    "TestingAdapter",
    "VerificationAdapter",
    "build_approved_test_specs",
    "build_known_artifact_registry",
    "validate_review_evidence_references",
]
