def analyze_failure(evidence):

    if "AssertionError" in evidence:
        return {
            "error_type": "AssertionError",
            "reason": "Actual result differs from expected result",
            "risk": "medium",
            "recommendation": "Review business logic and test expectations"
        }

    elif "ModuleNotFoundError" in evidence:
        return {
            "error_type": "ModuleNotFoundError",
            "reason": "Required module is missing",
            "risk": "unknown",
            "recommendation": "Check project dependencies"
        }

    else:
        return {
            "error_type": "Unknown",
            "reason": "Further analysis required",
            "risk": "unknown",
            "recommendation": "Review failure evidence"
        }