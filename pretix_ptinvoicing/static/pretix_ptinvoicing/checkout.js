// With "the custom field holds the NIF" on, the custom field is the one NIF field for
// everyone, businesses included, so pretix's other two candidates are hidden — and
// disabled, so nothing typed there is submitted; bare_tin then reads the custom field:
// - internal_reference, which pretix's own pt_PT catalog mislabels "NIF (Número de
//   Contribuinte)" ("Internal reference"), shown to businesses: always.
// - vat_id: for Portuguese addresses only — a foreign EU business still needs it for
//   reverse charge, which the custom field doesn't trigger.
// A class from checkout.css, not style/hidden: pretix's own JS shows these again with an
// inline display, which only an !important stylesheet rule outranks.
document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll('input[name$="custom_field"]').forEach(function (custom) {
        var prefix = custom.name.slice(0, -"custom_field".length);
        var country = document.querySelector('[name="' + prefix + 'country"]');

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
        hide("vat_id", function () {
            return !country || country.value === "PT";
        });
    });
});
