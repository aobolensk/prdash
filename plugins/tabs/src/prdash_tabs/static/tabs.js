(function() {
    const tabLabels = {
        my_prs: 'My PRs',
        my_prs_merged: 'My PRs: Merged',
        author_prs: 'PRs by Author',
        review_requests: 'Review Requests',
        assigned: 'Assigned'
    };
    const tabShortLabels = {
        my_prs: 'M',
        my_prs_merged: 'G',
        author_prs: 'A',
        review_requests: 'R',
        assigned: 'D'
    };
    const storageKey = 'prdash-tabs:' +
        (window.prdashTabsUserId || 'user');
    const inFlightRequests = new Set();
    const syntheticSources = new WeakSet();
    let tabs = [];
    let activeTab = null;

    function dashboard() {
        return document.getElementById('dashboard-layout');
    }

    function content() {
        return document.getElementById('pr-content');
    }

    function contentHtml(list) {
        const snapshot = list.cloneNode(true);
        snapshot.querySelectorAll('.htmx-request, .htmx-added, .htmx-settling, .htmx-swapping')
            .forEach(function(element) {
                element.classList.remove('htmx-request', 'htmx-added', 'htmx-settling', 'htmx-swapping');
        });
        return snapshot.innerHTML;
    }

    function restoreContent(list, html) {
        if (window.htmx && window.htmx.swap) {
            window.htmx.swap(list, html, { swapStyle: 'innerHTML', ignoreTitle: true }, {
                contextElement: list,
                eventInfo: { target: list, elt: list }
            });
            window.htmx.process(list);
        } else {
            list.innerHTML = html;
        }
    }

    function ensureTabBar() {
        if (document.getElementById('prdash-tabs-bar')) return true;
        const main = document.querySelector('.main-content');
        if (!main) return false;
        const bar = document.createElement('nav');
        bar.id = 'prdash-tabs-bar';
        bar.className = 'prdash-tabs-bar';
        bar.setAttribute('aria-label', 'Dashboard tabs');
        bar.hidden = true;
        const list = document.createElement('ul');
        list.id = 'prdash-tabs';
        list.className = 'prdash-tabs';
        bar.appendChild(list);
        const searchBar = document.getElementById('pr-search-bar');
        main.insertBefore(bar, searchBar || main.firstElementChild);
        return true;
    }

    function currentUrl() {
        return window.location.pathname + window.location.search;
    }

    function groupForTab(tab) {
        if (tab === 'author' || tab === 'author_merged' || tab === 'author_prs') return 'author_prs';
        if (tab === 'review_requests' || tab === 'review_approved') return 'review_requests';
        if (tab === 'assigned') return 'assigned';
        return 'my_prs';
    }

    function defaultTabForGroup(group) {
        if (group === 'my_prs_merged') return 'merged';
        return group === 'author_prs' ? 'author' : group;
    }

    function tabIdForCurrent(currentTab) {
        return currentTab === 'merged' ? 'my_prs_merged' : groupForTab(currentTab);
    }

    function tabForUrl(group, url) {
        if (group === 'author_prs' && /\/merged\/?(?:\?|$)/.test(url)) return 'author_merged';
        return defaultTabForGroup(group);
    }

    function repoForUrl(group, url) {
        if (group === 'author_prs') return '';
        if (/\/prs\/(?:merged|review-requests|assigned)(?:\/|\?|$)/.test(url)) return '';
        const match = url.match(/\/prs\/([^/]+)\/([^/?]+)(?:\/|\?|$)/);
        return match ? match[1] + '/' + match[2] : '';
    }

    function groupForUrl(url) {
        const path = url.split('?')[0];
        if (/\/prs\/by-author(?:\/|$)/.test(path)) return 'author_prs';
        if (/\/review-requests(?:\/|$)/.test(path)) return 'review_requests';
        if (/\/assigned(?:\/|$)/.test(path)) return 'assigned';
        if (/\/merged\/?$/.test(path)) return 'my_prs_merged';
        return 'my_prs';
    }

    function getSearchState() {
        const api = window.prdash && window.prdash.pullRequestSearch;
        return api ? api.getState() : null;
    }

    function setSearchState(state) {
        const api = window.prdash && window.prdash.pullRequestSearch;
        if (!api) return;
        api.setState(state || {
            open: false,
            include: { text: '', pills: [] },
            exclude: { text: '', pills: [] }
        });
    }

    function saveTab(tab) {
        if (!tab) return;
        const layout = dashboard();
        const list = content();
        if (tab === activeTab && layout && list) {
            tab.url = currentUrl();
            if (!tab.pendingRender) tab.html = contentHtml(list);
            tab.currentTab = layout.dataset.currentTab || defaultTabForGroup(tab.id);
            tab.currentRepo = layout.dataset.currentRepo || '';
            tab.reviewTab = layout.dataset.reviewTab || 'pending';
            tab.title = document.title;
            tab.search = getSearchState();
            const main = document.querySelector('.main-content');
            tab.scrollTop = main ? main.scrollTop : 0;
        }
        persistTabs();
    }

    function persistTabs() {
        const data = {
            activeId: activeTab && activeTab.id,
            tabs: tabs.map(function(tab) {
                return {
                    id: tab.id,
                    url: tab.url,
                    currentTab: tab.currentTab,
                    currentRepo: tab.currentRepo,
                    reviewTab: tab.reviewTab,
                    title: tab.title,
                    search: tab.search,
                    scrollTop: tab.scrollTop
                };
            })
        };
        try {
            window.sessionStorage.setItem(storageKey, JSON.stringify(data));
        } catch {
            // Tab state still works for the lifetime of this page.
        }
    }

    function readTabs() {
        try {
            const data = JSON.parse(window.sessionStorage.getItem(storageKey) || '{}');
            return Array.isArray(data.tabs) ? data.tabs.filter(function(tab) {
                return tab && tabLabels[tab.id] && typeof tab.url === 'string';
            }) : [];
        } catch {
            return [];
        }
    }

    function tabDocument(tab) {
        return new DOMParser().parseFromString(tab.html || '', 'text/html');
    }

    function appendControlValue(params, control, names) {
        if (!control.name || control.disabled || /^(button|submit|reset|file|image)$/i.test(control.type)) return;
        if ((control.type === 'checkbox' || control.type === 'radio') && !control.checked) return;
        names.add(control.name);
        if (control.tagName === 'SELECT' && control.multiple) {
            Array.from(control.selectedOptions).forEach(function(option) {
                params.append(control.name, option.value);
            });
        } else {
            params.append(control.name, control.value);
        }
    }

    function mergeParams(url, params, names) {
        names.forEach(function(name) { url.searchParams.delete(name); });
        params.forEach(function(value, name) { url.searchParams.append(name, value); });
    }

    function backgroundRequestUrl(tab, doc, autoRefresh) {
        const rawUrl = autoRefresh && autoRefresh.getAttribute('hx-get') || tab.url;
        const url = new URL(rawUrl, window.location.href);
        if (!autoRefresh) return url;
        const params = new URLSearchParams();
        const names = new Set();
        const controls = new Set();
        (autoRefresh.getAttribute('hx-include') || '').split(',').forEach(function(selector) {
            if (!selector.trim()) return;
            doc.querySelectorAll(selector.trim()).forEach(function(target) {
                if (target.matches('input, select, textarea, button')) controls.add(target);
                target.querySelectorAll('input, select, textarea, button').forEach(function(control) {
                    controls.add(control);
                });
            });
        });
        controls.forEach(function(control) {
            appendControlValue(params, control, names);
        });
        mergeParams(url, params, names);
        return url;
    }

    function backgroundRefreshDelay(autoRefresh) {
        if (!autoRefresh) return 0;
        const trigger = autoRefresh.getAttribute('hx-trigger') || '';
        const match = trigger.match(/(?:^|,)\s*every\s+([\d.]+)\s*(ms|s|m|h)/i);
        if (!match) return 0;
        const factors = { ms: 1, s: 1000, m: 60000, h: 3600000 };
        return Math.max(1000, Number(match[1]) * factors[match[2].toLowerCase()]);
    }

    function updateSidebarCounts(counts) {
        if (!counts) return;
        document.querySelectorAll('.sidebar-count[data-count]').forEach(function(element) {
            const value = counts[element.dataset.count];
            element.hidden = value == null;
            if (value != null) element.textContent = String(value);
        });
    }

    function applyBackgroundTriggers(response, tab) {
        const raw = response.headers.get('HX-Trigger');
        if (!raw) return {};
        try {
            const triggers = JSON.parse(raw);
            updateSidebarCounts(triggers.prCounts);
            if (triggers.pageTitle) tab.title = triggers.pageTitle;
            return triggers;
        } catch {
            return {};
        }
    }

    async function refreshTabInBackground(tab, force) {
        if (!tab || tab.refreshing || (!force && tab === activeTab)) return;
        tab.refreshing = true;
        const needsInitialContent = !tab.html;
        if (needsInitialContent) {
            tab.loading = true;
            renderTabs();
        }
        const doc = tabDocument(tab);
        const autoRefresh = doc.getElementById('auto-refresh-container');
        const url = backgroundRequestUrl(tab, doc, autoRefresh);
        const headers = {
            'HX-Request': 'true',
            'HX-Trigger': 'auto-refresh-container',
            'HX-Target': 'pr-content',
            'HX-Current-URL': url.href
        };
        const renderHash = autoRefresh && autoRefresh.dataset.renderHash;
        if (renderHash) headers['X-PR-Render-Hash'] = renderHash;
        try {
            const response = await fetch(url.href, {
                method: 'GET',
                credentials: 'same-origin',
                headers: headers
            });
            const triggers = applyBackgroundTriggers(response, tab);
            if (response.status === 204) {
                if (triggers.refreshedAt && tab.html && tab !== activeTab) {
                    const cachedDoc = tabDocument(tab);
                    const timestamp = cachedDoc.querySelector('.last-updated[data-updated]');
                    if (timestamp) {
                        timestamp.dataset.updated = triggers.refreshedAt;
                        tab.html = cachedDoc.body.innerHTML;
                    }
                }
                return;
            }
            if (!response.ok) return;
            const html = await response.text();
            if (!html.includes('id="auto-refresh-container"') || tab === activeTab) return;
            tab.html = html;
            persistTabs();
            scheduleBackgroundRefresh(tab);
        } catch {
            // The cached list remains available if GitHub or the connection is unavailable.
        } finally {
            tab.refreshing = false;
            if (needsInitialContent) {
                tab.loading = false;
                renderTabs();
            }
        }
    }

    function scheduleBackgroundRefresh(tab) {
        if (tab.refreshTimer) {
            window.clearInterval(tab.refreshTimer);
            tab.refreshTimer = null;
        }
        const doc = tabDocument(tab);
        const delay = backgroundRefreshDelay(doc.getElementById('auto-refresh-container'));
        if (delay) {
            tab.refreshTimer = window.setInterval(function() {
                refreshTabInBackground(tab, false);
            }, delay);
        }
    }

    function stopBackgroundRefresh(tab) {
        if (!tab || !tab.refreshTimer) return;
        window.clearInterval(tab.refreshTimer);
        tab.refreshTimer = null;
    }

    function renderTabs() {
        const list = document.getElementById('prdash-tabs');
        const bar = document.getElementById('prdash-tabs-bar');
        if (!list || !bar) return;
        bar.hidden = tabs.length < 2;
        list.innerHTML = '';
        tabs.forEach(function(tab) {
            const row = document.createElement('li');
            row.className = 'prdash-tab-row' + (tab === activeTab ? ' active' : '');
            const select = document.createElement('button');
            select.type = 'button';
            select.className = 'prdash-tab' + (tab === activeTab ? ' active' : '');
            select.dataset.prdashTab = tab.id;
            select.dataset.tabShort = tabShortLabels[tab.id];
            select.setAttribute('aria-current', tab === activeTab ? 'page' : 'false');
            select.title = tab.label + ' - ' + tab.url;
            const loading = tab.pendingRender || tab.loading;
            select.setAttribute('aria-label', loading ? 'Loading ' + tab.label : tab.label);
            const indicator = document.createElement('span');
            indicator.className = 'prdash-tab-loading';
            indicator.setAttribute('aria-hidden', 'true');
            indicator.hidden = !loading;
            const label = document.createElement('span');
            label.className = 'prdash-tab-label';
            label.textContent = tab.label;
            select.appendChild(indicator);
            select.appendChild(label);
            const close = document.createElement('button');
            close.type = 'button';
            close.className = 'prdash-tab-close';
            close.dataset.closePrdashTab = tab.id;
            close.setAttribute('aria-label', 'Close ' + tab.label + ' tab');
            close.title = 'Close tab';
            close.textContent = '\u00d7';
            row.appendChild(select);
            row.appendChild(close);
            list.appendChild(row);
        });
    }

    function updateSidebarSelection() {
        const layout = dashboard();
        if (!layout || !activeTab) return;
        const currentTab = activeTab.currentTab || defaultTabForGroup(activeTab.id);
        layout.dataset.currentTab = currentTab;
        layout.dataset.currentRepo = activeTab.currentRepo || '';
        layout.dataset.reviewTab = activeTab.reviewTab || 'pending';
        const activeGroup = groupForTab(currentTab);
        document.querySelectorAll('.sidebar-link[data-tab]').forEach(function(link) {
            link.classList.toggle('active', link.dataset.tab === activeGroup);
        });
        document.querySelectorAll('.repo-link[data-repo]').forEach(function(link) {
            link.classList.toggle('active', link.dataset.repo === (activeTab.currentRepo || ''));
        });
    }

    function captureActive() {
        saveTab(activeTab);
    }

    function abortRequests() {
        inFlightRequests.forEach(function(source) {
            if (window.htmx) window.htmx.trigger(source, 'htmx:abort');
            if (syntheticSources.has(source)) source.remove();
        });
        inFlightRequests.clear();
    }

    function updateHistory(tab, url) {
        const state = Object.assign({}, window.history.state || {}, { prdashTabId: tab.id });
        if (currentUrl() === url) {
            window.history.replaceState(state, '', url);
        } else {
            delete state.htmx;
            window.history.pushState(state, '', url);
        }
    }

    function requestTab(tab, url) {
        const previousTab = activeTab;
        captureActive();
        abortRequests();
        if (previousTab && previousTab !== tab) scheduleBackgroundRefresh(previousTab);
        const source = document.createElement('a');
        source.setAttribute('hx-push-url', 'false');
        source.href = url;
        source.prdashTab = tab;
        source.hidden = true;
        document.body.appendChild(source);
        syntheticSources.add(source);
        tab.url = url;
        tab.html = null;
        tab.pendingRender = true;
        tab.currentTab = tabForUrl(tab.id, url);
        tab.currentRepo = repoForUrl(tab.id, url);
        tab.reviewTab = 'pending';
        activeTab = tab;
        setSearchState(tab.search);
        updateSidebarSelection();
        renderTabs();
        updateHistory(tab, url);
        persistTabs();
        if (window.htmx) {
            window.htmx.ajax('GET', url, {
                source: source,
                target: '#pr-content',
                swap: 'innerHTML'
            });
        } else {
            window.location.href = url;
        }
    }

    function activateTab(tab) {
        if (tab === activeTab) return;
        const previousTab = activeTab;
        captureActive();
        abortRequests();
        if (previousTab) scheduleBackgroundRefresh(previousTab);
        if (tab.html === null || tab.html === undefined) {
            requestTab(tab, tab.url);
            return;
        }
        activeTab = tab;
        updateSidebarSelection();
        updateHistory(tab, tab.url);
        renderTabs();
        const list = content();
        setSearchState(tab.search);
        document.title = tab.title || (tabLabels[tab.id] + ' - PR Dashboard');
        restoreContent(list, tab.html);
        if (tab.scrollTop !== undefined) {
            const main = document.querySelector('.main-content');
            if (main) main.scrollTop = tab.scrollTop;
        }
        saveTab(tab);
        persistTabs();
    }

    function urlForSidebarLink(link) {
        const layout = dashboard();
        const group = link.dataset.tab;
        const currentGroup = groupForTab(layout.dataset.currentTab || 'my_prs');
        const repo = layout.dataset.currentRepo;
        if (!repo) return link.dataset.urlAll || link.href;
        if (group === currentGroup) return link.dataset.urlAll || link.href;
        const repoLink = Array.from(document.querySelectorAll('.repo-link[data-repo]'))
            .find(function(item) { return item.dataset.repo === repo; });
        const repoUrl = repoLink && repoLink.getAttribute('data-url-' + group);
        return repoUrl || link.dataset.urlAll || link.href;
    }

    function openSidebarTab(link) {
        const id = link.dataset.tab;
        const existing = tabs.find(function(tab) { return tab.id === id; });
        if (existing) {
            if (existing === activeTab) {
                const url = urlForSidebarLink(link);
                if (url !== currentUrl() || activeTab.currentTab !== defaultTabForGroup(id)) {
                    requestTab(existing, url);
                }
                return;
            }
            activateTab(existing);
            return;
        }
        const tab = {
            id: id,
            label: tabLabels[id],
            url: urlForSidebarLink(link),
            html: null,
            currentTab: defaultTabForGroup(id),
            currentRepo: '',
            reviewTab: 'pending',
            search: null,
            scrollTop: 0
        };
        tabs.push(tab);
        renderTabs();
        requestTab(tab, tab.url);
    }

    function openMergedSwitch(link) {
        const url = new URL(link.getAttribute('href'), window.location.href);
        const toolbar = document.querySelector('.filter-toolbar');
        if (toolbar) {
            const params = new URLSearchParams();
            const names = new Set();
            toolbar.querySelectorAll('input, select, textarea').forEach(function(control) {
                appendControlValue(params, control, names);
            });
            mergeParams(url, params, names);
        }
        const href = url.pathname + url.search;
        const id = groupForUrl(href);
        const existing = tabs.find(function(tab) { return tab.id === id; });
        if (existing === activeTab) return;
        if (existing && existing.currentRepo === repoForUrl(id, href)) {
            activateTab(existing);
            return;
        }
        const tab = existing || {
            id: id,
            label: tabLabels[id],
            url: href,
            html: null,
            currentTab: defaultTabForGroup(id),
            currentRepo: '',
            reviewTab: 'pending',
            search: null,
            scrollTop: 0
        };
        if (!existing) {
            tabs.push(tab);
            renderTabs();
        }
        requestTab(tab, href);
    }

    function closeTab(id) {
        const index = tabs.findIndex(function(tab) { return tab.id === id; });
        if (index === -1) return;
        stopBackgroundRefresh(tabs[index]);
        const wasActive = tabs[index] === activeTab;
        if (wasActive) {
            captureActive();
            abortRequests();
        }
        tabs.splice(index, 1);
        if (!tabs.length) {
            tabs.push({
                id: 'my_prs',
                label: tabLabels.my_prs,
                url: '/prs/',
                html: null,
                currentTab: 'my_prs',
                currentRepo: '',
                reviewTab: 'pending',
                search: null,
                scrollTop: 0
            });
        }
        if (wasActive) {
            activeTab = null;
            activateTab(tabs[Math.min(index, tabs.length - 1)]);
        } else {
            renderTabs();
            persistTabs();
        }
    }

    function initialize() {
        const layout = dashboard();
        const list = content();
        if (!layout || !list || !ensureTabBar()) return;
        const saved = readTabs();
        const currentGroup = tabIdForCurrent(layout.dataset.currentTab || 'my_prs');
        const url = currentUrl();
        tabs = saved.map(function(tab) {
            if (tab.id === 'my_prs' && groupForUrl(tab.url) === 'my_prs_merged') tab.id = 'my_prs_merged';
            return Object.assign(tab, { label: tabLabels[tab.id], html: null });
        }).filter(function(tab, index, all) {
            return all.findIndex(function(other) { return other.id === tab.id; }) === index;
        });
        activeTab = tabs.find(function(tab) { return tab.id === currentGroup; });
        if (!activeTab) {
            activeTab = {
                id: currentGroup,
                label: tabLabels[currentGroup],
                url: url,
                currentTab: layout.dataset.currentTab || defaultTabForGroup(currentGroup),
                currentRepo: layout.dataset.currentRepo || '',
                reviewTab: layout.dataset.reviewTab || 'pending',
                search: null,
                scrollTop: 0
            };
            tabs.push(activeTab);
        }
        activeTab.url = url;
        activeTab.html = contentHtml(list);
        activeTab.currentTab = layout.dataset.currentTab || activeTab.currentTab || defaultTabForGroup(currentGroup);
        activeTab.currentRepo = layout.dataset.currentRepo || '';
        activeTab.reviewTab = layout.dataset.reviewTab || activeTab.reviewTab || 'pending';
        activeTab.title = document.title;
        renderTabs();
        updateSidebarSelection();
        if (activeTab.search) setSearchState(activeTab.search);
        const state = Object.assign({}, window.history.state || {}, { prdashTabId: activeTab.id });
        window.history.replaceState(state, '', url);
        persistTabs();
        tabs.forEach(function(tab) {
            if (tab === activeTab) return;
            if (tab.html) scheduleBackgroundRefresh(tab);
            else refreshTabInBackground(tab, true);
        });
    }

    document.addEventListener('click', function(event) {
        const closeButton = event.target.closest('[data-close-prdash-tab]');
        if (closeButton) {
            event.preventDefault();
            event.stopPropagation();
            closeTab(closeButton.dataset.closePrdashTab);
            return;
        }
        const prdashTab = event.target.closest('[data-prdash-tab]');
        if (prdashTab) {
            event.preventDefault();
            activateTab(tabs.find(function(tab) { return tab.id === prdashTab.dataset.prdashTab; }));
            return;
        }
        const switchLink = event.target.closest('#pr-content .tab-navigation a.tab');
        if (switchLink && activeTab && (activeTab.id === 'my_prs' || activeTab.id === 'my_prs_merged') && event.button === 0 &&
                !event.metaKey && !event.ctrlKey && !event.shiftKey && !event.altKey) {
            event.preventDefault();
            event.stopPropagation();
            openMergedSwitch(switchLink);
            return;
        }
        const sidebarLink = event.target.closest('.sidebar-link[data-tab]');
        if (!sidebarLink || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
        event.preventDefault();
        openSidebarTab(sidebarLink);
    }, true);

    document.addEventListener('htmx:beforeRequest', function(event) {
        if (event.detail.target && event.detail.target.id === 'pr-content') {
            inFlightRequests.forEach(function(source) {
                if (source !== event.detail.elt && window.htmx) {
                    window.htmx.trigger(source, 'htmx:abort');
                }
            });
            inFlightRequests.clear();
            inFlightRequests.add(event.detail.elt);
        }
    });

    document.addEventListener('htmx:afterRequest', function(event) {
        inFlightRequests.delete(event.detail.elt);
        if (syntheticSources.has(event.detail.elt)) event.detail.elt.remove();
        const tab = event.detail.elt.prdashTab;
        if (tab && tab.pendingRender) {
            tab.pendingRender = false;
            renderTabs();
            persistTabs();
        }
    });

    document.addEventListener('htmx:beforeHistoryUpdate', captureActive);

    document.addEventListener('htmx:pushedIntoHistory', function(event) {
        if (!activeTab) return;
        activeTab.url = event.detail.path;
        const state = Object.assign({}, window.history.state || {}, { prdashTabId: activeTab.id });
        window.history.replaceState(state, '', event.detail.path);
        persistTabs();
    });

    document.addEventListener('htmx:afterSwap', function(event) {
        if (event.detail.target && event.detail.target.id === 'pr-content') {
            if (activeTab) activeTab.pendingRender = false;
            renderTabs();
            saveTab(activeTab);
        }
    });

    document.addEventListener('pageTitle', function(event) {
        if (activeTab) activeTab.title = event.detail.value || event.detail;
    });

    document.addEventListener('htmx:historyRestore', function() {
        const id = window.history.state && window.history.state.prdashTabId;
        let tab = tabs.find(function(item) { return item.id === id; });
        if (!tab) {
            const group = tabLabels[id] ? id : groupForUrl(currentUrl());
            tab = tabs.find(function(item) { return item.id === group; });
        }
        if (!tab) {
            const group = groupForUrl(currentUrl());
            tab = {
                id: group,
                label: tabLabels[group],
                url: currentUrl(),
                html: null,
                currentTab: defaultTabForGroup(group),
                currentRepo: '',
                reviewTab: 'pending',
                search: null,
                scrollTop: 0
            };
            tabs.push(tab);
        }
        if (activeTab && activeTab !== tab) scheduleBackgroundRefresh(activeTab);
        activeTab = tab;
        const layout = dashboard();
        tab.url = currentUrl();
        tab.html = contentHtml(content());
        tab.currentTab = layout.dataset.currentTab || defaultTabForGroup(tab.id);
        tab.currentRepo = layout.dataset.currentRepo || '';
        tab.reviewTab = layout.dataset.reviewTab || 'pending';
        tab.title = document.title;
        tab.search = getSearchState();
        updateSidebarSelection();
        renderTabs();
        persistTabs();
    });

    window.addEventListener('popstate', function(event) {
        const state = event.state || {};
        if (state.htmx) return;
        let tab = tabs.find(function(item) { return item.id === state.prdashTabId; });
        if (!tab) {
            const group = tabLabels[state.prdashTabId] ? state.prdashTabId : groupForUrl(currentUrl());
            tab = tabs.find(function(item) { return item.id === group; });
        }
        if (!tab) {
            const group = groupForUrl(currentUrl());
            tab = {
                id: group,
                label: tabLabels[group],
                url: currentUrl(),
                html: null,
                currentTab: defaultTabForGroup(group),
                currentRepo: '',
                reviewTab: 'pending',
                search: null,
                scrollTop: 0
            };
            tabs.push(tab);
            renderTabs();
        }
        tab.url = currentUrl();
        activateFromHistory(tab);
    });

    function activateFromHistory(tab) {
        const previousTab = activeTab;
        abortRequests();
        if (previousTab && previousTab !== tab) scheduleBackgroundRefresh(previousTab);
        if (tab.html === null || tab.html === undefined) {
            tab.url = currentUrl();
            requestTab(tab, tab.url);
            return;
        }
        activeTab = tab;
        updateSidebarSelection();
        setSearchState(tab.search);
        document.title = tab.title || (tabLabels[tab.id] + ' - PR Dashboard');
        const list = content();
        restoreContent(list, tab.html);
        const main = document.querySelector('.main-content');
        if (main) main.scrollTop = tab.scrollTop || 0;
        renderTabs();
        persistTabs();
    }

    document.addEventListener('prdash:pullRequestSearchChanged', function() {
        if (activeTab) {
            activeTab.search = getSearchState();
            persistTabs();
        }
    });

    document.addEventListener('DOMContentLoaded', initialize);
})();
