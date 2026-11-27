
from billing import apply_discount


def calculate_refund(original_total, customer_type):
    return apply_discount(original_total, customer_type)
