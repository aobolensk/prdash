(function() {
    function dialog() {
        let element = document.getElementById('github-pr-preview-dialog');
        if (element) return element;
        element = document.createElement('dialog');
        element.id = 'github-pr-preview-dialog';
        element.className = 'github-pr-preview-dialog';
        element.innerHTML = '<div class="github-pr-preview-shell"><button type="button" class="btn btn-secondary btn-icon-square github-pr-preview-close" title="Close" aria-label="Close"><svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M18 6 6 18"></path><path d="m6 6 12 12"></path></svg></button><div class="github-pr-preview-content"></div></div>';
        document.body.appendChild(element);
        return element;
    }

    function formatDates(container) {
        container.querySelectorAll('[data-github-pr-preview-date]').forEach(function(element) {
            const date = new Date(element.dateTime);
            if (!Number.isNaN(date.valueOf())) {
                element.textContent = new Intl.DateTimeFormat(undefined, {
                    dateStyle: 'medium', timeStyle: 'short'
                }).format(date);
            }
        });
    }

    document.addEventListener('click', function(event) {
        const openLink = event.target.closest('.github-pr-preview-open');
        if (openLink) {
            if (event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
            event.preventDefault();
            const element = dialog();
            const content = element.querySelector('.github-pr-preview-content');
            content.textContent = 'Loading diff...';
            element.showModal();
            fetch(openLink.href, {credentials: 'same-origin'})
                .then(function(response) { return response.text(); })
                .then(function(html) { content.innerHTML = html; formatDates(content); window.htmx.process(content); })
                .catch(function() { content.textContent = 'Could not load the pull request diff.'; });
            return;
        }
        const element = event.target.closest('#github-pr-preview-dialog');
        if (!element) return;
        if (event.target.closest('.github-pr-preview-close')) element.close();
        const commentButton = event.target.closest('.github-pr-preview-comment-button');
        if (commentButton) commentButton.nextElementSibling.classList.add('is-open');
        const cancelButton = event.target.closest('.github-pr-preview-cancel');
        if (cancelButton) cancelButton.closest('form').classList.remove('is-open');
        const replyButton = event.target.closest('.github-pr-preview-reply-button');
        if (replyButton) replyButton.nextElementSibling.classList.add('is-open');
    });

    document.addEventListener('click', function(event) {
        const toggle = event.target.closest('.github-pr-preview-merge-dropdown-toggle');
        if (toggle) {
            const menu = toggle.closest('.github-pr-preview-merge-dropdown').querySelector('.github-pr-preview-merge-menu');
            const wasOpen = !menu.hidden;
            menu.hidden = wasOpen;
            toggle.setAttribute('aria-expanded', String(!wasOpen));
            return;
        }
        if (event.target.closest('.github-pr-preview-merge-menu-item')) {
            event.target.closest('.github-pr-preview-merge-menu').hidden = true;
            return;
        }
        const dropdown = event.target.closest('.github-pr-preview-merge-dropdown');
        if (!dropdown) {
            const menu = document.querySelector('.github-pr-preview-merge-menu');
            if (menu) menu.hidden = true;
        }
    });

    document.addEventListener('githubPRPreviewToast', function(event) {
        const toast = event.detail.value || event.detail;
        window.showToast(toast.message, toast.type);
    });

    formatDates(document);
})();
