"""Yandex Cloud Functions entrypoint."""
import os

from autorepeater.logging_config import configure_yc_logging
from autorepeater.reporting import print_launch
from autorepeater.runner import Runner
from autorepeater.strategies import prepare_strategy

DEFAULT_DST_ACCOUNT = '2141399550'
DEFAULT_ALGORITM = 'COMPOSITE'
DEFAULT_SRC = 'BALANCED'


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
    algoritm = (
        params['algoritm'] if 'algoritm' in params
        else os.environ.get('ALGORITM', DEFAULT_ALGORITM))
    default_src = DEFAULT_SRC if algoritm == DEFAULT_ALGORITM else None
    src = params['src'] if 'src' in params else os.environ.get('SRC_ACCOUNT', default_src)
    prepared = prepare_strategy(algoritm, src)
    dst = get_param(params, 'dst', 'DST_ACCOUNT', DEFAULT_DST_ACCOUNT)
    print_launch(algoritm, src, dst)
    invest_token = get_param(params, 'token', 'INVEST_TOKEN')
    if invest_token is None:
        invest_token = os.environ['t_token']

    runner = Runner(
        token=invest_token,
        prepared_strategy=prepared,
        dst=dst)
    runner.run_sync()

    return {
        'statusCode': 200,
        'headers': {
            'Content-Type': 'text/plain',
        },
        'isBase64Encoded': False,
        'body': f'Success sync, {prepared.source_display} {dst}!',
    }
