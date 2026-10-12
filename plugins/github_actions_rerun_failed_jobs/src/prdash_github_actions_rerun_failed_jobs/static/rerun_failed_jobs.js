(function() {
    var POLL_INTERVAL_MS = 60000;
    var tracking = new Map();
    var requesting = new Set();

    function csrfToken() {
        var el = document.querySelector('[name=csrfmiddlewaretoken]');
        return el ? el.value : '';
    }

    function findMenu(key) {
        var menus = document.querySelectorAll('.pr-ci-rerun-menu');
        for (var i = 0; i < menus.length; i++) {
            if (menus[i].dataset.key === key) return menus[i];
        }
        return null;
    }

    function setTrackingUI(menu, active) {
        var group = menu.closest('.pr-ci-rerun-group');
        var toggle = group.querySelector('.pr-ci-rerun-dropdown-toggle');
        var item = group.querySelector('.pr-ci-rerun-track-item');
        var indicator = group.querySelector('.pr-ci-rerun-tracking-indicator');
        toggle.classList.toggle('tracking', active);
        item.classList.toggle('tracking', active);
        item.textContent = active ? 'Stop tracking' : 'Track and retry when available';
        if (indicator) indicator.hidden = !active;
    }

    function setRequestSpinner(menu, active) {
        var spinner = menu.closest('.pr-ci-rerun-group').querySelector('.pr-ci-rerun-spinner');
        if (spinner) spinner.hidden = !active;
    }

    function stopTracking(key) {
        var timerId = tracking.get(key);
        if (timerId !== undefined) {
            clearTimeout(timerId);
            tracking.delete(key);
        }
        requesting.delete(key);
        var menu = findMenu(key);
        if (menu) {
            setTrackingUI(menu, false);
            setRequestSpinner(menu, false);
        }
    }

    // Shared by tick() and the post-swap listener since htmx swaps replace PR cards with fresh DOM nodes.
    function resync(key) {
        var menu = findMenu(key);
        if (!menu) {
            stopTracking(key);
            return null;
        }
        setTrackingUI(menu, true);
        setRequestSpinner(menu, requesting.has(key));
        return menu;
    }

    function postAction(url, formData, errorContext) {
        return fetch(url, {
            method: 'POST',
            credentials: 'same-origin',
            headers: { 'X-CSRFToken': csrfToken() },
            body: formData,
        }).then(function(response) {
            if (!response.ok) {
                return { outcome: 'error', message: 'Server returned HTTP ' + response.status + ' while ' + errorContext + '.' };
            }
            return response.json();
        });
    }

    function attemptRerun(menu) {
        var formData = new FormData();
        formData.append('owner', menu.dataset.owner);
        formData.append('repository', menu.dataset.repository);
        (menu.dataset.runIds || '').split(',').filter(Boolean).forEach(function(runId) {
            formData.append('run_id', runId);
        });
        return postAction(menu.dataset.url, formData, 'retrying');
    }

    function updateBranch(menu) {
        var formData = new FormData();
        formData.append('owner', menu.dataset.owner);
        formData.append('repository', menu.dataset.repository);
        formData.append('pr_number', menu.dataset.key.split('#')[1]);
        return postAction(menu.dataset.updateBranchUrl, formData, 'updating the branch');
    }

    function finishRequest(key, menu) {
        requesting.delete(key);
        if (!tracking.has(key)) return false;
        setRequestSpinner(findMenu(key) || menu, false);
        return true;
    }

    function tick(key) {
        if (!tracking.has(key)) return;
        requesting.add(key);
        var menu = resync(key);
        if (!menu) {
            requesting.delete(key);
            return;
        }
        attemptRerun(menu).then(function(result) {
            if (!finishRequest(key, menu)) return;
            if (result.outcome === 'success') {
                window.showToast(result.message, 'success');
                stopTracking(key);
            } else if (result.outcome === 'retry') {
                tracking.set(key, setTimeout(function() { tick(key); }, POLL_INTERVAL_MS));
            } else {
                window.showToast(result.message, 'error');
                stopTracking(key);
            }
        }).catch(function() {
            if (!finishRequest(key, menu)) return;
            window.showToast('Lost connection while tracking CI status.', 'error');
            stopTracking(key);
        });
    }

    function startTracking(menu) {
        var key = menu.dataset.key;
        tracking.set(key, null);
        setTrackingUI(menu, true);
        window.showToast('Tracking CI status. Will retry automatically once available.', 'success');
        tick(key);
    }

    document.addEventListener('DOMContentLoaded', function() {
        document.body.addEventListener('githubActionsRerunFailedJobsToast', function(event) {
            var toast = event.detail.value || event.detail;
            window.showToast(toast.message, toast.type);
        });

        document.body.addEventListener('htmx:afterSwap', function(event) {
            if (tracking.size === 0) return;
            var target = event.detail.target;
            if (target && target.id === 'pr-content') {
                tracking.forEach(function(_, key) { resync(key); });
            }
        });

        document.addEventListener('click', function(evt) {
            var toggle = evt.target.closest('.pr-ci-rerun-dropdown-toggle');
            if (toggle) {
                var group = toggle.closest('.pr-ci-rerun-group');
                var menu = group.querySelector('.pr-ci-rerun-menu');
                var wasOpen = !menu.hidden;
                document.querySelectorAll('.pr-ci-rerun-menu').forEach(function(m) { m.hidden = true; });
                document.querySelectorAll('.pr-ci-rerun-dropdown-toggle').forEach(function(t) {
                    t.setAttribute('aria-expanded', 'false');
                });
                menu.hidden = wasOpen;
                toggle.setAttribute('aria-expanded', String(!wasOpen));
                return;
            }

            var item = evt.target.closest('.pr-ci-rerun-track-item');
            if (item) {
                var menuEl = item.closest('.pr-ci-rerun-menu');
                var key = menuEl.dataset.key;
                if (tracking.has(key)) {
                    stopTracking(key);
                } else {
                    startTracking(menuEl);
                }
                menuEl.hidden = true;
                return;
            }

            var updateBranchItem = evt.target.closest('.pr-ci-rerun-update-branch-item');
            if (updateBranchItem) {
                var updateBranchMenu = updateBranchItem.closest('.pr-ci-rerun-menu');
                updateBranchMenu.hidden = true;
                updateBranch(updateBranchMenu).then(function(result) {
                    window.showToast(result.message, result.outcome === 'success' ? 'success' : 'error');
                    if (result.outcome === 'success') {
                        htmx.trigger(document.body, 'manualRefresh');
                    }
                }).catch(function() {
                    window.showToast('Lost connection while updating the branch.', 'error');
                });
                return;
            }

            if (!evt.target.closest('.pr-ci-rerun-dropdown')) {
                document.querySelectorAll('.pr-ci-rerun-menu').forEach(function(m) { m.hidden = true; });
            }
        });
    });
})();
