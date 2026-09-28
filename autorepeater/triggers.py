"""Trigger checks for operations stream events."""


def check_triggers(position, src_account, dst_account):
    """check triggers for sync accounts"""
    # Проверяем, что все ценные бумаги разблокированы
    all_securities_unblocked = (
        position is not None and
        position.account_id == src_account and
        len(position.securities) > 0 and
        all(sec.blocked == 0 for sec in position.securities)
    )

    # Проверяем, что нет ценных бумаг и деньги разблокированы
    no_securities_and_money_unblocked = (
        position is not None and
        position.account_id == dst_account and
        len(position.securities) == 0 and
        len(position.money) > 0 and
        position.money[0].blocked_value.units == 0 and
        position.money[0].blocked_value.nano == 0
    )

    return all_securities_unblocked or no_securities_and_money_unblocked
