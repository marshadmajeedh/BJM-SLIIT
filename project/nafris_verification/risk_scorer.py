def calculate_risk(ai_analysis):

    if not isinstance(ai_analysis, dict):
        return {
            "score": None,
            "severity": "Unknown"
        }

    impact = str(
        ai_analysis.get("business_impact", "")
    ).lower()

    if any(word in impact for word in [
        "security breach", "data breach", "data loss"
    ]):
        score = 95
        severity = "Critical"

    elif any(word in impact for word in [
        "financial loss", "overpayment",
        "revenue loss", "excessive refund"
    ]):
        score = 80
        severity = "High"

    else:
        return {
            "score": None,
            "severity": "Unknown"
        }

    return {
        "score": score,
        "severity": severity,
        "method": "keyword_based"
    }