// Provider-agnostic settings page behaviour:
//  1. show only the selected provider's fieldset;
//  2. ask the lookups endpoint for live dropdown options for that provider's fields,
//     using whatever is currently typed in (not necessarily saved yet).
// Must stay a real static file: the Control panel's nonce-based CSP blocks inline scripts.
document.addEventListener("DOMContentLoaded", function () {
    // Copy buttons (Moloni's redirect URI): data-copy is the text, data-copied the label
    // shown for a moment afterwards.
    document.querySelectorAll(".ptinvoicing-copy").forEach(function (button) {
        button.addEventListener("click", function () {
            const label = button.querySelector(".ptinvoicing-copy-label");
            const original = label.textContent;
            navigator.clipboard.writeText(button.dataset.copy).then(function () {
                label.textContent = button.dataset.copied;
                setTimeout(function () {
                    label.textContent = original;
                }, 2000);
            });
        });
    });

    const radios = document.querySelectorAll("input[name=ptinvoicing_provider]");
    const fieldsets = Array.prototype.slice.call(
        document.querySelectorAll(".ptinvoicing-provider")
    );
    if (!radios.length || !fieldsets.length) return;

    // Translated by the template (data-text-* on the form): a static file can't be.
    const text = document.getElementById("ptinvoicing-settings").dataset;

    function selectedProvider() {
        const checked = document.querySelector("input[name=ptinvoicing_provider]:checked");
        return checked ? checked.value : "";
    }

    // Lookup fields whose value scopes other lookups (Moloni's company): changing one
    // re-runs the lookup instead of being ignored like the other lookup-filled fields.
    function triggers(fieldset) {
        return (fieldset.dataset.lookupTriggers || "").split(" ").filter(Boolean);
    }

    const lookupsUrl = window.location.pathname.replace(/\/?$/, "/") + "lookups/";
    const csrfToken = document.querySelector("input[name=csrfmiddlewaretoken]").value;

    // Fields the lookup itself filled: changing one must not trigger another lookup.
    const lookupFields = new Set();

    function activeFieldset() {
        return fieldsets.filter(function (fs) {
            return fs.dataset.provider === selectedProvider();
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
        // Carry over the password-manager opt-outs (autocomplete, data-*-ignore).
        Array.prototype.forEach.call(input.attributes, function (attr) {
            if (attr.name === "autocomplete" || attr.name.indexOf("data-") === 0) {
                replacement.setAttribute(attr.name, attr.value);
            }
        });
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
            option.textContent = text.textSaved + " " + currentValue;
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

        setStatus(fieldset, text.textLoading, false);

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
                        text.textFailed + " " + (result.data.error || text.textUnknown),
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

                // A trigger the dropdown just filled in by itself (the first company,
                // when none was set yet) fires no change event, so look up once more
                // with it. Stops on its own: the next request sends that same value.
                const changed = triggers(fieldset).some(function (name) {
                    const field = document.getElementById("id_" + prefix + name);
                    return field && field.value && field.value !== (body.get(name) || "");
                });
                if (changed) loadOptions();
            })
            .catch(function () {
                setStatus(fieldset, text.textUnreachable, true);
            });
    }

    function showActive() {
        // disabled, not just hidden: a hidden required field still fails native HTML5
        // validation ("not focusable"). <fieldset disabled> excludes it from both.
        fieldsets.forEach(function (fs) {
            const inactive = fs.dataset.provider !== selectedProvider();
            fs.hidden = inactive;
            fs.disabled = inactive;
        });
        loadOptions();
    }

    radios.forEach(function (radio) {
        radio.addEventListener("change", showActive);
    });
    // Any credential/config change in the active fieldset may change the options.
    fieldsets.forEach(function (fs) {
        fs.addEventListener("change", function (e) {
            const name = e.target.name.slice(fs.dataset.provider.length + 1);
            if (!lookupFields.has(e.target.id) || triggers(fs).indexOf(name) !== -1) {
                loadOptions();
            }
        });
    });
    showActive();
});
