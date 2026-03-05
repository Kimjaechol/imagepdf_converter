"""Credit system – tracks API token usage and manages user credit balances.

Business model:
- Operator sets their Gemini API key (server-side env or config).
- Users pre-purchase credits (denominated in USD) within the app.
- Each Gemini API call's token usage is metered.  The user's balance is
  debited at ``cost_markup`` times the raw API cost (default 2.2x).
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from threading import Lock

logger = logging.getLogger(__name__)

# Gemini 3.1 Flash-Lite pricing (USD per 1 million tokens)
_DEFAULT_INPUT_COST = 0.25
_DEFAULT_OUTPUT_COST = 1.50
_DEFAULT_MARKUP = 2.2


@dataclass
class TokenUsage:
    """Token counts for a single API call."""
    input_tokens: int = 0
    output_tokens: int = 0
    timestamp: float = 0.0

    @property
    def raw_cost_usd(self) -> float:
        return (
            self.input_tokens / 1_000_000 * _DEFAULT_INPUT_COST
            + self.output_tokens / 1_000_000 * _DEFAULT_OUTPUT_COST
        )


@dataclass
class UserAccount:
    user_id: str = ""
    balance_usd: float = 0.0
    total_purchased_usd: float = 0.0
    total_consumed_usd: float = 0.0
    usage_history: list[dict] = field(default_factory=list)


class CreditService:
    """Manages user credit balances and API cost metering."""

    def __init__(
        self,
        data_dir: str = "data",
        cost_markup: float = _DEFAULT_MARKUP,
        input_cost_per_million: float = _DEFAULT_INPUT_COST,
        output_cost_per_million: float = _DEFAULT_OUTPUT_COST,
    ):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.cost_markup = cost_markup
        self.input_cost = input_cost_per_million
        self.output_cost = output_cost_per_million
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
    # Usage metering
    # ------------------------------------------------------------------

    def check_sufficient_balance(self, user_id: str, estimated_pages: int) -> bool:
        """Check if user has enough credits for an estimated conversion."""
        # Rough estimate: ~7,900 tokens per page (input+output)
        estimated_input = estimated_pages * 5_500
        estimated_output = estimated_pages * 2_400
        raw_cost = (
            estimated_input / 1_000_000 * self.input_cost
            + estimated_output / 1_000_000 * self.output_cost
        )
        charged = raw_cost * self.cost_markup
        return self.get_balance(user_id) >= charged

    def debit_usage(
        self,
        user_id: str,
        input_tokens: int,
        output_tokens: int,
        description: str = "",
    ) -> dict:
        """Debit a user's balance based on actual token usage.

        Returns a dict with cost details.
        """
        raw_cost = (
            input_tokens / 1_000_000 * self.input_cost
            + output_tokens / 1_000_000 * self.output_cost
        )
        charged = raw_cost * self.cost_markup

        with self._lock:
            acct = self._accounts.setdefault(
                user_id, UserAccount(user_id=user_id)
            )
            acct.balance_usd -= charged
            acct.total_consumed_usd += charged
            record = {
                "type": "usage",
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "raw_cost_usd": round(raw_cost, 6),
                "charged_usd": round(charged, 6),
                "markup": self.cost_markup,
                "balance_after": round(acct.balance_usd, 6),
                "description": description,
                "timestamp": time.time(),
            }
            acct.usage_history.append(record)
            self._persist()
            return record

    def estimate_cost(self, num_pages: int) -> dict:
        """Estimate the user-facing cost for converting *num_pages* pages."""
        estimated_input = num_pages * 5_500
        estimated_output = num_pages * 2_400
        raw_cost = (
            estimated_input / 1_000_000 * self.input_cost
            + estimated_output / 1_000_000 * self.output_cost
        )
        charged = raw_cost * self.cost_markup
        return {
            "num_pages": num_pages,
            "estimated_input_tokens": estimated_input,
            "estimated_output_tokens": estimated_output,
            "raw_cost_usd": round(raw_cost, 6),
            "charged_usd": round(charged, 6),
            "markup": self.cost_markup,
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
