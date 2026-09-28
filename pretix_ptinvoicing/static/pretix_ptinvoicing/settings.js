// Provider-agnostic settings page behaviour:
//  1. show only the selected provider's fieldset;
//  2. ask the lookups endpoint for live dropdown options for that provider's fields,
//     using whatever is currently typed in (not necessarily saved yet).
// Must stay a real static file: the Control panel's nonce-based CSP blocks inline scripts.
document.addEventListener("DOMContentLoaded", function () {
    var select = document.getElementById("id_ptinvoicing_provider");
    var fieldsets = Array.prototype.slice.call(
        document.querySelectorAll(".ptinvoicing-provider")
    );
    if (!select || !fieldsets.length) return;

    var lookupsUrl = window.location.pathname.replace(/\/?$/, "/") + "lookups/";
    var csrfToken = document.querySelector("input[name=csrfmiddlewaretoken]").value;

    // Fields the lookup itself filled: changing one must not trigger another lookup.
    var lookupFields = new Set();

    function activeFieldset() {
        return fieldsets.filter(function (fs) {
            return fs.dataset.provider === select.value;
        })[0];
    }

    function setStatus(fieldset, text, isError) {
        var node = fieldset.querySelector(".ptinvoicing-lookup-status");
        node.textContent = text;
        node.classList.toggle("text-danger", !!isError);
    }

    function toSelect(input) {
        if (input.tagName === "SELECT") return input;
        var replacement = document.createElement("select");
        replacement.name = input.name;
        replacement.id = input.id;
        replacement.className = input.className;
        input.replaceWith(replacement);
        return replacement;
    }

    function populate(input, items) {
        // Keep the already-saved value selectable even if it's inactive or missing from
        // this fetch, so re-rendering the dropdown never silently changes a saved setting.
        var currentValue = input.value;
        var field = toSelect(input);
        field.innerHTML = "";

        var matched = false;
        items.forEach(function (item) {
            var option = document.createElement("option");
            option.value = item.id;
            option.textContent = item.label;
            if (currentValue && String(item.id) === String(currentValue)) {
                option.selected = true;
                matched = true;
            }
            field.appendChild(option);
        });

        if (currentValue && !matched) {
            var option = document.createElement("option");
            option.value = currentValue;
            option.textContent = currentValue + " (current)";
            option.selected = true;
            field.prepend(option);
        }
    }

    function loadOptions() {
        var fieldset = activeFieldset();
        if (!fieldset) return;

        var prefix = fieldset.dataset.provider + "-";
        var body = new URLSearchParams();
        body.set("provider", fieldset.dataset.provider);
        fieldset.querySelectorAll("input, select").forEach(function (input) {
            if (!input.name || input.name.indexOf(prefix) !== 0) return;
            var name = input.name.slice(prefix.length);
            if (input.type === "checkbox") {
                body.set(name, input.checked ? "true" : "false");
            } else if (input.value.trim()) {
                body.set(name, input.value.trim());
            }
        });

        setStatus(fieldset, "Loading options…", false);

        fetch(lookupsUrl, {
            method: "POST",
            headers: {
                "X-CSRFToken": csrfToken,
                "Content-Type": "application/x-www-form-urlencoded",
            },
            body: body.toString(),
        })
            .then(function (r) {
                return r.json().then(function (data) {
                    return { ok: r.ok, data: data };
                });
            })
            .then(function (result) {
                if (!result.ok || result.data.error) {
                    setStatus(
                        fieldset,
                        "Could not load options: " + (result.data.error || "unknown error"),
                        true
                    );
                    return;
                }
                Object.keys(result.data.fields || {}).forEach(function (name) {
                    var input = document.getElementById("id_" + prefix + name);
                    if (!input) return;
                    populate(input, result.data.fields[name]);
                    lookupFields.add(input.id);
                });
                setStatus(fieldset, "", false);
            })
            .catch(function () {
                setStatus(fieldset, "Could not reach the server to load options.", true);
            });
    }

    function showActive() {
        fieldsets.forEach(function (fs) {
            fs.hidden = fs.dataset.provider !== select.value;
        });
        loadOptions();
    }

    select.addEventListener("change", showActive);
    // Any credential/config change in the active fieldset may change the options.
    fieldsets.forEach(function (fs) {
        fs.addEventListener("change", function (e) {
            if (e.target !== select && !lookupFields.has(e.target.id)) loadOptions();
        });
    });
    showActive();
});
