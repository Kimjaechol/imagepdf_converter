"""Payment service – Stripe (international) + Toss Payments (Korea).

Stripe: card payments for international users.
Toss Payments: 카카오페이, 네이버페이, 카드, 계좌이체 등 한국 간편결제.

Both gateways create checkout sessions and handle webhooks to confirm payments
and credit the user's balance.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
import base64
from dataclasses import asdict, dataclass, field
from pathlib import Path
from threading import Lock
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Payment record persistence
# ---------------------------------------------------------------------------

@dataclass
class PaymentRecord:
    payment_id: str  # internal ID
    gateway: str  # "stripe" | "toss"
    gateway_payment_id: str  # Stripe session_id or Toss paymentKey
    user_id: str
    amount_usd: float
    amount_krw: int = 0
    currency: str = "usd"
    status: str = "pending"  # pending | completed | failed | cancelled
    method: str = ""  # card, kakaopay, naverpay, etc.
    created_at: float = 0.0
    completed_at: float = 0.0
    metadata: dict = field(default_factory=dict)


class PaymentService:
    """Manages payment sessions for Stripe and Toss Payments."""

    def __init__(self, data_dir: str = "data"):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._payments: dict[str, PaymentRecord] = {}
        self._load()

    # ------------------------------------------------------------------
    # Stripe
    # ------------------------------------------------------------------

    def create_stripe_checkout(
        self,
        user_id: str,
        amount_usd: float,
        success_url: str | None = None,
        cancel_url: str | None = None,
    ) -> dict:
        """Create a Stripe Checkout Session.

        Supports card payments. Korean sole-proprietor can receive payouts
        to Korean bank account via Stripe Atlas / Stripe Connect.
        """
        import stripe

        stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")
        if not stripe.api_key:
            raise RuntimeError("STRIPE_SECRET_KEY not configured")

        payment_id = f"pay_{int(time.time())}_{user_id[:8]}"

        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[{
                "price_data": {
                    "currency": "usd",
                    "product_data": {
                        "name": "MoA 문서 변환기 크레딧",
                        "description": f"${amount_usd:.2f} 크레딧 충전",
                    },
                    "unit_amount": int(amount_usd * 100),
                },
                "quantity": 1,
            }],
            mode="payment",
            success_url=success_url or os.environ.get(
                "STRIPE_SUCCESS_URL",
                "http://localhost:3000/payment/success?session_id={CHECKOUT_SESSION_ID}",
            ),
            cancel_url=cancel_url or os.environ.get(
                "STRIPE_CANCEL_URL",
                "http://localhost:3000/payment/cancel",
            ),
            metadata={
                "user_id": user_id,
                "amount_usd": str(amount_usd),
                "internal_payment_id": payment_id,
            },
            customer_email=None,  # Stripe Checkout will collect email
        )

        record = PaymentRecord(
            payment_id=payment_id,
            gateway="stripe",
            gateway_payment_id=session.id,
            user_id=user_id,
            amount_usd=amount_usd,
            currency="usd",
            status="pending",
            method="card",
            created_at=time.time(),
        )
        self._save_record(record)

        return {
            "payment_id": payment_id,
            "gateway": "stripe",
            "checkout_url": session.url,
            "session_id": session.id,
        }

    def verify_stripe_webhook(self, payload: bytes, sig_header: str) -> dict | None:
        """Verify Stripe webhook signature and return event dict."""
        import stripe

        stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")
        webhook_secret = os.environ.get("STRIPE_WEBHOOK_SECRET", "")

        if not webhook_secret:
            logger.warning("STRIPE_WEBHOOK_SECRET not set, skipping signature verification")
            return json.loads(payload)

        try:
            event = stripe.Webhook.construct_event(payload, sig_header, webhook_secret)
            return event
        except (stripe.SignatureVerificationError, ValueError) as e:
            logger.error("Stripe webhook verification failed: %s", e)
            return None

    def handle_stripe_event(self, event: dict) -> dict | None:
        """Process a verified Stripe event. Returns credit info if payment completed."""
        event_type = event.get("type", "")

        if event_type == "checkout.session.completed":
            session = event.get("data", {}).get("object", {})
            metadata = session.get("metadata", {})
            user_id = metadata.get("user_id")
            amount_usd = float(metadata.get("amount_usd", "0"))
            payment_id = metadata.get("internal_payment_id", "")

            if user_id and amount_usd > 0:
                # Update payment record
                if payment_id and payment_id in self._payments:
                    with self._lock:
                        self._payments[payment_id].status = "completed"
                        self._payments[payment_id].completed_at = time.time()
                        self._persist()

                return {
                    "user_id": user_id,
                    "amount_usd": amount_usd,
                    "gateway": "stripe",
                    "payment_id": payment_id,
                }

        return None

    # ------------------------------------------------------------------
    # Toss Payments (토스페이먼츠)
    # ------------------------------------------------------------------

    def create_toss_payment(
        self,
        user_id: str,
        amount_krw: int,
        order_name: str = "MoA 문서 변환기 크레딧",
        method: str = "",  # empty = show all methods
        success_url: str | None = None,
        fail_url: str | None = None,
    ) -> dict:
        """Create a Toss Payments checkout session.

        Supports: 카드, 카카오페이, 네이버페이, 토스페이, 계좌이체, 가상계좌, 휴대폰
        """
        client_key = os.environ.get("TOSS_CLIENT_KEY", "")
        secret_key = os.environ.get("TOSS_SECRET_KEY", "")

        if not client_key or not secret_key:
            raise RuntimeError("Toss Payments keys not configured")

        payment_id = f"toss_{int(time.time())}_{user_id[:8]}"

        # KRW -> USD conversion using auto-updated exchange rate
        # ExchangeRateService keeps KRW_USD_RATE env var updated automatically
        exchange_rate = float(os.environ.get("KRW_USD_RATE", "1350"))
        amount_usd = round(amount_krw / exchange_rate, 2)

        # Determine available payment methods based on request
        # Toss Payments method values: 카드, 가상계좌, 간편결제, 계좌이체, 휴대폰
        toss_method = method if method else None

        # Build the payment request body
        body: dict[str, Any] = {
            "amount": amount_krw,
            "orderId": payment_id,
            "orderName": order_name,
            "successUrl": success_url or os.environ.get(
                "TOSS_SUCCESS_URL",
                "http://localhost:3000/payment/toss/success",
            ),
            "failUrl": fail_url or os.environ.get(
                "TOSS_FAIL_URL",
                "http://localhost:3000/payment/toss/fail",
            ),
        }

        if toss_method:
            body["method"] = toss_method

        # Save record
        record = PaymentRecord(
            payment_id=payment_id,
            gateway="toss",
            gateway_payment_id="",  # filled after confirmation
            user_id=user_id,
            amount_usd=amount_usd,
            amount_krw=amount_krw,
            currency="krw",
            status="pending",
            method=method or "all",
            created_at=time.time(),
            metadata={"order_name": order_name},
        )
        self._save_record(record)

        # Return info for frontend to initialize Toss Payments widget/SDK
        return {
            "payment_id": payment_id,
            "gateway": "toss",
            "client_key": client_key,
            "amount": amount_krw,
            "order_id": payment_id,
            "order_name": order_name,
            "success_url": body["successUrl"],
            "fail_url": body["failUrl"],
            "method": method,
            "amount_usd_equivalent": amount_usd,
        }

    def confirm_toss_payment(
        self,
        payment_key: str,
        order_id: str,
        amount: int,
    ) -> dict:
        """Confirm (승인) a Toss payment after user completes checkout.

        Called from the success redirect URL handler.
        Returns credit info if successful.
        """
        secret_key = os.environ.get("TOSS_SECRET_KEY", "")
        if not secret_key:
            raise RuntimeError("TOSS_SECRET_KEY not configured")

        # Base64 encode secret_key for Authorization header
        auth_str = base64.b64encode(f"{secret_key}:".encode()).decode()

        # Call Toss Payments confirm API
        response = httpx.post(
            "https://api.tosspayments.com/v1/payments/confirm",
            headers={
                "Authorization": f"Basic {auth_str}",
                "Content-Type": "application/json",
            },
            json={
                "paymentKey": payment_key,
                "orderId": order_id,
                "amount": amount,
            },
            timeout=30.0,
        )

        if response.status_code != 200:
            error_data = response.json()
            logger.error("Toss payment confirm failed: %s", error_data)

            # Update record as failed
            if order_id in self._payments:
                with self._lock:
                    self._payments[order_id].status = "failed"
                    self._payments[order_id].metadata["error"] = error_data
                    self._persist()

            raise RuntimeError(
                f"결제 승인 실패: {error_data.get('message', 'Unknown error')}"
            )

        result = response.json()
        payment_method = result.get("method", "")
        easy_pay = result.get("easyPay", {})

        # Determine actual payment method
        if easy_pay:
            provider = easy_pay.get("provider", "")
            if provider:
                payment_method = provider  # 카카오페이, 네이버페이, 토스페이, etc.

        # Update payment record
        if order_id in self._payments:
            with self._lock:
                rec = self._payments[order_id]
                rec.status = "completed"
                rec.gateway_payment_id = payment_key
                rec.method = payment_method
                rec.completed_at = time.time()
                rec.metadata["toss_response"] = {
                    "status": result.get("status"),
                    "method": result.get("method"),
                    "easyPay": easy_pay,
                    "approvedAt": result.get("approvedAt"),
                    "receipt_url": result.get("receipt", {}).get("url", ""),
                }
                self._persist()

            return {
                "user_id": rec.user_id,
                "amount_usd": rec.amount_usd,
                "amount_krw": amount,
                "gateway": "toss",
                "payment_id": order_id,
                "payment_key": payment_key,
                "method": payment_method,
                "receipt_url": result.get("receipt", {}).get("url", ""),
            }

        raise RuntimeError(f"Payment record not found: {order_id}")

    def cancel_toss_payment(
        self,
        payment_key: str,
        cancel_reason: str = "고객 요청에 의한 취소",
    ) -> dict:
        """Cancel (취소) a Toss payment."""
        secret_key = os.environ.get("TOSS_SECRET_KEY", "")
        if not secret_key:
            raise RuntimeError("TOSS_SECRET_KEY not configured")

        auth_str = base64.b64encode(f"{secret_key}:".encode()).decode()

        response = httpx.post(
            f"https://api.tosspayments.com/v1/payments/{payment_key}/cancel",
            headers={
                "Authorization": f"Basic {auth_str}",
                "Content-Type": "application/json",
            },
            json={"cancelReason": cancel_reason},
            timeout=30.0,
        )

        if response.status_code != 200:
            error_data = response.json()
            raise RuntimeError(
                f"결제 취소 실패: {error_data.get('message', 'Unknown error')}"
            )

        return response.json()

    # ------------------------------------------------------------------
    # Payment history & lookup
    # ------------------------------------------------------------------

    def get_payment(self, payment_id: str) -> PaymentRecord | None:
        return self._payments.get(payment_id)

    def get_user_payments(self, user_id: str, limit: int = 50) -> list[dict]:
        """Get recent payments for a user."""
        records = [
            asdict(r) for r in self._payments.values()
            if r.user_id == user_id
        ]
        records.sort(key=lambda x: x["created_at"], reverse=True)
        return records[:limit]

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _save_record(self, record: PaymentRecord) -> None:
        with self._lock:
            self._payments[record.payment_id] = record
            self._persist()

    def _persist(self) -> None:
        path = self.data_dir / "payments.json"
        data = {pid: asdict(rec) for pid, rec in self._payments.items()}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def _load(self) -> None:
        path = self.data_dir / "payments.json"
        if not path.exists():
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            for pid, info in data.items():
                self._payments[pid] = PaymentRecord(
                    payment_id=info.get("payment_id", pid),
                    gateway=info.get("gateway", ""),
                    gateway_payment_id=info.get("gateway_payment_id", ""),
                    user_id=info.get("user_id", ""),
                    amount_usd=info.get("amount_usd", 0.0),
                    amount_krw=info.get("amount_krw", 0),
                    currency=info.get("currency", "usd"),
                    status=info.get("status", "pending"),
                    method=info.get("method", ""),
                    created_at=info.get("created_at", 0.0),
                    completed_at=info.get("completed_at", 0.0),
                    metadata=info.get("metadata", {}),
                )
        except Exception as exc:
            logger.error("Failed to load payment records: %s", exc)
