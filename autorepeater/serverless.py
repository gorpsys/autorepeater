"""Yandex Cloud Functions entrypoint."""
import os

from autorepeater.logging_config import configure_yc_logging
from autorepeater.runner import Runner

DEFAULT_SRC_ACCOUNT = '2193248994'
DEFAULT_DST_ACCOUNT = '2141399550'


def get_query_params(event):
    """Return query string params from a Yandex Cloud event."""
    params = event.get('queryStringParameters') if event else None
    if params is None:
        return {}
    return params


def get_param(params, name, env_name, default=None):
    """Read a request parameter with environment fallback."""
    value = params.get(name)
    if value:
        return value
    value = os.environ.get(env_name)
    if value:
        return value
    return default


def handler(event, context):
    """Run one account sync from a serverless invocation."""
    del context
    configure_yc_logging()

    params = get_query_params(event)
    src = get_param(params, 'src', 'SRC_ACCOUNT', DEFAULT_SRC_ACCOUNT)
    dst = get_param(params, 'dst', 'DST_ACCOUNT', DEFAULT_DST_ACCOUNT)
    invest_token = get_param(params, 'token', 'INVEST_TOKEN')
    if invest_token is None:
        invest_token = os.environ['t_token']

    runner = Runner(
        token=invest_token,
        src=src,
        dst=dst)
    runner.run_sync()

    return {
        'statusCode': 200,
        'headers': {
            'Content-Type': 'text/plain',
        },
        'isBase64Encoded': False,
        'body': f'Success sync, {src} {dst}!',
    }
