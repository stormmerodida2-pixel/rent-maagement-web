import re


def normalize_phone(phone):
    """Convert Kenyan numbers like 0712 345 678 / +254712345678 / 712345678 to 254712345678.

    Raises ValueError for anything that is not a Kenyan mobile number.
    """
    digits = re.sub(r'\D', '', phone or '')
    if digits.startswith('254'):
        digits = digits[3:]
    elif digits.startswith('0'):
        digits = digits[1:]
    if not re.fullmatch(r'[17]\d{8}', digits):
        raise ValueError(f'{phone!r} is not a valid Kenyan mobile number.')
    return '254' + digits
