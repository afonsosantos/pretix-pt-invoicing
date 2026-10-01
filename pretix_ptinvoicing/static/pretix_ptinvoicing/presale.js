// Moves the invoice (and credit note) buttons in beside pretix's ticket download buttons.
//
// It has to be done here rather than in the template: the buttons live in pretix's
// fragment_downloads.html, which has no plugin signal inside it, so the nearest hook
// (presale.signals.order_info_top) renders *above* that block. If the block isn't on the
// page — an order with no downloadable tickets — the button stays where it was rendered.
(function () {
    function place() {
        const buttons = document.querySelectorAll(".ptinvoicing-invoice-download");
        if (!buttons.length) return;

        // Land next to the ticket button itself, not merely inside .info-download: that
        // container also holds a help paragraph after the button row, so appending to it
        // would drop the button onto a line of its own below the help text.
        const ticketButton = document.querySelector(
            ".info-download form.download-btn-form:not(.ptinvoicing-invoice-download)"
        );
        const target = ticketButton
            ? ticketButton.parentNode
            : document.querySelector(".info-download");

        if (!target) return;
        buttons.forEach(function (button) {
            if (!target.contains(button)) target.appendChild(button);
        });
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", place);
    } else {
        place();
    }
})();
