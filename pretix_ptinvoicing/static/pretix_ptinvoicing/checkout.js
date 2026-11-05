// With "the custom field holds the NIF" on, the custom field is the one NIF field for
// everyone, businesses included, so pretix's other two candidates are hidden — and
// disabled, so nothing typed there is submitted; bare_tin then reads the custom field:
// - internal_reference, which pretix's own pt_PT catalog mislabels "NIF (Número de
//   Contribuinte)" ("Internal reference"), shown to businesses: always.
// - vat_id: for Portuguese addresses only — a foreign EU business still needs it for
//   reverse charge, which the custom field doesn't trigger.
// A class from checkout.css, not style/hidden: pretix's own JS shows these again with an
// inline display, which only an !important stylesheet rule outranks.
//
// With "require the tax number" on as well (data-nif-required on this script's tag), the
// custom field is made mandatory for Portuguese addresses: for businesses always, for
// individuals when pretix requires the invoice address (data-address-required). The
// same rule as orderdata.nif_problem, which holds it when the order is placed. Setting
// `required` and a custom validity is enough to block the form: pretix's own submit
// handler (it sets novalidate and calls checkValidity itself) reports them.

// Only readable while the script itself runs, not from the DOMContentLoaded handler.
var ptinvoicingConfig = document.currentScript ? document.currentScript.dataset : {};

// The Portuguese NIF's check digit (mod 11), as orderdata.is_valid_pt_nif.
function ptinvoicingValidNif(value) {
    var nif = value.replace(/[\s.\-]/g, "").replace(/^PT/i, "");
    if (!/^\d{9}$/.test(nif)) {
        return false;
    }
    var total = 0;
    for (var i = 0; i < 8; i++) {
        total += Number(nif[i]) * (9 - i);
    }
    var check = 11 - (total % 11);
    return (check >= 10 ? 0 : check) === Number(nif[8]);
}

document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll('input[name$="custom_field"]').forEach(function (custom) {
        var prefix = custom.name.slice(0, -"custom_field".length);
        var country = document.querySelector('[name="' + prefix + 'country"]');

        function portuguese() {
            return !country || country.value === "PT";
        }

        function hide(name, when) {
            var input = document.querySelector('input[name="' + prefix + name + '"]');
            var group = input && input.closest(".form-group");
            if (!group) {
                return;
            }
            function toggle() {
                var hidden = when();
                group.classList.toggle("ptinvoicing-hidden", hidden);
                input.disabled = hidden;
            }
            if (country) {
                country.addEventListener("change", toggle);
            }
            toggle();
        }

        hide("internal_reference", function () {
            return true;
        });
        hide("vat_id", portuguese);

        if (ptinvoicingConfig.nifRequired !== "true") {
            return;
        }

        var business = document.querySelectorAll('input[name="' + prefix + 'is_business"]');
        var label = document.querySelector('label[for="' + custom.id + '"]');

        function isBusiness() {
            // Radios, or the single option left when the event only sells to businesses.
            return Array.prototype.some.call(business, function (input) {
                return input.value === "business" && (input.type !== "radio" || input.checked);
            });
        }

        function update() {
            var required =
                portuguese() &&
                (ptinvoicingConfig.addressRequired === "true" || isBusiness());
            custom.required = required;
            // The marker pretix renders beside the label of a required field.
            var marker = label && label.querySelector(".label-required");
            if (label && required && !marker) {
                marker = document.createElement("i");
                marker.className = "label-required";
                marker.textContent = ptinvoicingConfig.textRequired;
                label.appendChild(marker);
            } else if (marker && !required) {
                marker.remove();
            }
            // A number that is there has to be a real one, required or not.
            var invalid =
                portuguese() && custom.value.trim() && !ptinvoicingValidNif(custom.value);
            custom.setCustomValidity(invalid ? ptinvoicingConfig.textInvalid : "");
        }

        if (country) {
            country.addEventListener("change", update);
        }
        business.forEach(function (input) {
            input.addEventListener("change", update);
        });
        custom.addEventListener("input", update);
        update();
    });
});
