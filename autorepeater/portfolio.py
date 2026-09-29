"""Shared read-only access to account portfolios."""


def get_portfolio(client, account_id):
    """Load one portfolio without filtering positions or reporting it."""
    return client.operations.get_portfolio(account_id=account_id)
