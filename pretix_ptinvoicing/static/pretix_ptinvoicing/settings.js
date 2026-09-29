// Provider-agnostic settings page behaviour:
//  1. show only the selected provider's fieldset;
//  2. ask the lookups endpoint for live dropdown options for that provider's fields,
//     using whatever is currently typed in (not necessarily saved yet).
// Must stay a real static file: the Control panel's nonce-based CSP blocks inline scripts.
document.addEventListener("DOMContentLoaded", function () {
    const select = document.getElementById("id_ptinvoicing_provider");
    const fieldsets = Array.prototype.slice.call(
        document.querySelectorAll(".ptinvoicing-provider")
    );
    if (!select || !fieldsets.length) return;

    const lookupsUrl = window.location.pathname.replace(/\/?$/, "/") + "lookups/";
    const csrfToken = document.querySelector("input[name=csrfmiddlewaretoken]").value;

    // Fields the lookup itself filled: changing one must not trigger another lookup.
    const lookupFields = new Set();

    function activeFieldset() {
        return fieldsets.filter(function (fs) {
            return fs.dataset.provider === select.value;
        })[0];
    }

    function setStatus(fieldset, text, isError) {
        const node = fieldset.querySelector(".ptinvoicing-lookup-status");
        node.textContent = text;
        node.classList.toggle("text-danger", !!isError);
    }

    function toSelect(input) {
        if (input.tagName === "SELECT") return input;
        const replacement = document.createElement("select");
        replacement.name = input.name;
        replacement.id = input.id;
        replacement.className = input.className;
        input.replaceWith(replacement);
        return replacement;
    }

    function populate(input, items) {
        // Keep the already-saved value selectable even if it's inactive or missing from
        // this fetch, so re-rendering the dropdown never silently changes a saved setting.
        const currentValue = input.value;
        const field = toSelect(input);
        field.innerHTML = "";

        var matched = false;
        items.forEach(function (item) {
            const option = document.createElement("option");
            option.value = item.id;
            option.textContent = item.label;
            if (currentValue && String(item.id) === String(currentValue)) {
                option.selected = true;
                matched = true;
            }
            field.appendChild(option);
        });

        if (currentValue && !matched) {
            const option = document.createElement("option");
            option.value = currentValue;
            option.textContent = currentValue + " (current)";
            option.selected = true;
            field.prepend(option);
        }
    }

    function loadOptions() {
        const fieldset = activeFieldset();
        if (!fieldset) return;

        const prefix = fieldset.dataset.provider + "-";
        const body = new URLSearchParams();
        body.set("provider", fieldset.dataset.provider);
        fieldset.querySelectorAll("input, select").forEach(function (input) {
            if (!input.name || input.name.indexOf(prefix) !== 0) return;
            const name = input.name.slice(prefix.length);
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
                    const input = document.getElementById("id_" + prefix + name);
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
        // disabled, not just hidden: a hidden required field still fails native HTML5
        // validation ("not focusable"). <fieldset disabled> excludes it from both.
        fieldsets.forEach(function (fs) {
            const inactive = fs.dataset.provider !== select.value;
            fs.hidden = inactive;
            fs.disabled = inactive;
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
