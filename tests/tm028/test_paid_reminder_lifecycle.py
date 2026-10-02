from tm_api.v25 import finance

FMT = {
    "received_marker": "💶",
    "partial_separator": " + ",
    "thousands_suffix": "к",
    "decimal_separator": ",",
    "currency_suffix": " ₽",
}


def test_received_marker_is_partial_only():
    partial = {
        "work_amount": "40000.00",
        "received_net": "20000.00",
        "remaining": "20000.00",
        "payment_status": "partial",
    }
    paid = {
        "work_amount": "40000.00",
        "received_net": "40000.00",
        "remaining": "0.00",
        "payment_status": "paid",
    }
    assert finance.display(partial, FMT) == "💶20к + 20к"
    assert finance.display(paid, FMT) == "40к"
