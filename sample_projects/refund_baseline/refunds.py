from billing import calculate_payment


def calculate_refund(price, discount):
    return calculate_payment(price, discount)
