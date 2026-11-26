
from refunds import calculate_refund


def process_customer_refund(total, customer_type):
    return calculate_refund(total, customer_type)
