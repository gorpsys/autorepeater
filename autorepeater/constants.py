"""Constants used by the autorepeater."""
from decimal import getcontext

DST_MONEY_RESERVED = '0.01'
THRESHOLD = '0.004'
IMPORTANT = 25

# Устанавливаем точность для Decimal
getcontext().prec = 28
