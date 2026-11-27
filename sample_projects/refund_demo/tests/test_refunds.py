from refunds import calculate_refund

def test_discounted_refund():
    assert calculate_refund(1000, 10) == 900