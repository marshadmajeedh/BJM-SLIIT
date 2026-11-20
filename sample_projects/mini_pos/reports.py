
from billing import checkout


def generate_sales_report(price, quantity, customer_type):
    amount = checkout(price, quantity, customer_type)
    return {"total_sales": amount}
