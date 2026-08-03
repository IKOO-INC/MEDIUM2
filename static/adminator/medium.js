(function () {
    'use strict';

    var root = document.documentElement;
    var body = document.body;
    var themeToggle = document.getElementById('themeToggle');
    var drawerOpen = document.getElementById('drawerOpen');
    var drawerBackdrop = document.getElementById('drawerBackdrop');
    var currentDate = document.getElementById('currentDate');
    var heroDate = document.getElementById('heroDate');

    function themeIcon() {
        if (!themeToggle) return;
        var isDark = root.getAttribute('data-theme') === 'dark';
        themeToggle.innerHTML = isDark
            ? '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41"/></svg>'
            : '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/></svg>';
        themeToggle.setAttribute('aria-label', isDark ? 'Gunakan tema terang' : 'Gunakan tema gelap');
    }

    function closeDrawer() {
        body.classList.remove('has-drawer-open');
    }

    if (themeToggle) {
        themeIcon();
        themeToggle.addEventListener('click', function () {
            var nextTheme = root.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
            root.setAttribute('data-theme', nextTheme);
            try { localStorage.setItem('medium-theme', nextTheme); } catch (error) {}
            themeIcon();
        });
    }

    if (drawerOpen) {
        drawerOpen.addEventListener('click', function () {
            body.classList.add('has-drawer-open');
        });
    }
    if (drawerBackdrop) drawerBackdrop.addEventListener('click', closeDrawer);
    document.addEventListener('keydown', function (event) {
        if (event.key === 'Escape') closeDrawer();
    });
    document.querySelectorAll('.d-sidebar a').forEach(function (link) {
        link.addEventListener('click', closeDrawer);
    });

    var now = new Date();
    if (currentDate) {
        currentDate.textContent = new Intl.DateTimeFormat('id-ID', {
            weekday: 'short', day: 'numeric', month: 'short', year: 'numeric'
        }).format(now);
    }
    if (heroDate) {
        heroDate.textContent = new Intl.DateTimeFormat('id-ID', {
            weekday: 'long', day: 'numeric', month: 'long', year: 'numeric'
        }).format(now);
    }
})();
