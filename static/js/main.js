/* HireBridge Africa – Main JS */
(function () {
  'use strict';

  // ── Navbar scroll shadow ─────────────────────────────────
  const header = document.getElementById('site-header');
  if (header) {
    const onScroll = () => header.classList.toggle('scrolled', window.scrollY > 20);
    window.addEventListener('scroll', onScroll, { passive: true });
    onScroll();
  }

  // ── Hamburger / Mobile Drawer ────────────────────────────
  const hamburger = document.getElementById('hamburger');
  const drawer    = document.getElementById('mobileDrawer');
  const overlay   = document.getElementById('mobileOverlay');
  const drawerClose = document.getElementById('drawerClose');

  function openDrawer() {
    if (!drawer) return;
    drawer.classList.add('open');
    overlay.classList.add('open');
    hamburger.classList.add('open');
    hamburger.setAttribute('aria-expanded', 'true');
    drawer.setAttribute('aria-hidden', 'false');
    document.body.style.overflow = 'hidden';
  }
  function closeDrawer() {
    if (!drawer) return;
    drawer.classList.remove('open');
    overlay.classList.remove('open');
    hamburger.classList.remove('open');
    hamburger.setAttribute('aria-expanded', 'false');
    drawer.setAttribute('aria-hidden', 'true');
    document.body.style.overflow = '';
  }

  if (hamburger) hamburger.addEventListener('click', openDrawer);
  if (drawerClose) drawerClose.addEventListener('click', closeDrawer);
  if (overlay) overlay.addEventListener('click', closeDrawer);
  document.addEventListener('keydown', e => { if (e.key === 'Escape') closeDrawer(); });

  // ── Flash auto-dismiss ───────────────────────────────────
  const flashContainer = document.getElementById('flashContainer');
  if (flashContainer) {
    setTimeout(() => {
      flashContainer.querySelectorAll('.flash-item').forEach((el, i) => {
        setTimeout(() => el.style.cssText = 'opacity:0;transform:translateX(20px);transition:.3s;', i * 200);
        setTimeout(() => el.remove(), i * 200 + 300);
      });
    }, 5000);
  }

  // ── Newsletter Modal ─────────────────────────────────────
  const nlModal  = document.getElementById('newsletterModal');
  const fabNl    = document.getElementById('fabNewsletter');
  const nlClose  = document.getElementById('nlModalClose');

  function openModal()  { if (nlModal) nlModal.classList.add('open'); }
  function closeModal() { if (nlModal) nlModal.classList.remove('open'); }

  if (fabNl)  fabNl.addEventListener('click', openModal);
  if (nlClose) nlClose.addEventListener('click', closeModal);
  if (nlModal) nlModal.addEventListener('click', e => { if (e.target === nlModal) closeModal(); });

  // Show newsletter modal once after 30s (new visitors)
  if (nlModal && !localStorage.getItem('hba_nl_shown')) {
    setTimeout(() => {
      openModal();
      localStorage.setItem('hba_nl_shown', '1');
    }, 30000);
  }

  // ── Consent Banner ───────────────────────────────────────
  const consentBanner = document.getElementById('consentBanner');
  const consentAccept = document.getElementById('consentAccept');

  if (consentBanner && !localStorage.getItem('hba_consent')) {
    setTimeout(() => consentBanner.classList.add('show'), 1500);
  }
  if (consentAccept) {
    consentAccept.addEventListener('click', () => {
      localStorage.setItem('hba_consent', '1');
      consentBanner.classList.remove('show');
      setTimeout(() => consentBanner.remove(), 400);
    });
  }

  // ── Active sidebar link by hash ──────────────────────────
  const sidebarLinks = document.querySelectorAll('.sidebar-link');
  if (sidebarLinks.length) {
    const hash = window.location.hash;
    sidebarLinks.forEach(a => {
      a.classList.toggle('active', a.getAttribute('href') === hash);
    });
    sidebarLinks.forEach(a => {
      a.addEventListener('click', () => {
        sidebarLinks.forEach(l => l.classList.remove('active'));
        a.classList.add('active');
      });
    });
  }

  // ── Smooth scroll to hash sections ──────────────────────
  document.querySelectorAll('a[href^="#"]').forEach(anchor => {
    anchor.addEventListener('click', function (e) {
      const target = document.querySelector(this.getAttribute('href'));
      if (target) {
        e.preventDefault();
        target.scrollIntoView({ behavior: 'smooth', block: 'start' });
      }
    });
  });

  // ── Confirm dangerous actions ────────────────────────────
  document.querySelectorAll('[data-confirm]').forEach(btn => {
    btn.addEventListener('click', e => {
      if (!confirm(btn.dataset.confirm)) e.preventDefault();
    });
  });

  // ── Table row hover effect (jobs) ─────────────────────────
  document.querySelectorAll('.dash-table tr').forEach(row => {
    row.style.cursor = 'default';
  });

})();

// ── Sticky filter bar detection ──────────────────────────────
(function() {
  const filterBar = document.querySelector('.job-filters-bar');
  if (!filterBar) return;
  const observer = new IntersectionObserver(
    ([e]) => filterBar.classList.toggle('stuck', e.intersectionRatio < 1),
    { threshold: [1], rootMargin: '-1px 0px 0px 0px' }
  );
  observer.observe(filterBar);
})();

// ── Loading overlay on filter form submit ─────────────────────
(function() {
  const form = document.getElementById('jobFilterForm');
  const overlay = document.getElementById('jobsLoadingOverlay');
  if (!form || !overlay) return;
  form.addEventListener('submit', () => {
    overlay.classList.add('show');
    // Remove after 8s max as fallback
    setTimeout(() => overlay.classList.remove('show'), 8000);
  });
  // Also show on pagination links
  document.querySelectorAll('.page-btn').forEach(btn => {
    btn.addEventListener('click', () => overlay.classList.add('show'));
  });
  // Hide if page loaded (back-button scenario)
  window.addEventListener('pageshow', () => overlay.classList.remove('show'));
})();

// ── Active filter chips (show selected non-default filters) ───
(function() {
  const form = document.getElementById('jobFilterForm');
  if (!form) return;
  const container = document.getElementById('activeFiltersContainer');
  if (!container) return;

  const params = new URLSearchParams(window.location.search);
  const labels = {
    location: 'Location',
    category: 'Category',
    level: 'Level',
    type: 'Type',
  };
  let hasFilters = false;
  params.forEach((val, key) => {
    if (!val || val === 'all' || key === 'q' || key === 'page') return;
    hasFilters = true;
    const chip = document.createElement('span');
    chip.className = 'active-filter-chip';
    chip.innerHTML = `<i class="fas fa-tag"></i>${labels[key] || key}: ${decodeURIComponent(val)}
      <button aria-label="Remove ${key} filter" data-key="${key}">×</button>`;
    chip.querySelector('button').addEventListener('click', () => {
      params.delete(key);
      params.set('page', '1');
      window.location.search = params.toString();
    });
    container.appendChild(chip);
  });
  if (hasFilters && container.parentElement) {
    container.parentElement.style.display = 'flex';
  }
})();

// ── Skeleton loaders on initial page paint ─────────────────────
(function() {
  // Only run if grid is present and empty (first load)
  const grid = document.querySelector('.jobs-grid');
  if (!grid || grid.children.length > 0) return;
  for (let i = 0; i < 6; i++) {
    grid.innerHTML += `
      <div class="skeleton-card">
        <div class="skeleton-header">
          <div class="skeleton skeleton-icon"></div>
          <div style="flex:1;display:flex;flex-direction:column;gap:6px">
            <div class="skeleton skeleton-title"></div>
            <div class="skeleton skeleton-line w-40"></div>
          </div>
        </div>
        <div class="skeleton-tags">
          <div class="skeleton skeleton-tag"></div>
          <div class="skeleton skeleton-tag"></div>
          <div class="skeleton skeleton-tag"></div>
        </div>
        <div class="skeleton skeleton-line w-80"></div>
        <div class="skeleton skeleton-line w-60"></div>
      </div>`;
  }
})();

// ── Mark current mobile bottom nav link active ─────────────────
(function() {
  const path = window.location.pathname;
  document.querySelectorAll('.mobile-bottom-nav a').forEach(a => {
    if (a.getAttribute('href') === path) a.classList.add('active');
  });
})();
