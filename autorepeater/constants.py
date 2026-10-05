"""Constants used by the autorepeater."""
from decimal import getcontext

IMPORTANT = 25

# Устанавливаем точность для Decimal
getcontext().prec = 28
