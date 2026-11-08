from __future__ import annotations

from remasol.solana_service import SolanaService


class RecordingClient:
    def __init__(self):
        self.search_transaction_history = None

    def get_signature_statuses(self, signatures, *, search_transaction_history=False):
        assert signatures == ["parsed-signature"]
        self.search_transaction_history = search_transaction_history
        return {
            "result": {
                "value": [
                    {
                        "err": None,
                        "confirmationStatus": "finalized",
                    }
                ]
            }
        }


class SignatureParser:
    @staticmethod
    def from_string(signature):
        assert signature == "saved-signature"
        return "parsed-signature"


def test_signature_status_searches_history_for_saved_transactions():
    service = SolanaService.__new__(SolanaService)
    service._imports_error = None
    service.client = RecordingClient()
    service.Signature = SignatureParser

    assert service.signature_status("saved-signature") == "finalized"
    assert service.client.search_transaction_history is True
