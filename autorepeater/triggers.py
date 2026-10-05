"""Trigger checks for operations stream events."""


def check_triggers(event, src_account, dst_account):
    """check triggers for sync accounts"""
    # Проверяем, что все ценные бумаги разблокированы
    all_securities_unblocked = (
        event.has_position and
        event.account_id == src_account and
        len(event.securities) > 0 and
        all(sec.blocked == 0 for sec in event.securities)
    )

    destination_unblocked = (
        event.has_position and
        event.account_id == dst_account and
        bool(event.securities or event.money) and
        all(sec.blocked == 0 for sec in event.securities) and
        all(money.blocked_value == 0 for money in event.money)
    )

    return all_securities_unblocked or destination_unblocked
