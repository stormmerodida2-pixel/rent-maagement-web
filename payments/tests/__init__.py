"""Tests for payments. The helpers here are shared with other apps' tests."""

from ..models import MpesaAccount

MPESA_SETTINGS = {'CALLBACK_BASE_URL': 'https://example.ngrok.app'}


def make_account(owner, **kwargs):
    fields = {
        'shortcode': '600986', 'consumer_key': f'key-{owner.username}',
        'consumer_secret': f'secret-{owner.username}', 'passkey': 'passkey',
        **kwargs,
    }
    return MpesaAccount.objects.create(owner=owner, **fields)


def mock_daraja(req, post_json, status=200):
    req.RequestException = Exception
    req.get.return_value.status_code = 200
    req.get.return_value.json.return_value = {'access_token': 'T', 'expires_in': '3599'}
    req.post.return_value.status_code = status
    req.post.return_value.json.return_value = post_json


def success_callback(checkout_id, amount=15000, receipt='SJK1ABC2DE'):
    return {'Body': {'stkCallback': {
        'MerchantRequestID': 'm-1',
        'CheckoutRequestID': checkout_id,
        'ResultCode': 0,
        'ResultDesc': 'The service request is processed successfully.',
        'CallbackMetadata': {'Item': [
            {'Name': 'Amount', 'Value': amount},
            {'Name': 'MpesaReceiptNumber', 'Value': receipt},
            {'Name': 'Balance'},
            {'Name': 'TransactionDate', 'Value': 20261013143015},
            {'Name': 'PhoneNumber', 'Value': 254712345678},
        ]},
    }}}


def c2b_body(trans_id='SJK9XYZ123', amount='7500.00', bill_ref='', msisdn='254712345678'):
    return {
        'TransactionType': 'Pay Bill',
        'TransID': trans_id,
        'TransTime': '20261013091500',
        'TransAmount': amount,
        'BusinessShortCode': '600986',
        'BillRefNumber': bill_ref,
        'InvoiceNumber': '',
        'OrgAccountBalance': '',
        'ThirdPartyTransID': '',
        'MSISDN': msisdn,
        'FirstName': 'Jane',
        'MiddleName': '',
        'LastName': 'Doe',
    }
