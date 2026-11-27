"use strict";

const workspaceMenu = document.querySelector(".workspace-menu");
if (workspaceMenu) {
    const narrow = window.matchMedia("(max-width: 960px)");
    const fitNavigation = () => { workspaceMenu.open = !narrow.matches; };
    fitNavigation();
    narrow.addEventListener("change", fitNavigation);
}

const personaSelect = document.querySelector("#persona-select");
if (personaSelect) {
    personaSelect.addEventListener("change", () => personaSelect.form.requestSubmit());
}

const seriesSelect = document.querySelector("#series-select");
if (seriesSelect) {
    seriesSelect.addEventListener("change", () => seriesSelect.form.requestSubmit());
}

const guide = document.querySelector(".journey-guide");
if (guide) {
    const narrow = window.matchMedia("(max-width: 1100px)");
    guide.querySelector(".journey-copy").open = !narrow.matches;
    const selector = guide.dataset.target;
    const target = selector ? document.querySelector(selector) : null;
    const note = guide.querySelector("[data-highlight-note]");
    if (target) {
        target.classList.add("journey-highlight");
        // Blur sibling branches, never an ancestor of the target or guide.
        for (let branch = target; branch !== document.body; branch = branch.parentElement) {
            for (const sibling of branch.parentElement.children) {
                if (sibling !== branch && !sibling.contains(guide) &&
                    !sibling.matches("script, style, .skip-link")) {
                    sibling.classList.add("journey-background");
                }
            }
        }
        target.scrollIntoView({block: narrow.matches ? "start" : "center", behavior: "instant"});
        if (narrow.matches) {
            window.scrollBy({top: -guide.getBoundingClientRect().height - 24, behavior: "instant"});
        }
        note.textContent = "The outlined area stays sharp; the background is softened. Hover or focus another area to read and use it.";
    } else if (selector) {
        note.textContent = "This area is not available in the current record or role. Read the page's actual state; nothing was created.";
    }
    document.querySelector("#journey-title").focus({preventScroll: true});
    document.addEventListener("keydown", event => {
        if (event.key === "Escape") {
            guide.querySelector(".journey-exit").focus();
        }
    });
}
