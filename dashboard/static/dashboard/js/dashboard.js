/* exported copyBranchName */
function showToast(message, type = 'error') {
    const container = document.getElementById('toast-container');
    const toast = document.createElement('div');
    toast.className = `toast toast-${type}`;
    toast.textContent = message;

    container.appendChild(toast);

    setTimeout(() => toast.classList.add('show'), 10);

    setTimeout(() => {
        toast.classList.remove('show');
        setTimeout(() => toast.remove(), 300);
    }, 5000);

    toast.addEventListener('click', () => {
        toast.classList.remove('show');
        setTimeout(() => toast.remove(), 300);
    });
}

function updatePrReadState(card, action) {
    const form = card.querySelector('.pr-read-state-form');
    if (card.dataset.readPending || !form) return;
    card.dataset.readPending = 'true';
    form.querySelector('[name=action]').value = action;

    const csrfInput = form.querySelector('[name=csrfmiddlewaretoken]');
    const headers = {};
    if (csrfInput) headers['X-CSRFToken'] = csrfInput.value;
    fetch(form.getAttribute('action'), {
        method: 'POST',
        credentials: 'same-origin',
        headers: headers,
        body: new URLSearchParams(new FormData(form)),
        keepalive: true
    }).then(function(response) {
        if (!response.ok) throw new Error('Read state update failed');
    }).then(function() {
        card.classList.toggle('pr-card-unread', action === 'unread');
    }).catch(function() {
        showToast('Could not update pull request read state', 'error');
    }).finally(function() {
        delete card.dataset.readPending;
    });
}

function markPrCardRead(card) {
    if (card && card.classList.contains('pr-card-unread')) {
        updatePrReadState(card, 'read');
    }
}

function repoTabUrl(repoLink, tab, dashboard) {
    const key = tab === 'issues' && dashboard.dataset.currentIssueState === 'closed'
        ? 'issues-closed'
        : tab;
    return repoLink.getAttribute('data-url-' + key);
}

document.addEventListener('click', function(event) {
    const toggle = event.target.closest('.pr-unread-toggle');
    if (toggle) {
        updatePrReadState(toggle.closest('.pr-card'), 'unread');
        return;
    }

    markPrCardRead(event.target.closest('.pr-card'));
});

// Middle-clicking a link fires auxclick instead of click.
document.addEventListener('auxclick', function(event) {
    if (event.button === 1 && event.target.closest('.pr-card a')) {
        markPrCardRead(event.target.closest('.pr-card'));
    }
});

// ---- Instant client-side fuzzy search over PRs on the current page ----
// Fields highlighted (and, per-field, where a matched window is drawn).
const PR_HL_SELECTOR = '.pr-title, .pr-repo, .pr-number, .pr-author, .pr-branch-name, .pr-linked-issues, .pr-label';
const PR_STATUS_FILTERS = [
    'approved', 'has conflicts', 'changes requested', 'not reviewed', 'ci failing',
    'ci pending', 'draft', 'auto-merge enabled', 'queued to merge'
];

// Pill-field controllers for the include/exclude search inputs, set up in
// DOMContentLoaded. A PR is shown when any group matches. Tokens are
// {kind: 'text'|'repo'|'label'|'author'|'status', value} objects. Committed
// field pills match exactly, while text tokens match fuzzily as before.
const prSearchGroups = [];
const PR_MAX_SEARCH_ALTERNATIVES = 10;

// Typo budget scales with token length: short tokens must match exactly.
function prMaxTypos(len) {
    if (len < 4) return 0;
    if (len <= 6) return 1;
    if (len <= 10) return 2;
    return 3;
}

// Sellers approximate-substring DP in O(n) space: cheaply test whether a
// match exists before paying for the full alignment table below.
function prFuzzyBestDistance(text, token) {
    const n = text.length, m = token.length;
    let prev = new Array(n + 1).fill(0);
    for (let i = 1; i <= m; i++) {
        const cur = new Array(n + 1);
        cur[0] = i;
        for (let j = 1; j <= n; j++) {
            const cost = token[i - 1] === text[j - 1] ? 0 : 1;
            let v = prev[j - 1] + cost;
            const up = prev[j] + 1;
            if (up < v) v = up;
            const left = cur[j - 1] + 1;
            if (left < v) v = left;
            cur[j] = v;
        }
        prev = cur;
    }
    let bestD = Infinity;
    for (let j = 1; j <= n; j++) {
        if (prev[j] < bestD) bestD = prev[j];
    }
    return bestD;
}

// Full O(n*m) table + backtrack to recover the alignment, for highlighting.
// Returns {start, end, d, typo} or null; typo[k] is true when
// text[start + k] is a mismatch rather than an exact match.
function prFuzzyWindow(text, token, maxD) {
    const n = text.length, m = token.length;
    if (m === 0) return null;
    const dp = [new Array(n + 1).fill(0)];
    for (let i = 1; i <= m; i++) {
        const prevRow = dp[i - 1];
        const row = new Array(n + 1);
        row[0] = i;
        for (let j = 1; j <= n; j++) {
            const cost = token[i - 1] === text[j - 1] ? 0 : 1;
            let v = prevRow[j - 1] + cost;
            const up = prevRow[j] + 1;
            if (up < v) v = up;
            const left = row[j - 1] + 1;
            if (left < v) v = left;
            row[j] = v;
        }
        dp.push(row);
    }
    let bestJ = -1, bestD = Infinity;
    for (let j = 1; j <= n; j++) {
        if (dp[m][j] <= bestD) { bestD = dp[m][j]; bestJ = j; }
    }
    if (bestD > maxD) return null;

    // Prefer a diagonal step over up/left on ties, matching the forward
    // pass's tie-break, so alignment favors exact matches.
    const typo = [];
    let i = m, j = bestJ;
    while (i > 0) {
        if (j > 0) {
            const cost = token[i - 1] === text[j - 1] ? 0 : 1;
            if (dp[i][j] === dp[i - 1][j - 1] + cost) {
                typo.push(cost === 1);
                i--; j--;
                continue;
            }
        }
        if (dp[i][j] === dp[i - 1][j] + 1) {
            i--;
            continue;
        }
        typo.push(true);
        j--;
    }
    typo.reverse();
    return { start: j, end: bestJ, d: bestD, typo: typo };
}

function prTokenMatches(token, haystack) {
    if (haystack.indexOf(token) !== -1) return true;
    const maxD = prMaxTypos(token.length);
    return maxD > 0 && prFuzzyBestDistance(haystack, token) <= maxD;
}

// A pill token must match its dedicated field exactly; a free-typed 'text'
// token keeps the original fuzzy substring match. Exact
// matching for pills avoids a multi-word label like "good first issue" being
// split into unrelated words by the fuzzy matcher.
function prTokenMatchesCard(token, card, haystack) {
    if (token.kind === 'repo') {
        return (card.dataset.repo || '').toLowerCase() === token.value;
    }
    if (token.kind === 'label') {
        const labels = (card.dataset.labels || '').toLowerCase().split('|').filter(Boolean);
        return labels.indexOf(token.value) !== -1;
    }
    if (token.kind === 'author') {
        return (card.dataset.author || '').toLowerCase() === token.value;
    }
    if (token.kind === 'status') {
        return (card.dataset.status || '').split('|').includes(token.value);
    }
    return prTokenMatches(token.value, haystack);
}

function prCardMatches(tokens, card, haystack, excludeTokens) {
    // Multi-token AND: each token must match somewhere on the card.
    for (const token of tokens) {
        if (!prTokenMatchesCard(token, card, haystack)) return false;
    }
    // Any exclude token matching rules the card out.
    for (const token of excludeTokens) {
        if (prTokenMatchesCard(token, card, haystack)) return false;
    }
    return true;
}

// Committed pills plus the still-being-typed free-text words, as tokens for
// matching (lowercased) and display parts for the "no results" message.
function prFieldTokens(field) {
    if (!field) return { tokens: [], displayParts: [] };
    const pills = prFieldPills(field);
    const tokens = pills.map(p => ({ kind: p.kind, value: p.value.toLowerCase() }));
    const displayParts = pills.map(p => p.value);
    const freeText = prFieldText(field).trim();
    if (freeText) {
        displayParts.push(freeText);
        freeText.toLowerCase().split(/\s+/).filter(Boolean).forEach(word => {
            tokens.push({ kind: 'text', value: word });
        });
    }
    return { tokens, displayParts };
}

function prClearHighlights(root) {
    const parents = new Set();
    root.querySelectorAll('mark.pr-hl, mark.pr-hl-typo').forEach(mark => {
        const parent = mark.parentNode;
        parent.replaceChild(document.createTextNode(mark.textContent), mark);
        parents.add(parent);
    });
    parents.forEach(p => p.normalize());
}

// marks: 0 none, 1 typo, 2 exact per char. Exact wins on overlap so an
// unrelated fuzzy match can't downgrade a char another token matched exactly.
function prHighlightTextNode(node, tokens, fuzzyTokens) {
    const text = node.nodeValue;
    if (!text.trim()) return;
    const lower = text.toLowerCase();
    // Indices are computed against `lower`; if lowercasing changes the
    // string length (locale-specific casing), positions no longer map
    // back onto the original text, so skip highlighting this node.
    if (lower.length !== text.length) return;
    const marks = new Array(text.length).fill(0);
    let any = false;
    for (const token of tokens) {
        let from = 0, idx, found = false;
        while ((idx = lower.indexOf(token, from)) !== -1) {
            for (let k = idx; k < idx + token.length; k++) marks[k] = 2;
            any = true;
            from = idx + Math.max(1, token.length);
            found = true;
        }
        // Only fall back to a fuzzy highlight when the token has no exact
        // match anywhere on the card; otherwise the exact field owns it
        // and a fuzzy window here would mark unrelated text.
        if (!found && fuzzyTokens.has(token)) {
            const maxD = prMaxTypos(token.length);
            if (maxD > 0) {
                const w = prFuzzyWindow(lower, token, maxD);
                if (w) {
                    for (let k = 0; k < w.typo.length; k++) {
                        const v = w.typo[k] ? 1 : 2;
                        if (v > marks[w.start + k]) marks[w.start + k] = v;
                    }
                    any = true;
                }
            }
        }
    }
    if (!any) return;
    const frag = document.createDocumentFragment();
    let pos = 0;
    while (pos < text.length) {
        const level = marks[pos];
        let end = pos + 1;
        while (end < text.length && marks[end] === level) end++;
        if (level === 0) {
            frag.appendChild(document.createTextNode(text.slice(pos, end)));
        } else {
            const mark = document.createElement('mark');
            mark.className = level === 2 ? 'pr-hl' : 'pr-hl-typo';
            mark.textContent = text.slice(pos, end);
            // Zero the shared edge so adjacent exact+typo marks of one match don't show a seam.
            if (pos > 0 && marks[pos - 1] !== 0) {
                mark.style.paddingLeft = '0';
                mark.style.borderTopLeftRadius = '0';
                mark.style.borderBottomLeftRadius = '0';
            }
            if (end < text.length && marks[end] !== 0) {
                mark.style.paddingRight = '0';
                mark.style.borderTopRightRadius = '0';
                mark.style.borderBottomRightRadius = '0';
            }
            frag.appendChild(mark);
        }
        pos = end;
    }
    node.parentNode.replaceChild(frag, node);
}

function prHighlightCard(card, tokenValues) {
    // A token gets a fuzzy highlight only if it has no exact substring
    // match anywhere on the card; exact matches are highlighted in place.
    // Repo/label pill values are plain strings that appear verbatim in
    // data-search, so they highlight the same way as free-typed words.
    const haystack = (card.dataset.search || '').toLowerCase();
    const fuzzyTokens = new Set(tokenValues.filter(t => haystack.indexOf(t) === -1));
    card.querySelectorAll(PR_HL_SELECTOR).forEach(el => {
        const textNodes = [];
        const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT, null);
        let n;
        while ((n = walker.nextNode())) textNodes.push(n);
        textNodes.forEach(node => prHighlightTextNode(node, tokenValues, fuzzyTokens));
    });
}

// ---- Autosuggest pills for the search inputs ----
// A suggestion only appears once the word being typed has reached this
// length; below it, there's not enough signal to guess what the user means.
const PR_SUGGEST_MIN_CHARS = 3;
const PR_SUGGEST_MAX_ITEMS = 8;

// A PR has one repo and one author, so once an include pill picks one, no
// other same-kind suggestion can also match; exclude tokens are OR'd, so
// they're unaffected.
const PR_SINGLETON_PILL_KINDS = ['repo', 'author'];

const PR_UNDO_STACK_LIMIT = 100;

function prCollectUniqueCandidates(elements, kind, getName, candidates) {
    const seen = new Set();
    elements.forEach(function(el) {
        const name = getName(el).trim();
        const key = name.toLowerCase();
        if (name && !seen.has(key)) {
            seen.add(key);
            candidates.push({ kind: kind, value: name });
        }
    });
}

// Repo names come from the live sidebar; label and author names come from the
// PR cards currently rendered.
function prSuggestCandidates() {
    const content = document.getElementById('pr-content');
    const candidates = PR_STATUS_FILTERS.map(function(value) {
        return { kind: 'status', value: value };
    });
    prCollectUniqueCandidates(document.querySelectorAll('.repo-link[data-repo]'), 'repo', function(el) {
        return el.dataset.repo;
    }, candidates);
    if (content) {
        prCollectUniqueCandidates(content.querySelectorAll('.pr-label'), 'label', function(el) {
            return el.textContent;
        }, candidates);
        prCollectUniqueCandidates(content.querySelectorAll('.pr-card[data-author]'), 'author', function(card) {
            return card.dataset.author;
        }, candidates);
    }
    return candidates;
}

function prCreatePillElement(field, pill) {
    const el = document.createElement('span');
    el.className = 'pr-search-pill' + (field.isExclude ? ' pr-search-pill-exclude' : '');
    el.contentEditable = 'false';
    el.prPill = pill;
    const typeEl = document.createElement('span');
    typeEl.className = 'pr-search-pill-type';
    typeEl.textContent = pill.kind;
    const valueEl = document.createElement('span');
    valueEl.textContent = pill.value;
    const removeBtn = document.createElement('button');
    removeBtn.type = 'button';
    removeBtn.className = 'pr-search-pill-remove';
    removeBtn.setAttribute('aria-label', 'Remove ' + pill.value);
    removeBtn.textContent = '×';
    removeBtn.addEventListener('click', function(evt) {
        evt.stopPropagation();
        const caret = prRemovePill(field, pill);
        field.inputEl.focus();
        if (caret) prSetCaret(caret.node, caret.offset);
    });
    el.appendChild(typeEl);
    el.appendChild(valueEl);
    el.appendChild(removeBtn);
    return el;
}

function prCreatePill(field, kind, value) {
    const pill = { kind: kind, value: value };
    pill.el = prCreatePillElement(field, pill);
    return pill;
}

// The search field is a contenteditable box: pills are atomic inline
// elements and the free text lives in the text nodes around them. A
// zero-width space keeps a caret position between and after pills.
const PR_ZWSP = '\u200b';

const PR_ZWSP_RE = new RegExp(PR_ZWSP, 'g');

function prStripZwsp(text) {
    return text.replace(PR_ZWSP_RE, '');
}

function prFieldPills(field) {
    const pills = [];
    field.inputEl.childNodes.forEach(function(node) {
        if (node.prPill) pills.push(node.prPill);
    });
    return pills;
}

// Free text of the field, with pills acting as word separators.
function prFieldText(field) {
    let text = '';
    field.inputEl.childNodes.forEach(function(node) {
        text += node.prPill ? ' ' : (node.nodeType === Node.TEXT_NODE ? prStripZwsp(node.textContent) : '');
    });
    return text;
}

// Ordered sequence of text runs and pills, for exact undo and restore.
function prFieldItems(field) {
    const items = [];
    field.inputEl.childNodes.forEach(function(node) {
        if (node.prPill) {
            items.push({ kind: node.prPill.kind, value: node.prPill.value });
        } else if (node.nodeType === Node.TEXT_NODE) {
            const text = prStripZwsp(node.textContent);
            if (!text) return;
            const last = items[items.length - 1];
            if (last && typeof last.text === 'string') last.text += text; else items.push({ text: text });
        }
    });
    return items;
}

function prNormalizeEditor(field) {
    const editor = field.inputEl;
    if (!prFieldPills(field).length && !prStripZwsp(editor.textContent)) editor.replaceChildren();
    Array.from(editor.childNodes).forEach(function(node) {
        if (node.nodeType !== Node.TEXT_NODE && !node.prPill) node.remove();
    });
    Array.from(editor.childNodes).forEach(function(node) {
        const prev = node.previousSibling;
        if (node.prPill && (!prev || prev.nodeType !== Node.TEXT_NODE)) {
            editor.insertBefore(document.createTextNode(PR_ZWSP), node);
        }
    });
    if (editor.lastChild && editor.lastChild.prPill) {
        editor.appendChild(document.createTextNode(PR_ZWSP));
    }
    editor.classList.toggle('is-empty', !editor.firstChild);
}

function prSelectRange(range) {
    const sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(range);
}

function prSetCaret(node, offset) {
    const range = document.createRange();
    range.setStart(node, offset);
    range.collapse(true);
    prSelectRange(range);
}

// Collapsed caret inside one of the field text nodes, or null.
function prCaret(field) {
    const sel = window.getSelection();
    if (!sel.rangeCount || !sel.isCollapsed || !field.inputEl.contains(sel.anchorNode)) return null;
    let node = sel.anchorNode;
    let offset = sel.anchorOffset;
    if (node === field.inputEl) {
        const next = node.childNodes[offset];
        const prev = node.childNodes[offset - 1];
        if (next && next.nodeType === Node.TEXT_NODE) return { node: next, offset: 0 };
        if (prev && prev.nodeType === Node.TEXT_NODE) return { node: prev, offset: prev.length };
        return null;
    }
    return node.nodeType === Node.TEXT_NODE ? { node: node, offset: offset } : null;
}

// The pill directly before (dir < 0) or after (dir > 0) the caret, if no
// visible text sits between them.
function prAdjacentPill(caret, dir) {
    const text = caret.node.textContent;
    const between = dir < 0 ? text.slice(0, caret.offset) : text.slice(caret.offset);
    if (prStripZwsp(between)) return null;
    const sibling = dir < 0 ? caret.node.previousSibling : caret.node.nextSibling;
    return sibling && sibling.prPill ? sibling : null;
}

function prLeadingZwsp(text) {
    let n = 0;
    while (text[n] === PR_ZWSP) n++;
    return n;
}

// Removes the whole pill as one unit rather than character by character,
// merges the text around it, and returns the caret position at the join.
function prRemovePill(field, pill) {
    const el = pill.el;
    if (!el.parentNode) return null;
    const prev = el.previousSibling;
    const next = el.nextSibling;
    el.remove();
    let caret = null;
    if (prev && next && prev.nodeType === Node.TEXT_NODE && next.nodeType === Node.TEXT_NODE) {
        caret = { node: prev, offset: prev.length };
        prev.appendData(next.data);
        next.remove();
    } else if (prev && prev.nodeType === Node.TEXT_NODE) {
        caret = { node: prev, offset: prev.length };
    } else if (next && next.nodeType === Node.TEXT_NODE) {
        caret = { node: next, offset: 0 };
    }
    prNormalizeEditor(field);
    prCommitUndoSnapshot(field);
    applyPrSearch();
    return caret;
}

// Owned undo/redo: native undo only tracks the input's text, not the pills.
function prCommitUndoSnapshot(field) {
    const snapshot = prSearchFieldState(field);
    const top = field.undoStack[field.undoStack.length - 1];
    if (top && JSON.stringify(top) === JSON.stringify(snapshot)) return;
    field.undoStack.push(snapshot);
    if (field.undoStack.length > PR_UNDO_STACK_LIMIT) field.undoStack.shift();
    field.redoStack = [];
}

function prFlushUndoTimer(field) {
    clearTimeout(field.undoTimer);
    field.undoTimer = null;
    prCommitUndoSnapshot(field);
}

function prScheduleUndoSnapshot(field) {
    clearTimeout(field.undoTimer);
    field.undoTimer = setTimeout(function() {
        field.undoTimer = null;
        prCommitUndoSnapshot(field);
    }, 500);
}

function prUndoField(field) {
    prFlushUndoTimer(field);
    if (field.undoStack.length < 2) return;
    field.redoStack.push(field.undoStack.pop());
    prSetSearchFieldState(field, field.undoStack[field.undoStack.length - 1]);
    applyPrSearch();
}

function prRedoField(field) {
    prFlushUndoTimer(field);
    if (!field.redoStack.length) return;
    const next = field.redoStack.pop();
    field.undoStack.push(next);
    prSetSearchFieldState(field, next);
    applyPrSearch();
}

function prSearchFieldState(field) {
    return {
        text: field ? prFieldText(field).trim() : '',
        pills: field ? prFieldPills(field).map(function(pill) {
            return { kind: pill.kind, value: pill.value };
        }) : [],
        items: field ? prFieldItems(field) : []
    };
}

function prValidPillItem(field, item) {
    if (!item || !['repo', 'label', 'author', 'status'].includes(item.kind) ||
            typeof item.value !== 'string' || !item.value.trim() ||
            (item.kind === 'status' && !PR_STATUS_FILTERS.includes(item.value.toLowerCase()))) {
        return null;
    }
    const value = item.value.trim();
    const duplicate = prFieldPills(field).some(function(existing) {
        return existing.kind === item.kind && existing.value.toLowerCase() === value.toLowerCase();
    });
    return duplicate ? null : { kind: item.kind, value: value };
}

function prSetSearchFieldState(field, state) {
    if (!field || !state || typeof state !== 'object') return;
    const editor = field.inputEl;
    editor.replaceChildren();
    // Older states hold just the pills followed by free text.
    let items = Array.isArray(state.items) ? state.items : null;
    if (!items) {
        items = (Array.isArray(state.pills) ? state.pills : []).slice();
        if (typeof state.text === 'string' && state.text) items.push({ text: state.text });
    }
    items.forEach(function(item) {
        if (item && typeof item.text === 'string') {
            if (item.text) editor.appendChild(document.createTextNode(item.text));
            return;
        }
        const valid = prValidPillItem(field, item);
        if (!valid) return;
        editor.appendChild(prCreatePill(field, valid.kind, valid.value).el);
    });
    prNormalizeEditor(field);
    prHideSuggestions(field);
    if (document.activeElement === editor && editor.lastChild) {
        prSetCaret(editor.lastChild, editor.lastChild.length);
    }
}

function prSearchGroupState(group) {
    return {
        include: prSearchFieldState(group.include),
        exclude: prSearchFieldState(group.exclude)
    };
}

function prSetSearchGroupState(group, state) {
    if (!state || typeof state !== 'object') return;
    prSetSearchFieldState(group.include, state.include);
    prSetSearchFieldState(group.exclude, state.exclude);
}

function prBuildSearchField(isExclude) {
    const fieldEl = document.createElement('div');
    fieldEl.className = 'pr-search-field' + (isExclude ? ' pr-search-exclude-field' : '');
    const inputEl = document.createElement('input');
    inputEl.type = 'text';
    inputEl.className = 'input pr-search-input' + (isExclude ? ' pr-search-exclude-input' : '');
    inputEl.placeholder = isExclude ? 'Exclude\u2026' : 'OR search\u2026';
    inputEl.setAttribute('aria-label', isExclude ?
        'Exclude pull requests matching this text (alternative search)' :
        'Alternative search for pull requests on this page');
    inputEl.autocomplete = 'off';
    inputEl.spellcheck = false;
    const suggestEl = document.createElement('div');
    suggestEl.className = 'pr-search-suggest';
    suggestEl.hidden = true;
    fieldEl.appendChild(inputEl);
    fieldEl.appendChild(suggestEl);
    return initPillField(fieldEl, inputEl, suggestEl, isExclude);
}

function prAddSearchGroup() {
    const rowsEl = document.getElementById('pr-search-rows');
    if (!rowsEl || prSearchGroups.length > PR_MAX_SEARCH_ALTERNATIVES) return null;
    const rowEl = document.createElement('div');
    rowEl.className = 'pr-search-row pr-search-row-alt';
    const labelEl = document.createElement('span');
    labelEl.className = 'pr-search-or-label';
    labelEl.textContent = 'OR';
    const group = { rowEl: rowEl, include: prBuildSearchField(false), exclude: prBuildSearchField(true) };
    const removeBtn = document.createElement('button');
    removeBtn.type = 'button';
    removeBtn.className = 'btn-icon pr-search-row-remove';
    removeBtn.title = 'Remove this alternative';
    removeBtn.setAttribute('aria-label', 'Remove this alternative search');
    removeBtn.textContent = '\u00d7';
    removeBtn.addEventListener('click', function() {
        prRemoveSearchGroup(group);
    });
    rowEl.appendChild(labelEl);
    rowEl.appendChild(group.include.fieldEl);
    rowEl.appendChild(group.exclude.fieldEl);
    rowEl.appendChild(removeBtn);
    rowsEl.appendChild(rowEl);
    prSearchGroups.push(group);
    prUpdateAddOrButton();
    return group;
}

function prRemoveSearchGroup(group) {
    const idx = prSearchGroups.indexOf(group);
    if (idx < 1) return;
    prSearchGroups.splice(idx, 1);
    group.rowEl.remove();
    prUpdateAddOrButton();
    applyPrSearch();
}

function prUpdateAddOrButton() {
    const btn = document.getElementById('pr-search-add-or');
    if (btn) btn.disabled = prSearchGroups.length > PR_MAX_SEARCH_ALTERNATIVES;
}

function getPrSearchState() {
    const bar = document.getElementById('pr-search-bar');
    const first = prSearchGroups.length ? prSearchGroupState(prSearchGroups[0]) : {
        include: prSearchFieldState(null),
        exclude: prSearchFieldState(null)
    };
    return {
        open: Boolean(bar && !bar.hidden),
        include: first.include,
        exclude: first.exclude,
        alternatives: prSearchGroups.slice(1).map(prSearchGroupState)
    };
}

function setPrSearchState(state) {
    if (!state || typeof state !== 'object' || !prSearchGroups.length) {
        return false;
    }
    const alternatives = Array.isArray(state.alternatives) ?
        state.alternatives.slice(0, PR_MAX_SEARCH_ALTERNATIVES) : [];
    while (prSearchGroups.length > alternatives.length + 1) {
        prRemoveSearchGroup(prSearchGroups[prSearchGroups.length - 1]);
    }
    while (prSearchGroups.length < alternatives.length + 1) {
        if (!prAddSearchGroup()) break;
    }
    prSetSearchGroupState(prSearchGroups[0], state);
    alternatives.forEach(function(alternative, idx) {
        prSetSearchGroupState(prSearchGroups[idx + 1], alternative);
    });
    togglePrSearch(state.open !== false);
    document.dispatchEvent(new CustomEvent('prdash:pullRequestSearchChanged', {
        detail: getPrSearchState()
    }));
    return true;
}

function prHideSuggestions(field) {
    field.suggestions = [];
    field.activeIndex = -1;
    field.suggestEl.hidden = true;
    field.suggestEl.innerHTML = '';
}

function prRenderSuggestions(field) {
    field.suggestEl.innerHTML = '';
    field.suggestions.forEach(function(candidate, idx) {
        const item = document.createElement('div');
        item.className = 'pr-search-suggest-item' + (idx === field.activeIndex ? ' active' : '');
        const typeEl = document.createElement('span');
        typeEl.className = 'pr-suggest-type';
        typeEl.textContent = candidate.kind;
        const valueEl = document.createElement('span');
        valueEl.textContent = candidate.value;
        item.appendChild(typeEl);
        item.appendChild(valueEl);
        // mousedown (not click) fires before the input would lose focus,
        // and preventDefault keeps focus in the input entirely.
        item.addEventListener('mousedown', function(evt) {
            evt.preventDefault();
            prCommitSuggestion(field, candidate);
        });
        field.suggestEl.appendChild(item);
    });
    field.suggestEl.hidden = false;
}

function prTrailingWord(value) {
    const match = value.match(/(\S+)$/);
    return match ? match[1] : '';
}

// Match distance for ranking suggestions: 0 for an exact substring match,
// otherwise the fuzzy edit distance (reusing the same typo budget as the
// main search), or null if the candidate doesn't match at all.
function prSuggestMatchDistance(wordLower, maxD, candidateLower) {
    if (candidateLower.indexOf(wordLower) !== -1) return 0;
    if (maxD === 0) return null;
    const w = prFuzzyWindow(candidateLower, wordLower, maxD);
    return w ? w.d : null;
}

function prUpdateSuggestions(field) {
    const caret = prCaret(field);
    const word = caret ? prTrailingWord(caret.node.textContent.slice(0, caret.offset)) : '';
    if (word.length < PR_SUGGEST_MIN_CHARS) {
        prHideSuggestions(field);
        return;
    }
    const wordLower = word.toLowerCase();
    const maxD = prMaxTypos(wordLower.length);
    const pills = prFieldPills(field);
    const taken = new Set(pills.map(p => p.kind + ':' + p.value.toLowerCase()));
    const blockedKinds = field.isExclude ? new Set() : new Set(
        PR_SINGLETON_PILL_KINDS.filter(function(kind) {
            return pills.some(function(p) { return p.kind === kind; });
        })
    );
    const matches = prSuggestCandidates()
        .filter(function(c) { return !blockedKinds.has(c.kind); })
        .filter(function(c) { return !taken.has(c.kind + ':' + c.value.toLowerCase()); })
        .map(function(c) {
            const d = prSuggestMatchDistance(wordLower, maxD, c.value.toLowerCase());
            if (d === null) return null;
            return { candidate: c, d: d, caseMatch: c.value.indexOf(word) !== -1 };
        })
        .filter(Boolean)
        .sort(function(a, b) { return a.d - b.d || (b.caseMatch - a.caseMatch); })
        .slice(0, PR_SUGGEST_MAX_ITEMS)
        .map(function(m) { return m.candidate; });
    if (!matches.length) {
        prHideSuggestions(field);
        return;
    }
    field.suggestions = matches;
    field.activeIndex = 0;
    prRenderSuggestions(field);
}

function prCommitSuggestion(field, candidate) {
    let caret = prCaret(field);
    if (!caret) {
        const last = field.inputEl.lastChild;
        const node = last && last.nodeType === Node.TEXT_NODE ? last : field.inputEl.appendChild(document.createTextNode(''));
        caret = { node: node, offset: node.length };
    }
    const node = caret.node;
    const after = node.textContent.slice(caret.offset);
    // Drop the word currently being typed; it's replaced by the pill.
    let text = node.textContent.slice(0, caret.offset).replace(/\S+$/, '');
    const candidatePrefix = candidate.value.slice(0, candidate.value.lastIndexOf(' ')).toLowerCase();
    const trimmedText = text.trimEnd();
    const normalizedText = trimmedText.toLowerCase();
    if (candidate.kind === 'status' && candidatePrefix &&
            (normalizedText === candidatePrefix || normalizedText.endsWith(' ' + candidatePrefix))) {
        text = trimmedText.slice(0, -candidatePrefix.length);
    }
    node.textContent = text.replace(/\s+$/, '');
    const pill = prCreatePill(field, candidate.kind, candidate.value);
    const afterNode = document.createTextNode(after || PR_ZWSP);
    node.after(pill.el, afterNode);
    prNormalizeEditor(field);
    prHideSuggestions(field);
    field.inputEl.focus();
    prSetCaret(afterNode, prLeadingZwsp(afterNode.data));
    prCommitUndoSnapshot(field);
    applyPrSearch();
}

const PR_PILL_KEY_DIRECTION = { Backspace: -1, ArrowLeft: -1, Delete: 1, ArrowRight: 1 };

function prHandleFieldKeydown(field, evt) {
    const suggestOpen = field.suggestions.length > 0 && !field.suggestEl.hidden;
    const key = evt.key.toLowerCase();
    if ((evt.ctrlKey || evt.metaKey) && !evt.altKey && (key === 'z' || key === 'y')) {
        evt.preventDefault();
        if (key === 'y' || evt.shiftKey) {
            prRedoField(field);
        } else {
            prUndoField(field);
        }
        return;
    }
    if (evt.key === 'Escape') {
        evt.preventDefault();
        if (suggestOpen) {
            prHideSuggestions(field);
        } else {
            togglePrSearch(false);
        }
        return;
    }
    if (suggestOpen) {
        if (evt.key === 'ArrowDown') {
            evt.preventDefault();
            field.activeIndex = (field.activeIndex + 1) % field.suggestions.length;
            prRenderSuggestions(field);
            return;
        }
        if (evt.key === 'ArrowUp') {
            evt.preventDefault();
            field.activeIndex = (field.activeIndex - 1 + field.suggestions.length) % field.suggestions.length;
            prRenderSuggestions(field);
            return;
        }
        if ((evt.key === 'Enter' || evt.key === 'Tab') && field.activeIndex >= 0) {
            evt.preventDefault();
            prCommitSuggestion(field, field.suggestions[field.activeIndex]);
            return;
        }
    }
    if (evt.key === 'Enter') {
        evt.preventDefault();
        return;
    }
    // Pills are atomic: Backspace/Delete remove the adjacent one and arrows
    // step over it, so the caret can sit anywhere between pills and text.
    if (evt.shiftKey || evt.ctrlKey || evt.metaKey || evt.altKey) return;
    const dir = PR_PILL_KEY_DIRECTION[evt.key];
    const caret = dir ? prCaret(field) : null;
    const pillEl = caret ? prAdjacentPill(caret, dir) : null;
    if (!pillEl) return;
    evt.preventDefault();
    if (evt.key === 'Backspace' || evt.key === 'Delete') {
        const joined = prRemovePill(field, pillEl.prPill);
        if (joined) prSetCaret(joined.node, joined.offset);
        return;
    }
    prHideSuggestions(field);
    if (dir < 0) {
        prSetCaret(pillEl.previousSibling, pillEl.previousSibling.length);
    } else {
        prSetCaret(pillEl.nextSibling, prLeadingZwsp(pillEl.nextSibling.data));
    }
}

// Turns the plain <input> into the contenteditable box used for the field.
function prCreateEditor(inputEl) {
    const editor = document.createElement('div');
    editor.id = inputEl.id;
    editor.className = inputEl.className + ' pr-search-editor is-empty';
    editor.contentEditable = 'true';
    editor.setAttribute('role', 'textbox');
    editor.setAttribute('aria-label', inputEl.getAttribute('aria-label') || '');
    editor.dataset.placeholder = inputEl.placeholder;
    editor.spellcheck = false;
    inputEl.replaceWith(editor);
    return editor;
}

function prFocusEditor(field, selectAll) {
    const editor = field.inputEl;
    editor.focus();
    const range = document.createRange();
    range.selectNodeContents(editor);
    if (!selectAll || prFieldPills(field).length) range.collapse(false);
    prSelectRange(range);
}

function initPillField(fieldEl, inputEl, suggestEl, isExclude) {
    if (!fieldEl || !inputEl || !suggestEl) return null;
    const field = {
        fieldEl: fieldEl,
        inputEl: prCreateEditor(inputEl),
        suggestEl: suggestEl,
        isExclude: !!isExclude,
        suggestions: [],
        activeIndex: -1,
        undoStack: [],
        redoStack: [],
        undoTimer: null
    };
    const editor = field.inputEl;
    prCommitUndoSnapshot(field);
    editor.addEventListener('input', function() {
        prNormalizeEditor(field);
        prUpdateSuggestions(field);
        prScheduleUndoSnapshot(field);
        applyPrSearch();
    });
    editor.addEventListener('beforeinput', function(evt) {
        if (evt.inputType.startsWith('format') || evt.inputType === 'insertParagraph' ||
                evt.inputType === 'insertLineBreak') {
            evt.preventDefault();
        }
    });
    editor.addEventListener('paste', function(evt) {
        evt.preventDefault();
        const text = (evt.clipboardData.getData('text/plain') || '').replace(/\s+/g, ' ');
        if (text) document.execCommand('insertText', false, text);
    });
    editor.addEventListener('keydown', function(evt) {
        prHandleFieldKeydown(field, evt);
    });
    return field;
}

function applyPrSearch() {
    const bar = document.getElementById('pr-search-bar');
    const input = document.getElementById('pr-search-input');
    const countEl = document.getElementById('pr-search-count');
    const content = document.getElementById('pr-content');
    if (!bar || !input || !content) return;

    const cards = content.querySelectorAll('.pr-card');
    const active = !bar.hidden;
    const groups = active ? prSearchGroups.map(function(group) {
        return { include: prFieldTokens(group.include), exclude: prFieldTokens(group.exclude) };
    }).filter(function(group) {
        return group.include.tokens.length || group.exclude.tokens.length;
    }) : [];

    let existingEmpty = content.querySelector('.pr-search-empty');

    // Always strip previous highlights before recomputing.
    prClearHighlights(content);

    if (!groups.length) {
        cards.forEach(card => { card.hidden = false; });
        if (countEl) countEl.textContent = '';
        if (existingEmpty) existingEmpty.remove();
        return;
    }

    let matches = 0;
    cards.forEach(card => {
        const haystack = (card.dataset.search || '').toLowerCase();
        const matched = groups.filter(function(group) {
            return prCardMatches(group.include.tokens, card, haystack, group.exclude.tokens);
        });
        card.hidden = !matched.length;
        if (matched.length) {
            matches++;
            prHighlightCard(card, matched.flatMap(group => group.include.tokens
                .filter(token => token.kind !== 'status').map(token => token.value)));
        }
    });

    if (countEl) countEl.textContent = matches + ' of ' + cards.length;

    if (matches === 0 && cards.length > 0) {
        if (!existingEmpty) {
            const list = content.querySelector('.pr-list') || content;
            existingEmpty = document.createElement('div');
            existingEmpty.className = 'empty-state pr-search-empty';
            list.appendChild(existingEmpty);
        }
        const queries = groups.map(function(group) {
            const queryParts = [];
            if (group.include.displayParts.length) queryParts.push(group.include.displayParts.join(' '));
            if (group.exclude.displayParts.length) queryParts.push('-' + group.exclude.displayParts.join(' -'));
            return queryParts.join(' ');
        });
        existingEmpty.textContent = 'No PRs match “' + queries.join('” or “') + '”';
    } else if (existingEmpty) {
        existingEmpty.remove();
    }
}

// Reconcile the toggle button's active state (it lives inside the swapped
// #pr-content) and re-apply the active filter to freshly rendered cards.
function syncPrSearchState() {
    const bar = document.getElementById('pr-search-bar');
    const toggle = document.getElementById('pr-search-toggle');
    if (bar && toggle) {
        toggle.classList.toggle('active', !bar.hidden);
        toggle.setAttribute('aria-expanded', String(!bar.hidden));
    }
    applyPrSearch();
}

function togglePrSearch(forceOpen) {
    const bar = document.getElementById('pr-search-bar');
    const input = document.getElementById('pr-search-input');
    const toggle = document.getElementById('pr-search-toggle');
    if (!bar || !input) return;
    const open = forceOpen === true ? true : (forceOpen === false ? false : bar.hidden);
    bar.hidden = !open;
    if (toggle) {
        toggle.classList.toggle('active', open);
        toggle.setAttribute('aria-expanded', String(open));
    }
    if (open) {
        const field = prSearchGroups.length ? prSearchGroups[0].include : null;
        if (field) prFocusEditor(field, true); else input.focus();
    } else {
        prSearchGroups.forEach(function(group) {
            prHideSuggestions(group.include);
            prHideSuggestions(group.exclude);
        });
    }
    applyPrSearch();
}

function copyBranchName(branchName) {
    navigator.clipboard.writeText(branchName).then(() => {
        showToast('Branch name copied!', 'success');
    }).catch(() => {
        showToast('Failed to copy branch name', 'error');
    });
}

const TIMESTAMP_FORMAT = {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
    second: '2-digit',
    timeZoneName: 'short'
};

// Server and client clocks can drift, which would otherwise make the
// "Updated X ago" text freeze at "just now" (client clock behind server's)
// or skip ahead (client clock ahead). Track the offset so elapsed time is
// computed against the client's own clock instead of assuming they match.
let clockOffsetMs = 0;

function syncClockOffset(el) {
    const date = new Date(el.dataset.updated);
    if (isNaN(date)) return;
    clockOffsetMs = date.getTime() - Date.now();
}

function elapsedSeconds(date) {
    return Math.max(0, Math.round((Date.now() + clockOffsetMs - date.getTime()) / 1000));
}

function formatLastUpdated(root = document) {
    root.querySelectorAll('.last-updated[data-updated]').forEach(el => {
        const date = new Date(el.dataset.updated);
        if (isNaN(date)) return;
        const seconds = elapsedSeconds(date);
        let text;
        if (seconds < 10) {
            text = 'just now';
        } else if (seconds < 60) {
            text = `${seconds}s ago`;
        } else if (seconds < 300) {
            const mins = Math.floor(seconds / 60);
            const secs = seconds % 60;
            text = secs > 0 ? `${mins}m ${secs}s ago` : `${mins}m ago`;
        } else if (seconds < 3600) {
            text = `${Math.floor(seconds / 60)}m ago`;
        } else if (seconds < 86400) {
            text = `${Math.floor(seconds / 3600)}h ago`;
        } else {
            text = `${Math.floor(seconds / 86400)}d ago`;
        }
        el.textContent = `Updated ${text}`;
        el.title = `Last updated ${date.toLocaleString(undefined, TIMESTAMP_FORMAT)}`;
    });
}

const plural = (n, unit) => `${n} ${unit}${n !== 1 ? 's' : ''} ago`;

// Mirror of the time_ago filter (dashboard_tags.py), which only renders the
// first paint: a poll finding no changes skips the swap, freezing that text.
function relativeTimeText(seconds) {
    const minutes = seconds / 60;
    const hours = minutes / 60;
    const days = hours / 24;
    if (seconds < 60) return 'just now';
    if (minutes < 60) return plural(Math.floor(minutes), 'minute');
    if (hours < 24) return plural(Math.floor(hours), 'hour');
    if (days < 7) return plural(Math.floor(days), 'day');
    if (days < 30) return plural(Math.floor(days / 7), 'week');
    if (days < 365) return plural(Math.floor(days / 30), 'month');
    return plural(Math.floor(days / 365), 'year');
}

function formatRelativeTimes(root = document) {
    root.querySelectorAll('[data-timestamp][data-time-prefix]').forEach(el => {
        const date = new Date(el.dataset.timestamp);
        if (isNaN(date)) return;
        const text = `${el.dataset.timePrefix} ${relativeTimeText(elapsedSeconds(date))}`;
        if (el.textContent !== text) el.textContent = text;
    });
}

function formatTimestampTitles(root = document) {
    root.querySelectorAll('[data-timestamp]').forEach(el => {
        if (el.title) return;
        const ts = el.dataset.timestamp;
        if (!ts) return;
        const date = new Date(ts);
        if (isNaN(date)) return;
        el.title = date.toLocaleString(undefined, TIMESTAMP_FORMAT);
    });
}

// Fetch-on-demand pages query GitHub only when asked, in a server job polled here.
const PR_FETCH_POLL_MS = 1000;
let prFetchPollingUrl = null;
let prFetchTimer = null;

function prFetchUrl(bar) {
    // The same fields the list request sends, so plugin query hooks pick the same fetch options.
    const params = new URLSearchParams();
    document.querySelectorAll('.filter-toolbar [name]').forEach(field => {
        if (field.disabled || field.value === undefined) return;
        if ((field.type === 'checkbox' || field.type === 'radio') && !field.checked) return;
        params.append(field.name, field.value);
    });
    const query = params.toString();
    return query ? `${bar.dataset.fetchUrl}?${query}` : bar.dataset.fetchUrl;
}

function renderPrFetchStatus(bar, status) {
    const running = status.state === 'running';
    bar.dataset.state = status.state;
    bar.querySelector('.pr-fetch-btn').disabled = running;
    const progress = bar.querySelector('.pr-fetch-progress');
    progress.hidden = !running;
    if (!running) return;
    const determinate = status.total > 0;
    const percent = determinate ? Math.round(100 * status.done / status.total) : 0;
    progress.classList.toggle('indeterminate', !determinate);
    progress.querySelector('.pr-fetch-progress-fill').style.width = determinate ? `${percent}%` : '';
    if (determinate) progress.setAttribute('aria-valuenow', percent);
    else progress.removeAttribute('aria-valuenow');
    const count = determinate ? ` ${status.done}/${status.total}` : '';
    bar.querySelector('.pr-fetch-status').textContent = `${status.phase}${count} (${status.elapsed_seconds}s)`;
}

function finishPrFetch(status) {
    (status.errors || []).forEach(message => showToast(message, 'error'));
    (status.warnings || []).forEach(message => showToast(message, 'warning'));
    htmx.trigger(document.body, 'manualRefresh');
    if (status.state === 'done') {
        document.dispatchEvent(new CustomEvent('prdash:pullRequestsFetched', { detail: status }));
    }
}

function pollPrFetch(bar) {
    const url = prFetchUrl(bar);
    if (prFetchPollingUrl === url) return;
    window.clearTimeout(prFetchTimer);
    prFetchPollingUrl = url;
    function tick() {
        fetch(url, { credentials: 'same-origin' }).then(response => {
            if (!response.ok) throw new Error(`HTTP ${response.status}`);
            return response.json();
        }).then(status => {
            if (prFetchPollingUrl !== url) return;
            const current = document.getElementById('pr-fetch-bar');
            if (!current || prFetchUrl(current) !== url) {
                // The page moved on to another list, which resumes its own fetch if one runs.
                prFetchPollingUrl = null;
                return;
            }
            renderPrFetchStatus(current, status);
            if (status.state === 'running') {
                prFetchTimer = window.setTimeout(tick, PR_FETCH_POLL_MS);
                return;
            }
            prFetchPollingUrl = null;
            finishPrFetch(status);
        }).catch(() => {
            if (prFetchPollingUrl === url) prFetchTimer = window.setTimeout(tick, PR_FETCH_POLL_MS * 3);
        });
    }
    tick();
}

function startPrFetch(bar) {
    const csrfToken = document.querySelector('[name=csrfmiddlewaretoken]');
    bar.querySelector('.pr-fetch-btn').disabled = true;
    fetch(prFetchUrl(bar), {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'X-CSRFToken': csrfToken ? csrfToken.value : '' },
    }).then(response => {
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        return response.json();
    }).then(status => {
        renderPrFetchStatus(bar, status);
        prFetchPollingUrl = null;
        pollPrFetch(bar);
    }).catch(() => {
        bar.querySelector('.pr-fetch-btn').disabled = false;
        showToast('Could not start fetching pull requests', 'error');
    });
}

function bindPrFetchBar() {
    const bar = document.getElementById('pr-fetch-bar');
    if (bar && bar.dataset.state === 'running') pollPrFetch(bar);
}

function sameOriginReferrer() {
    if (!document.referrer) return null;
    const referrer = new URL(document.referrer);
    return referrer.origin === window.location.origin ? referrer : null;
}

function pluginDependenciesAreResolved(pluginInputs, pluginId, seen = new Set()) {
    if (seen.has(pluginId)) return false;
    seen.add(pluginId);

    const pluginInput = pluginInputs.get(pluginId);
    if (!pluginInput) return false;
    const pluginGroup = pluginInput.closest('.form-group');
    return Array.from(pluginGroup.querySelectorAll('.plugin-requirement')).every(function(requirement) {
        const dependencyId = requirement.dataset.requirementId;
        const dependencyInput = pluginInputs.get(dependencyId);
        if (!dependencyInput || !dependencyInput.checked || requirement.dataset.versionProblem) return false;

        if (['disabled', 'permissions not approved'].includes(requirement.dataset.problem)) return true;
        if (!requirement.dataset.problem) return true;
        if (requirement.dataset.problem !== 'failed to load' || requirement.dataset.dependencyFailure !== 'true') {
            return false;
        }

        return pluginDependenciesAreResolved(
            pluginInputs,
            dependencyId,
            new Set(seen)
        );
    });
}

function updatePluginDependencyWarnings(form) {
    if (!form) return;

    const pluginInputs = new Map(Array.from(form.querySelectorAll('.plugin-enable-checkbox'))
        .map(input => [input.value, input]));
    form.querySelectorAll('.plugin-requirement').forEach(function(warning) {
        const pluginInput = warning.closest('.form-group').querySelector('.plugin-enable-checkbox');
        const dependencyInput = pluginInputs.get(warning.dataset.requirementId);
        const pluginName = warning.dataset.pluginName;
        const name = warning.dataset.requirementName;
        const problemDetail = warning.dataset.problemDetail;
        const dependencyFailure = warning.dataset.dependencyFailure === 'true';
        const requiredVersion = warning.dataset.requiredVersion;
        const installedVersion = warning.dataset.installedVersion;
        const blockers = [];
        const dependencyFailureResolved = warning.dataset.problem === 'failed to load'
            && dependencyFailure
            && pluginDependenciesAreResolved(pluginInputs, warning.dataset.requirementId);

        if (!dependencyInput) {
            blockers.push(`Install ${name} to enable ${pluginName}.`);
        } else if (
            warning.dataset.problem === 'permissions not approved'
            && !dependencyInput.checked
        ) {
            blockers.push(`Select ${name} to approve its permissions and enable ${pluginName}.`);
        } else if (!dependencyInput.checked) {
            blockers.push(`Select ${name} to enable ${pluginName}.`);
        } else if (warning.dataset.problem === 'failed to load' && !dependencyFailureResolved) {
            blockers.push(
                `Required plugin ${name} failed to load`
                + (problemDetail ? `: ${problemDetail}` : '.')
            );
        } else if (
            warning.dataset.problem
            && !['disabled', 'permissions not approved', 'failed to load'].includes(warning.dataset.problem)
        ) {
            blockers.push(`Required plugin ${name} is unavailable.`);
        }

        if (warning.dataset.versionProblem === 'version mismatch') {
            blockers.push(
                `Installed version ${installedVersion} does not meet required version ${requiredVersion}.`
            );
        } else if (warning.dataset.versionProblem) {
            blockers.push(`The required version for ${name} is invalid.`);
        }

        warning.querySelector('.plugin-requirement-message').textContent = blockers.join(' ');
        warning.querySelector('.plugin-requirement-version').textContent =
            `Required version: ${requiredVersion || 'any version'}. `
            + `Installed version: ${installedVersion || 'not installed'}.`;
        warning.hidden = !pluginInput.checked || blockers.length === 0;
    });
}

document.addEventListener('DOMContentLoaded', function() {
    formatTimestampTitles();

    document.querySelectorAll('.plugin-settings-form').forEach(updatePluginDependencyWarnings);
    document.addEventListener('change', function(event) {
        if (event.target.matches('.plugin-enable-checkbox')) {
            updatePluginDependencyWarnings(event.target.form);
        }
    });

    document.addEventListener('click', function(event) {
        document.querySelectorAll('.profile-menu[open]').forEach(function(menu) {
            if (!menu.contains(event.target)) menu.open = false;
        });
    });

    document.addEventListener('keydown', function(event) {
        if (event.key !== 'Escape') return;
        const openMenus = document.querySelectorAll('.profile-menu[open]');
        if (!openMenus.length) return;
        openMenus.forEach(function(menu) {
            menu.open = false;
            const trigger = menu.querySelector('summary');
            if (trigger) trigger.focus();
        });
        event.preventDefault();
    });

    // replaceState avoids polluting the back-stack that "Back to Dashboard" walks with history.back().
    const settingsNav = document.querySelector('.settings-nav');
    if (settingsNav) {
        settingsNav.addEventListener('click', function(e) {
            const link = e.target.closest('a[href*="#"]');
            if (!link || link.pathname !== window.location.pathname) return;
            const target = document.getElementById(link.hash.slice(1));
            if (!target) return;
            e.preventDefault();
            target.scrollIntoView({ behavior: 'smooth', block: 'start' });
            history.replaceState(null, '', link.hash);
        });
    }
    const initialLastUpdated = document.querySelector('.last-updated[data-updated]');
    if (initialLastUpdated) syncClockOffset(initialLastUpdated);
    formatLastUpdated();
    formatRelativeTimes();
    bindPrFetchBar();
    setInterval(formatLastUpdated, 15000);
    // Whole-minute resolution, so a coarser tick than the header above.
    setInterval(formatRelativeTimes, 60000);

    const settingsLink = document.getElementById('settings-link');
    if (settingsLink && window.location.pathname === settingsLink.getAttribute('href')) {
        settingsLink.addEventListener('click', function(e) {
            e.preventDefault();
            history.back();
        });
    }

    // Coming from a plugin settings subpage, history.back() would return there instead of to the dashboard.
    const referrer = sameOriginReferrer();
    const backToDashboardLink = document.getElementById('back-to-dashboard-link');
    const backToSettingsLink = document.getElementById('back-to-settings-link');
    [
        [backToDashboardLink, () => !referrer.pathname.startsWith(window.location.pathname)],
        [backToSettingsLink, () => referrer.pathname === backToSettingsLink.pathname],
    ].forEach(function([link, cameFromTarget]) {
        if (!link || !referrer || !cameFromTarget()) return;
        link.addEventListener('click', function(e) {
            e.preventDefault();
            history.back();
        });
    });

    // Theme selection (Appearance settings)
    const html = document.documentElement;
    const themeSelect = document.getElementById('theme-select');

    // Load saved theme or default to light
    function applySavedTheme() {
        const theme = localStorage.getItem('theme') || 'light';
        html.setAttribute('data-theme', theme);
        if (themeSelect) themeSelect.value = theme;
    }
    applySavedTheme();

    if (themeSelect) {
        themeSelect.addEventListener('change', () => {
            localStorage.setItem('theme', themeSelect.value);
            applySavedTheme();
        });
    }

    // The theme can change on the Settings page while this page sits in bfcache or in another tab.
    window.addEventListener('pageshow', function(e) {
        if (e.persisted) applySavedTheme();
    });
    window.addEventListener('storage', function(e) {
        if (e.key === 'theme') applySavedTheme();
    });

    // Sidebar collapse toggle
    const dashboardLayout = document.getElementById('dashboard-layout');
    const sidebarToggle = document.getElementById('sidebar-toggle');

    if (dashboardLayout) {
        if (localStorage.getItem('sidebarCollapsed') === 'true') {
            dashboardLayout.classList.add('sidebar-collapsed');
        }
    }

    if (sidebarToggle && dashboardLayout) {
        sidebarToggle.addEventListener('click', () => {
            const collapsed = dashboardLayout.classList.toggle('sidebar-collapsed');
            localStorage.setItem('sidebarCollapsed', collapsed);
            sidebarToggle.setAttribute('aria-label', collapsed ? 'Expand sidebar' : 'Collapse sidebar');
            sidebarToggle.setAttribute('title', collapsed ? 'Expand sidebar' : 'Collapse sidebar');
        });
    }

    // Position collapsed sidebar tooltips relative to the viewport, since the
    // sidebar's own overflow-y:auto clips any absolutely-positioned overflow.
    document.querySelectorAll('.sidebar-link').forEach(function(link) {
        const label = link.querySelector('.sidebar-label');
        if (!label) {
            return;
        }
        link.addEventListener('mouseenter', () => {
            const rect = link.getBoundingClientRect();
            label.style.left = (rect.right + 12) + 'px';
            label.style.top = (rect.top + rect.height / 2) + 'px';
        });
    });

    // HTMX swap fix: ensure content stays after swap
    document.body.addEventListener('htmx:afterSwap', function(evt) {
        if (evt.detail.target.innerHTML.length === 0 && evt.detail.xhr && evt.detail.xhr.responseText.length > 0) {
            evt.detail.target.innerHTML = evt.detail.xhr.responseText;
        }
        formatTimestampTitles(evt.detail.target);
        const pluginSettingsForm = evt.detail.target.matches
            && evt.detail.target.matches('.plugin-settings-form')
            ? evt.detail.target
            : evt.detail.target.querySelector
                ? evt.detail.target.querySelector('.plugin-settings-form')
                : null;
        updatePluginDependencyWarnings(pluginSettingsForm);
        const swappedLastUpdated = evt.detail.target.querySelector
            ? evt.detail.target.querySelector('.last-updated[data-updated]')
            : null;
        if (swappedLastUpdated && evt.detail.xhr) syncClockOffset(swappedLastUpdated);
        formatLastUpdated(evt.detail.target);
        formatRelativeTimes(evt.detail.target);
        // Only reconcile search state when #pr-content itself was swapped
        // (its cards and toggle button were recreated); unrelated swaps
        // like #repo-list must not trigger a full re-filter/re-highlight.
        const target = evt.detail.target;
        if (target && target.id === 'pr-content') {
            syncPrSearchState();
            bindPrFetchBar();
        }
    });

    document.body.addEventListener('click', function(evt) {
        if (evt.target.closest('#pr-search-toggle')) {
            togglePrSearch();
            return;
        }
        const fetchBar = document.getElementById('pr-fetch-bar');
        if (fetchBar && (evt.target.closest('#pr-refresh-btn') || evt.target.closest('.pr-fetch-btn'))) {
            // Refreshing a fetch-on-demand page is the explicit request to query GitHub.
            if (fetchBar.dataset.state !== 'running') startPrFetch(fetchBar);
            return;
        }
        if (evt.target.closest('#pr-refresh-btn')) {
            htmx.trigger(document.body, 'manualRefresh');
            return;
        }
        const copyButton = evt.target.closest('.btn-copy-branch');
        if (copyButton) copyBranchName(copyButton.dataset.branchName);
    });

    // Browser back/forward restores a cached #pr-content snapshot (with any
    // baked-in highlights/hidden cards) without firing afterSwap, so
    // reconcile the search state here too.
    document.body.addEventListener('htmx:historyRestore', syncPrSearchState);

    // A poll can confirm data is current without a swap (skipped render on
    // a cache hit), so the "Updated X ago" timestamp needs its own signal.
    document.body.addEventListener('refreshedAt', function(evt) {
        const el = document.querySelector('.last-updated');
        if (!el) return;
        el.dataset.updated = evt.detail.value;
        syncClockOffset(el);
        formatLastUpdated();
    });

    document.body.addEventListener('staleData', function(evt) {
        const el = document.getElementById('stale-indicator');
        if (!el) return;
        el.hidden = !(evt.detail.value ?? evt.detail);
    });

    document.body.addEventListener('prCounts', function(evt) {
        const counts = evt.detail || {};
        document.querySelectorAll('.sidebar-count[data-count]').forEach(function(el) {
            const value = counts[el.dataset.count];
            const hidden = value == null;
            if (el.hidden !== hidden) el.hidden = hidden;
            if (!hidden && el.textContent !== String(value)) el.textContent = value;
        });
    });

    const prIncludeField = initPillField(
        document.getElementById('pr-search-field'),
        document.getElementById('pr-search-input'),
        document.getElementById('pr-search-suggest'),
        false
    );
    const prExcludeField = initPillField(
        document.getElementById('pr-search-exclude-field'),
        document.getElementById('pr-search-exclude-input'),
        document.getElementById('pr-search-exclude-suggest'),
        true
    );
    if (prIncludeField && prExcludeField) {
        prSearchGroups.push({ rowEl: null, include: prIncludeField, exclude: prExcludeField });
    }
    const addOrBtn = document.getElementById('pr-search-add-or');
    if (addOrBtn) {
        addOrBtn.addEventListener('click', function() {
            const group = prAddSearchGroup();
            if (group) prFocusEditor(group.include, false);
        });
    }
    window.prdash = window.prdash || {};
    window.prdash.pullRequestSearch = {
        getState: getPrSearchState,
        setState: setPrSearchState
    };
    document.dispatchEvent(new CustomEvent('prdash:pullRequestSearchReady'));
    document.addEventListener('click', function(evt) {
        prSearchGroups.forEach(function(group) {
            [group.include, group.exclude].forEach(function(field) {
                if (!field.fieldEl.contains(evt.target)) prHideSuggestions(field);
            });
        });
    });
    const searchClear = document.getElementById('pr-search-clear');
    if (searchClear) {
        searchClear.addEventListener('click', function() {
            const empty = prSearchFieldState(null);
            setPrSearchState({ open: true, include: empty, exclude: empty, alternatives: [] });
        });
    }
    const searchClose = document.getElementById('pr-search-close');
    if (searchClose) {
        searchClose.addEventListener('click', function() {
            togglePrSearch(false);
        });
    }

    document.body.addEventListener('showErrors', function(evt) {
        const errors = evt.detail.value || evt.detail;
        if (Array.isArray(errors)) {
            errors.forEach(error => showToast(error, 'error'));
        }
    });

    document.body.addEventListener('showWarnings', function(evt) {
        const warnings = evt.detail.value || evt.detail;
        if (Array.isArray(warnings)) {
            warnings.forEach(warning => showToast(warning, 'warning'));
        }
    });

    document.body.addEventListener('tabChanged', function(evt) {
        const activeTab = evt.detail.value || evt.detail;
        const dashboard = document.getElementById('dashboard-layout');
        if (dashboard) {
            dashboard.dataset.currentTab = activeTab;
        }
        if (activeTab === 'issues') {
            const searchBar = document.getElementById('pr-search-bar');
            const searchToggle = document.getElementById('pr-search-toggle');
            if (searchBar) searchBar.hidden = true;
            if (searchToggle) searchToggle.setAttribute('aria-expanded', 'false');
        }
        // Map sub-tabs to their parent sidebar tabs
        let sidebarTab = activeTab;
        if (activeTab === 'merged') {
            sidebarTab = 'my_prs';
        } else if (activeTab === 'author_prs' || activeTab === 'author_merged') {
            sidebarTab = 'author_prs';
        }
        const sidebarLinks = document.querySelectorAll('.sidebar-link[data-tab]');
        sidebarLinks.forEach(link => {
            link.classList.remove('active');
            if (link.dataset.tab === sidebarTab) {
                link.classList.add('active');
            }
        });
    });

    document.body.addEventListener('pageTitle', function(evt) {
        document.title = evt.detail.value || evt.detail;
    });

    document.body.addEventListener('repoChanged', function(evt) {
        const currentRepo = evt.detail.value || evt.detail;
        const dashboard = document.getElementById('dashboard-layout');
        if (dashboard) {
            dashboard.dataset.currentRepo = currentRepo;
        }
        // Update repo link active state
        const repoLinks = document.querySelectorAll('.repo-link[data-repo]');
        repoLinks.forEach(link => {
            link.classList.remove('active');
            if (link.dataset.repo === currentRepo) {
                link.classList.add('active');
            }
        });
    });

    document.body.addEventListener('issueStateChanged', function(evt) {
        const dashboard = document.getElementById('dashboard-layout');
        if (dashboard) {
            dashboard.dataset.currentIssueState = evt.detail.value || evt.detail;
        }
    });

    document.body.addEventListener('repoToggled', function() {
        if (document.getElementById('pr-fetch-bar')) {
            // Only re-render a fetch-on-demand page, fetching stays an explicit request.
            htmx.trigger(document.body, 'manualRefresh');
            return;
        }
        const refreshBtn = document.getElementById('pr-refresh-btn');
        if (refreshBtn) {
            refreshBtn.click();
        }
    });

    document.body.addEventListener('htmx:configRequest', function(evt) {
        const csrfToken = document.querySelector('[name=csrfmiddlewaretoken]');
        if (csrfToken) {
            evt.detail.headers['X-CSRFToken'] = csrfToken.value;
        }

        const elt = evt.detail.elt;
        if (elt.id === 'auto-refresh-container' && elt.dataset.renderHash) {
            evt.detail.headers['X-PR-Render-Hash'] = elt.dataset.renderHash;
        }

        const dashboard = document.getElementById('dashboard-layout');
        if (!dashboard) return;

        // Handle repo link clicks - use current tab's URL
        if (elt.classList.contains('repo-link') && elt.dataset.repo) {
            let currentTab = dashboard.dataset.currentTab || 'my_prs';
            const url = repoTabUrl(elt, currentTab, dashboard);
            if (url) {
                evt.detail.path = url;
            }
        }

        // Handle tab link clicks - use current repo's URL if one is selected
        // BUT if clicking the already active tab, clear the repo filter
        if (elt.classList.contains('sidebar-link') && elt.dataset.tab) {
            const currentRepo = dashboard.dataset.currentRepo;
            const currentTab = dashboard.dataset.currentTab || 'my_prs';
            const clickedTab = elt.dataset.tab;

            // Map current tab to sidebar tab for comparison
            let currentSidebarTab = currentTab;
            if (currentTab === 'merged') currentSidebarTab = 'my_prs';
            else if (currentTab === 'author_merged') currentSidebarTab = 'author_prs';

            // If clicking the same tab we're on, clear repo filter (use "all" URL)
            if (currentRepo && clickedTab === currentSidebarTab) {
                const url = elt.dataset.urlAll;
                if (url) {
                    evt.detail.path = url;
                }
            } else if (currentRepo) {
                // Different tab, keep repo filter
                const repoLink = document.querySelector('.repo-link[data-repo="' + currentRepo + '"]');
                if (repoLink) {
                    const url = repoTabUrl(repoLink, clickedTab, dashboard);
                    if (url) {
                        evt.detail.path = url;
                    }
                }
            }
        }
    });

    // Also update hx-push-url to match the new path
    document.body.addEventListener('htmx:beforeRequest', function(evt) {
        const elt = evt.detail.elt;
        const dashboard = document.getElementById('dashboard-layout');
        if (!dashboard) return;

        if (elt.classList.contains('repo-link') && elt.dataset.repo) {
            let currentTab = dashboard.dataset.currentTab || 'my_prs';
            const url = repoTabUrl(elt, currentTab, dashboard);
            if (url) {
                elt.href = url;
            }
        }

        if (elt.classList.contains('sidebar-link') && elt.dataset.tab) {
            const currentRepo = dashboard.dataset.currentRepo;
            const currentTab = dashboard.dataset.currentTab || 'my_prs';
            const clickedTab = elt.dataset.tab;

            // Map current tab to sidebar tab for comparison
            let currentSidebarTab = currentTab;
            if (currentTab === 'merged') currentSidebarTab = 'my_prs';
            else if (currentTab === 'author_merged') currentSidebarTab = 'author_prs';

            // If clicking the same tab we're on, clear repo filter (use "all" URL)
            if (currentRepo && clickedTab === currentSidebarTab) {
                const url = elt.dataset.urlAll;
                if (url) {
                    elt.href = url;
                }
            } else if (currentRepo) {
                // Different tab, keep repo filter
                const repoLink = document.querySelector('.repo-link[data-repo="' + currentRepo + '"]');
                if (repoLink) {
                    const url = repoTabUrl(repoLink, clickedTab, dashboard);
                    if (url) {
                        elt.href = url;
                    }
                }
            } else {
                // No repo selected, use the "all" URL
                const url = elt.dataset.urlAll;
                if (url) {
                    elt.href = url;
                }
            }
        }
    });

    let isRefreshInProgress = false;

    document.body.addEventListener('htmx:beforeRequest', function(evt) {
        const target = evt.detail.target;
        if (target && target.id === 'pr-content') {
            isRefreshInProgress = true;
        }
    });

    document.body.addEventListener('htmx:afterRequest', function(evt) {
        const target = evt.detail.target;
        if (target && target.id === 'pr-content') {
            isRefreshInProgress = false;
        }
    });

    // Without this, an hx-trigger="load" region stays on its loading skeleton forever on failure.
    function showLoadFailure(evt) {
        const loader = evt.detail.elt;
        if (loader && loader.getAttribute('hx-trigger') === 'load') {
            loader.innerHTML = '<div class="alert alert-error">Failed to load content. Please try refreshing.</div>';
        }
    }
    document.body.addEventListener('htmx:responseError', showLoadFailure);
    document.body.addEventListener('htmx:sendError', showLoadFailure);

    document.addEventListener('keydown', function(evt) {
        if (evt.key === 'Enter' && !evt.isComposing && evt.target.id === 'author-picker-input') {
            evt.preventDefault();
            evt.target.form.requestSubmit();
            return;
        }
        if (evt.key === 'Escape') {
            const bar = document.getElementById('pr-search-bar');
            if (bar && !bar.hidden) {
                evt.preventDefault();
                togglePrSearch(false);
                return;
            }
        }
        // Backtick toggles search open/closed from anywhere except while
        // typing in a field (there the character types normally).
        if (evt.key === '`' && !evt.ctrlKey && !evt.metaKey && !evt.altKey) {
            const inField = evt.target.tagName === 'INPUT' || evt.target.tagName === 'TEXTAREA' || evt.target.tagName === 'SELECT' || evt.target.isContentEditable;
            if (!inField && document.getElementById('pr-search-bar')) {
                evt.preventDefault();
                togglePrSearch();
                return;
            }
        }
        if (evt.target.tagName === 'INPUT' || evt.target.tagName === 'TEXTAREA') {
            return;
        }
        if (evt.ctrlKey || evt.metaKey) {
            return;
        }
        if (evt.key === 'r' || evt.key === 'R') {
            if (isRefreshInProgress) {
                return;
            }
            const refreshBtn = document.getElementById('pr-refresh-btn');
            if (refreshBtn) {
                refreshBtn.click();
            }
        }
    });
});

// Replaces the native title tooltip (fixed browser delay). Title is stashed while shown, then restored.
(function() {
    const SHOW_DELAY_MS = 150;
    const tip = document.createElement('div');
    tip.className = 'app-tooltip';
    tip.setAttribute('role', 'tooltip');
    document.body.appendChild(tip);
    let current = null;
    let stashedTitle = '';
    let timer = null;

    function place(target) {
        const rect = target.getBoundingClientRect();
        const tipRect = tip.getBoundingClientRect();
        let left = rect.left + rect.width / 2 - tipRect.width / 2;
        left = Math.max(4, Math.min(left, window.innerWidth - tipRect.width - 4));
        let top = rect.bottom + 6;
        if (top + tipRect.height > window.innerHeight - 4) {
            top = rect.top - tipRect.height - 6;
        }
        tip.style.left = left + 'px';
        tip.style.top = Math.max(4, top) + 'px';
    }

    function hide() {
        clearTimeout(timer);
        tip.classList.remove('app-tooltip-visible');
        if (current) {
            if (!current.hasAttribute('title')) current.setAttribute('title', stashedTitle);
            current = null;
        }
    }

    function activate(evt) {
        const target = evt.target.closest ? evt.target.closest('[title]') : null;
        if (!target || target === current) return;
        hide();
        stashedTitle = target.getAttribute('title');
        if (!stashedTitle) return;
        target.removeAttribute('title');
        current = target;
        timer = setTimeout(() => {
            tip.textContent = stashedTitle;
            place(target);
            tip.classList.add('app-tooltip-visible');
        }, SHOW_DELAY_MS);
    }

    document.addEventListener('mouseover', activate);
    document.addEventListener('focusin', activate);
    document.addEventListener('mouseout', (evt) => {
        if (current && !current.contains(evt.relatedTarget)) hide();
    });
    document.addEventListener('focusout', hide);
    document.addEventListener('keydown', (evt) => {
        if (evt.key === 'Escape') hide();
    });
    document.addEventListener('scroll', hide, true);
    document.addEventListener('htmx:beforeSwap', hide);
})();
