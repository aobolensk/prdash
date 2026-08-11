document.addEventListener('DOMContentLoaded', function() {
    var errorsEl = document.getElementById('pr-list-errors');
    var warningsEl = document.getElementById('pr-list-warnings');
    if (errorsEl) {
        JSON.parse(errorsEl.textContent).forEach(function(message) {
            showToast(message, 'error');
        });
    }
    if (warningsEl) {
        JSON.parse(warningsEl.textContent).forEach(function(message) {
            showToast(message, 'warning');
        });
    }
});
