"""Constants used by the autorepeater."""
from decimal import getcontext

THRESHOLD = '0.004'
IMPORTANT = 25

# Устанавливаем точность для Decimal
getcontext().prec = 28
