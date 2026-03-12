/**
 * Profile Builder - multi-step form with autocomplete and keyword tags.
 */
(function () {
    "use strict";

    // --- State ---
    let keywords = [];
    let searchTimeout = null;

    // --- DOM refs ---
    const searchInput = document.getElementById("org-search-input");
    const searchResults = document.getElementById("search-results");
    const searchResultsList = document.getElementById("search-results-list");
    const searchLoading = document.getElementById("search-loading");
    const searchEmpty = document.getElementById("search-empty");
    const manualEinInput = document.getElementById("manual-ein-input");
    const manualEinBtn = document.getElementById("manual-ein-btn");

    const keywordTags = document.getElementById("keyword-tags");
    const keywordInput = document.getElementById("keyword-input");
    const keywordAddBtn = document.getElementById("keyword-add-btn");
    const keywordsHidden = document.getElementById("field-keywords");

    const profileForm = document.getElementById("profile-form");

    // --- Step navigation ---
    function showStep(n) {
        document.querySelectorAll(".step-panel").forEach(p => p.style.display = "none");
        document.getElementById("step-" + n).style.display = "block";
        document.querySelectorAll(".step-indicator").forEach((el, i) => {
            el.classList.toggle("active", i < n);
            el.classList.toggle("completed", i < n - 1);
        });
    }

    document.getElementById("back-to-step1").addEventListener("click", () => showStep(1));
    document.getElementById("back-to-step2").addEventListener("click", () => showStep(2));

    document.getElementById("to-step3").addEventListener("click", function () {
        // Sync keywords to hidden field
        syncKeywords();
        // Show confirmation
        document.getElementById("confirm-name").textContent =
            document.getElementById("field-name").value;
        document.getElementById("confirm-location").textContent =
            " | " + document.getElementById("field-city").value +
            ", " + document.getElementById("field-state").value;
        document.getElementById("confirm-keywords").textContent =
            "Keywords: " + keywords.join(", ");
        showStep(3);
    });

    document.getElementById("run-pipeline-btn").addEventListener("click", function () {
        // Set form values from step 3 settings
        document.getElementById("field-depth").value =
            document.getElementById("setting-depth").value;
        document.getElementById("field-limit").value =
            document.getElementById("setting-limit").value;
        syncKeywords();
        profileForm.submit();
    });

    // --- Autocomplete search ---
    searchInput.addEventListener("input", function () {
        const q = this.value.trim();
        clearTimeout(searchTimeout);
        searchResults.style.display = "none";
        searchEmpty.style.display = "none";

        if (q.length < 3) {
            searchLoading.style.display = "none";
            return;
        }

        searchLoading.style.display = "block";
        searchTimeout = setTimeout(() => doSearch(q), 300);
    });

    function doSearch(q) {
        fetch("/api/org-search?q=" + encodeURIComponent(q))
            .then(r => r.json())
            .then(data => {
                searchLoading.style.display = "none";
                if (!data.length) {
                    searchEmpty.style.display = "block";
                    return;
                }
                renderResults(data);
            })
            .catch(() => {
                searchLoading.style.display = "none";
                searchEmpty.style.display = "block";
            });
    }

    function renderResults(orgs) {
        searchResultsList.innerHTML = "";
        orgs.forEach(org => {
            const card = document.createElement("div");
            card.className = "search-result-card p-2 mb-1 border rounded";
            card.style.cursor = "pointer";
            card.innerHTML =
                '<div class="d-flex justify-content-between align-items-center">' +
                '<div><strong>' + escapeHtml(org.name) + '</strong>' +
                '<small class="text-muted ms-2">' + escapeHtml(org.city) + ', ' + escapeHtml(org.state) + '</small></div>' +
                '<div><small class="text-muted">EIN: ' + escapeHtml(org.ein) + '</small>' +
                (org.ntee_code ? ' <span class="badge bg-light text-dark">' + escapeHtml(org.ntee_code) + '</span>' : '') +
                '</div></div>';
            card.addEventListener("click", () => selectOrg(org));
            card.addEventListener("mouseenter", () => card.classList.add("bg-light"));
            card.addEventListener("mouseleave", () => card.classList.remove("bg-light"));
            searchResultsList.appendChild(card);
        });
        searchResults.style.display = "block";
    }

    function selectOrg(org) {
        document.getElementById("field-name").value = org.name;
        document.getElementById("field-ein").value = org.ein;
        document.getElementById("field-city").value = org.city || "";
        document.getElementById("field-state").value = org.state || "";
        document.getElementById("field-ntee").value = org.ntee_code || "";
        document.getElementById("field-search-states").value = org.state || "";
        document.getElementById("field-mission").value = "";

        // Auto-generate keywords from name
        const stopWords = new Set([
            "the", "of", "and", "for", "in", "a", "an", "to", "inc", "co", "org",
            "foundation", "fund", "trust", "association", "society", "corporation",
            "national", "american", "united", "international", "community"
        ]);
        keywords = org.name.toLowerCase().split(/\W+/)
            .filter(w => w.length >= 4 && !stopWords.has(w))
            .slice(0, 8);
        renderKeywords();

        showStep(2);
    }

    // --- Manual EIN lookup ---
    manualEinBtn.addEventListener("click", function () {
        const ein = manualEinInput.value.trim().replace(/-/g, "");
        if (ein.length < 7) return;
        searchLoading.style.display = "block";
        fetch("/api/org-search?q=" + encodeURIComponent(ein))
            .then(r => r.json())
            .then(data => {
                searchLoading.style.display = "none";
                if (data.length) {
                    selectOrg(data[0]);
                } else {
                    // Still let them proceed with just the EIN
                    document.getElementById("field-ein").value = ein;
                    document.getElementById("field-name").value = "";
                    keywords = [];
                    renderKeywords();
                    showStep(2);
                }
            })
            .catch(() => {
                searchLoading.style.display = "none";
            });
    });

    manualEinInput.addEventListener("keydown", function (e) {
        if (e.key === "Enter") {
            e.preventDefault();
            manualEinBtn.click();
        }
    });

    // --- Keyword tag editor ---
    function renderKeywords() {
        keywordTags.innerHTML = "";
        keywords.forEach((kw, i) => {
            const tag = document.createElement("span");
            tag.className = "badge bg-primary d-inline-flex align-items-center";
            tag.innerHTML = escapeHtml(kw) +
                ' <button type="button" class="btn-close btn-close-white ms-1" style="font-size:.6em;" data-idx="' + i + '"></button>';
            tag.querySelector(".btn-close").addEventListener("click", function () {
                keywords.splice(parseInt(this.dataset.idx), 1);
                renderKeywords();
            });
            keywordTags.appendChild(tag);
        });
        syncKeywords();
    }

    function addKeyword() {
        const kw = keywordInput.value.trim().toLowerCase();
        if (kw && !keywords.includes(kw)) {
            keywords.push(kw);
            renderKeywords();
        }
        keywordInput.value = "";
        keywordInput.focus();
    }

    keywordAddBtn.addEventListener("click", addKeyword);
    keywordInput.addEventListener("keydown", function (e) {
        if (e.key === "Enter") {
            e.preventDefault();
            addKeyword();
        }
    });

    function syncKeywords() {
        keywordsHidden.value = keywords.join(",");
    }

    // --- Helpers ---
    function escapeHtml(str) {
        const div = document.createElement("div");
        div.textContent = str || "";
        return div.innerHTML;
    }
})();
