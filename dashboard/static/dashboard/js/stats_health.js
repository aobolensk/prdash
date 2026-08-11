(function() {
    document.querySelectorAll('.health-stat[data-toggle]').forEach(function(btn) {
        btn.addEventListener('click', function() {
            var targetId = this.getAttribute('data-toggle');
            var target = document.getElementById(targetId);
            var icon = this.querySelector('.health-stat-icon');
            if (target) {
                if (target.style.display === 'none') {
                    target.style.display = 'block';
                    if (icon) icon.textContent = '−';
                    this.classList.add('active');
                } else {
                    target.style.display = 'none';
                    if (icon) icon.textContent = '+';
                    this.classList.remove('active');
                }
            }
        });
    });
})();
