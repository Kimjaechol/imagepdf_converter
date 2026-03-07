"""Credit system – tracks per-page usage and manages user credit balances.

Business model:
- Operator sets their API keys (Gemini, Upstage) server-side.
- Users pre-purchase credits (denominated in USD) within the app.
- Each conversion debits the user's balance at a fixed per-page rate.

Per-page pricing (user-facing):
  - Image PDF (scanned): $0.02 / page
  - Digital PDF (text-based): $0.005 / page
  - Other documents (DOCX, HWPX, XLSX, PPTX): free
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from threading import Lock

logger = logging.getLogger(__name__)

# Fixed per-page pricing for users (USD)
PRICE_IMAGE_PDF_PER_PAGE = 0.02
PRICE_DIGITAL_PDF_PER_PAGE = 0.005
PRICE_OTHER_PER_PAGE = 0.0


@dataclass
class UserAccount:
    user_id: str = ""
    balance_usd: float = 0.0
    total_purchased_usd: float = 0.0
    total_consumed_usd: float = 0.0
    usage_history: list[dict] = field(default_factory=list)


class CreditService:
    """Manages user credit balances and per-page cost metering."""

    def __init__(self, data_dir: str = "data"):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._accounts: dict[str, UserAccount] = {}
        self._load_accounts()

    # ------------------------------------------------------------------
    # Account management
    # ------------------------------------------------------------------

    def get_or_create_account(self, user_id: str) -> UserAccount:
        with self._lock:
            if user_id not in self._accounts:
                self._accounts[user_id] = UserAccount(user_id=user_id)
                self._persist()
            return self._accounts[user_id]

    def get_balance(self, user_id: str) -> float:
        return self.get_or_create_account(user_id).balance_usd

    def purchase_credits(self, user_id: str, amount_usd: float) -> float:
        """Add credits to a user's balance. Returns new balance."""
        with self._lock:
            acct = self._accounts.setdefault(
                user_id, UserAccount(user_id=user_id)
            )
            acct.balance_usd += amount_usd
            acct.total_purchased_usd += amount_usd
            acct.usage_history.append({
                "type": "purchase",
                "amount_usd": amount_usd,
                "balance_after": acct.balance_usd,
                "timestamp": time.time(),
            })
            self._persist()
            return acct.balance_usd

    # ------------------------------------------------------------------
    # Per-page pricing helpers
    # ------------------------------------------------------------------

    @staticmethod
    def price_per_page(doc_type: str) -> float:
        """Return the per-page price for a given document type.

        doc_type: "image_pdf" | "digital_pdf" | "other"
        """
        if doc_type == "image_pdf":
            return PRICE_IMAGE_PDF_PER_PAGE
        elif doc_type == "digital_pdf":
            return PRICE_DIGITAL_PDF_PER_PAGE
        return PRICE_OTHER_PER_PAGE

    # ------------------------------------------------------------------
    # Usage metering
    # ------------------------------------------------------------------

    def check_sufficient_balance(
        self, user_id: str, num_pages: int, doc_type: str = "image_pdf"
    ) -> bool:
        """Check if user has enough credits for a conversion."""
        cost = num_pages * self.price_per_page(doc_type)
        return self.get_balance(user_id) >= cost

    def debit_usage(
        self,
        user_id: str,
        num_pages: int,
        doc_type: str = "image_pdf",
        description: str = "",
    ) -> dict:
        """Debit a user's balance based on page count and document type.

        Returns a dict with cost details (no raw API cost exposed).
        """
        per_page = self.price_per_page(doc_type)
        charged = num_pages * per_page

        with self._lock:
            acct = self._accounts.setdefault(
                user_id, UserAccount(user_id=user_id)
            )
            acct.balance_usd -= charged
            acct.total_consumed_usd += charged
            record = {
                "type": "usage",
                "num_pages": num_pages,
                "doc_type": doc_type,
                "per_page_usd": per_page,
                "charged_usd": round(charged, 6),
                "balance_after": round(acct.balance_usd, 6),
                "description": description,
                "timestamp": time.time(),
            }
            acct.usage_history.append(record)
            self._persist()
            return record

    def estimate_cost(self, num_pages: int, doc_type: str = "image_pdf") -> dict:
        """Estimate the user-facing cost for converting *num_pages* pages."""
        per_page = self.price_per_page(doc_type)
        charged = num_pages * per_page
        return {
            "num_pages": num_pages,
            "doc_type": doc_type,
            "per_page_usd": per_page,
            "charged_usd": round(charged, 6),
        }

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _persist(self) -> None:
        path = self.data_dir / "credit_accounts.json"
        data = {
            uid: asdict(acct) for uid, acct in self._accounts.items()
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def _load_accounts(self) -> None:
        path = self.data_dir / "credit_accounts.json"
        if not path.exists():
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            for uid, info in data.items():
                self._accounts[uid] = UserAccount(
                    user_id=uid,
                    balance_usd=info.get("balance_usd", 0.0),
                    total_purchased_usd=info.get("total_purchased_usd", 0.0),
                    total_consumed_usd=info.get("total_consumed_usd", 0.0),
                    usage_history=info.get("usage_history", []),
                )
        except Exception as exc:
            logger.error("Failed to load credit accounts: %s", exc)
