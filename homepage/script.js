// ─── Navbar scroll effect ───
const nav = document.getElementById('nav');
window.addEventListener('scroll', () => {
  nav.classList.toggle('scrolled', window.scrollY > 20);
});

// ─── Mobile menu toggle ───
const navToggle = document.getElementById('nav-toggle');
const navLinks = document.getElementById('nav-links');

navToggle.addEventListener('click', () => {
  navLinks.classList.toggle('open');
});

// Close mobile menu on link click
navLinks.querySelectorAll('a').forEach(link => {
  link.addEventListener('click', () => {
    navLinks.classList.remove('open');
  });
});

// ─── Smooth scroll for anchor links ───
document.querySelectorAll('a[href^="#"]').forEach(anchor => {
  anchor.addEventListener('click', (e) => {
    e.preventDefault();
    const target = document.querySelector(anchor.getAttribute('href'));
    if (target) {
      const offset = 80;
      const pos = target.getBoundingClientRect().top + window.scrollY - offset;
      window.scrollTo({ top: pos, behavior: 'smooth' });
    }
  });
});

// ─── Intersection Observer for fade-in animations ───
const observerOptions = {
  threshold: 0.1,
  rootMargin: '0px 0px -50px 0px'
};

const observer = new IntersectionObserver((entries) => {
  entries.forEach(entry => {
    if (entry.isIntersecting) {
      entry.target.classList.add('visible');
      observer.unobserve(entry.target);
    }
  });
}, observerOptions);

// Observe all animatable elements
document.querySelectorAll('.feature-card, .step, .format-card, .pricing-card, .download-card').forEach(el => {
  el.classList.add('fade-in');
  observer.observe(el);
});

// ─── Download links (placeholder - replace with actual Cloudflare R2 URLs) ───
const downloadLinks = {
  windows: '#',  // TODO: Replace with Cloudflare R2 URL
  macos: '#',
  linux: '#'
};

document.getElementById('dl-windows')?.setAttribute('href', downloadLinks.windows);
document.getElementById('dl-macos')?.setAttribute('href', downloadLinks.macos);
document.getElementById('dl-linux')?.setAttribute('href', downloadLinks.linux);

// ─── Payment Section ───
(function() {
  const API_BASE = window.PAYMENT_API_BASE || '';

  // Tab switching
  document.querySelectorAll('.payment-tab').forEach(tab => {
    tab.addEventListener('click', () => {
      document.querySelectorAll('.payment-tab').forEach(t => t.classList.remove('active'));
      document.querySelectorAll('.payment-panel').forEach(p => p.classList.remove('active'));
      tab.classList.add('active');
      const gateway = tab.dataset.gateway;
      document.getElementById('panel-' + gateway)?.classList.add('active');
    });
  });

  // Amount selection (Toss)
  let selectedKrw = 20000;
  document.querySelectorAll('#panel-toss .amount-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('#panel-toss .amount-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      selectedKrw = parseInt(btn.dataset.krw);
      updateTossPayButton();
    });
  });

  // Amount selection (Stripe)
  let selectedUsd = 20;
  document.querySelectorAll('#panel-stripe .amount-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('#panel-stripe .amount-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      selectedUsd = parseInt(btn.dataset.usd);
    });
  });

  // Payment method selection (Toss)
  let selectedMethod = '';
  document.querySelectorAll('.method-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.method-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      selectedMethod = btn.dataset.method;
      updateTossPayButton();
    });
  });

  function updateTossPayButton() {
    const btn = document.getElementById('btn-pay-toss');
    if (selectedMethod && selectedKrw > 0) {
      btn.disabled = false;
      const formatted = selectedKrw.toLocaleString('ko-KR');
      btn.textContent = '\u20A9' + formatted + ' \uACB0\uC81C\uD558\uAE30';
    } else {
      btn.disabled = true;
      btn.textContent = '\uACB0\uC81C \uC218\uB2E8\uC744 \uC120\uD0DD\uD574\uC8FC\uC138\uC694';
    }
  }
  updateTossPayButton();

  // Toss pay button
  document.getElementById('btn-pay-toss')?.addEventListener('click', async () => {
    const token = localStorage.getItem('moa_token');
    if (!token) {
      alert('\uB85C\uADF8\uC778\uC774 \uD544\uC694\uD569\uB2C8\uB2E4.');
      return;
    }
    try {
      const res = await fetch(API_BASE + '/api/payments/toss/checkout', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Authorization': 'Bearer ' + token,
        },
        body: JSON.stringify({
          amount_krw: selectedKrw,
          method: selectedMethod,
        }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || 'Payment error');

      // Initialize Toss Payments SDK
      if (window.TossPayments) {
        const tossPayments = TossPayments(data.client_key);
        const payment = tossPayments.payment({ customerKey: 'ANONYMOUS' });
        await payment.requestPayment({
          method: selectedMethod === '\uAC04\uD3B8\uACB0\uC81C' ? 'EASY_PAY' :
                  selectedMethod === '\uCE74\uB4DC' ? 'CARD' :
                  selectedMethod === '\uACC4\uC88C\uC774\uCCB4' ? 'TRANSFER' :
                  selectedMethod === '\uAC00\uC0C1\uACC4\uC88C' ? 'VIRTUAL_ACCOUNT' :
                  selectedMethod === '\uD734\uB300\uD3F0' ? 'MOBILE_PHONE' : 'CARD',
          amount: { currency: 'KRW', value: data.amount },
          orderId: data.order_id,
          orderName: data.order_name,
          successUrl: data.success_url,
          failUrl: data.fail_url,
        });
      } else {
        alert('Toss Payments SDK\uAC00 \uB85C\uB4DC\uB418\uC9C0 \uC54A\uC558\uC2B5\uB2C8\uB2E4.');
      }
    } catch (err) {
      console.error('Toss payment error:', err);
      alert('\uACB0\uC81C \uC624\uB958: ' + err.message);
    }
  });

  // Stripe pay button
  document.getElementById('btn-pay-stripe')?.addEventListener('click', async () => {
    const token = localStorage.getItem('moa_token');
    if (!token) {
      alert('\uB85C\uADF8\uC778\uC774 \uD544\uC694\uD569\uB2C8\uB2E4.');
      return;
    }
    try {
      const res = await fetch(API_BASE + '/api/payments/stripe/checkout', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Authorization': 'Bearer ' + token,
        },
        body: JSON.stringify({ amount_usd: selectedUsd }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || 'Payment error');
      window.location.href = data.checkout_url;
    } catch (err) {
      console.error('Stripe payment error:', err);
      alert('Payment error: ' + err.message);
    }
  });
})();
