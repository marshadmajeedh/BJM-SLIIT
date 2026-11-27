
from customer_service import process_customer_refund


def display_refund(total, customer_type):
    return {
        "refund_amount": process_customer_refund(
            total, customer_type
        )
    }
