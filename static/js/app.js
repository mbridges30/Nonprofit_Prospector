/* Foundation Finder - Client JS */

/**
 * Poll a background task for completion.
 * Redirects to results page when done.
 */
function pollTask(taskId) {
    const progressEl = document.getElementById("progress-text");

    function check() {
        fetch("/api/status/" + taskId)
            .then(r => r.json())
            .then(data => {
                if (data.state === "complete") {
                    window.location.href = "/results/" + taskId;
                } else if (data.state === "error") {
                    if (progressEl) {
                        progressEl.textContent = "Error: " + data.error;
                        progressEl.classList.add("text-danger");
                    }
                } else {
                    if (progressEl && data.progress) {
                        progressEl.textContent = data.progress;
                    }
                    setTimeout(check, 2000);
                }
            })
            .catch(() => {
                setTimeout(check, 3000);
            });
    }

    setTimeout(check, 1000);
}

/**
 * EIN lookup form handler (home page).
 */
document.addEventListener("DOMContentLoaded", function () {
    const form = document.getElementById("org-lookup-form");
    if (form) {
        form.addEventListener("submit", function (e) {
            e.preventDefault();
            const ein = document.getElementById("org-ein-input").value.trim().replace(/-/g, "");
            if (ein) {
                window.location.href = "/org/" + ein;
            }
        });
    }
});
