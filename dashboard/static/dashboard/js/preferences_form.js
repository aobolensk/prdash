(function() {
    document.querySelectorAll('.preferences-form [data-interval-toggle]').forEach(function(checkbox) {
        checkbox.addEventListener('change', function() {
            var target = document.querySelector('.interval-subfield[data-interval-for="' + this.getAttribute('data-interval-toggle') + '"]');
            if (target) {
                target.hidden = !this.checked;
            }
        });
    });
})();
