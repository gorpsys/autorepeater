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

    # Проверяем, что нет ценных бумаг и деньги разблокированы
    no_securities_and_money_unblocked = (
        event.has_position and
        event.account_id == dst_account and
        len(event.securities) == 0 and
        len(event.money) > 0 and
        event.money[0].blocked_value == 0
    )

    return all_securities_unblocked or no_securities_and_money_unblocked
