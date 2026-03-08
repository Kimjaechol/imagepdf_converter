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
