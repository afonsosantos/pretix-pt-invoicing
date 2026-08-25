document.addEventListener("DOMContentLoaded", function () {
    var tokenField = document.getElementById("id_factpt-factpt_token");
    if (!tokenField) return;

    var sandboxField = document.getElementById("id_factpt-factpt_sandbox");
    var taxField = document.getElementById("id_factpt-factpt_default_tax_id");
    var statusField = document.getElementById("factpt-lookup-status");
    var lookupsUrl = window.location.pathname.replace(/\/?$/, "/") + "lookups/";
    var csrfToken = document.querySelector("input[name=csrfmiddlewaretoken]").value;

    function setStatus(text, isError) {
        statusField.textContent = text;
        statusField.classList.toggle("text-danger", !!isError);
    }

    function toSelect(input) {
        if (input.tagName === "SELECT") return input;
        var select = document.createElement("select");
        select.name = input.name;
        select.id = input.id;
        select.className = input.className;
        input.replaceWith(select);
        return select;
    }

    function populateTaxes(items, currentValue) {
        var select = toSelect(taxField);
        select.innerHTML = "";

        var matched = false;
        items.forEach(function (item) {
            var option = document.createElement("option");
            option.value = item.id;
            option.textContent = item.label;
            if (currentValue && String(item.id) === String(currentValue)) {
                option.selected = true;
                matched = true;
            }
            select.appendChild(option);
        });

        // Keep the already-saved rate selectable even if it's inactive or missing from this
        // fetch, so re-rendering the dropdown never silently changes a saved setting.
        if (currentValue && !matched) {
            var option = document.createElement("option");
            option.value = currentValue;
            option.textContent = currentValue + " (current)";
            option.selected = true;
            select.prepend(option);
        }

        taxField = select;
    }

    function loadOptions() {
        var token = tokenField.value.trim();
        if (!token) return;

        var currentTax = taxField.value;
        var body = new URLSearchParams();
        body.set("token", token);
        body.set("sandbox", sandboxField && sandboxField.checked ? "true" : "false");

        setStatus("Loading VAT rate options…", false);

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
                        "Could not load VAT rates: " + (result.data.error || "unknown error"),
                        true
                    );
                    return;
                }
                populateTaxes(result.data.taxes, currentTax);
                setStatus("", false);
            })
            .catch(function () {
                setStatus("Could not reach the server to load VAT rates.", true);
            });
    }

    tokenField.addEventListener("blur", loadOptions);
    if (sandboxField) sandboxField.addEventListener("change", loadOptions);
    if (tokenField.value.trim()) loadOptions();
});
