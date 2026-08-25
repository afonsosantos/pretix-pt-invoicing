document.addEventListener("DOMContentLoaded", function () {
    var tokenField = document.getElementById("id_factpt-factpt_token");
    if (!tokenField) return;

    var sandboxField = document.getElementById("id_factpt-factpt_sandbox");
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

    function populate(fieldId, items, currentValue) {
        var select = toSelect(document.getElementById(fieldId));
        select.innerHTML = "";
        items.forEach(function (item) {
            var option = document.createElement("option");
            option.value = item.id;
            option.textContent = item.label;
            if (String(item.id) === String(currentValue)) option.selected = true;
            select.appendChild(option);
        });
    }

    function loadOptions() {
        var token = tokenField.value.trim();
        if (!token) return;

        var taxField = document.getElementById("id_factpt-factpt_default_tax_id");
        var unitField = document.getElementById("id_factpt-factpt_default_unit_id");
        var currentTax = taxField.value;
        var currentUnit = unitField.value;
        var body = new URLSearchParams();
        body.set("token", token);
        body.set("sandbox", sandboxField && sandboxField.checked ? "true" : "false");

        setStatus("Loading VAT rate / unit options…", false);

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
                        "Could not load options: " + (result.data.error || "unknown error"),
                        true
                    );
                    return;
                }
                populate("id_factpt-factpt_default_tax_id", result.data.taxes, currentTax);
                populate("id_factpt-factpt_default_unit_id", result.data.units, currentUnit);
                setStatus("", false);
            })
            .catch(function () {
                setStatus("Could not reach the server to load options.", true);
            });
    }

    tokenField.addEventListener("blur", loadOptions);
    if (sandboxField) sandboxField.addEventListener("change", loadOptions);
    if (tokenField.value.trim()) loadOptions();
});
