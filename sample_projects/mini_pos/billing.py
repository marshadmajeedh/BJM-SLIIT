
def calculate_total(price, quantity):
    return price * quantity


def apply_discount(total, customer_type):
    if customer_type == "premium":
        return total * 0.90
    return total


def checkout(price, quantity, customer_type):
    total = calculate_total(price, quantity)
    return apply_discount(total, customer_type)
