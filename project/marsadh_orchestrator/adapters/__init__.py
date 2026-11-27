"""Agent integration adapters for ChangeGuard AI Module 02."""

from .codebase_adapter import CodebaseAdapter
from .patch_adapter import (
    DEFAULT_OFFLINE_PATCH_FIXTURE_LABEL,
    DeterministicPremiumDiscountPatchFixture,
    PatchGenerationAdapter,
    PatchGeneratorProvider,
)
from .testing_adapter import TestingAdapter
from .verification_adapter import VerificationAdapter, build_approved_test_specs

__all__ = [
    "CodebaseAdapter",
    "DEFAULT_OFFLINE_PATCH_FIXTURE_LABEL",
    "DeterministicPremiumDiscountPatchFixture",
    "PatchGenerationAdapter",
    "PatchGeneratorProvider",
    "TestingAdapter",
    "VerificationAdapter",
    "build_approved_test_specs",
]
