/* Pont portail ↔ iframe configurateur PFF.
 *
 * Reprend le protocole `postMessage` du parent back-office
 * (configurator_action.js), mais côté PORTAIL : au lieu de l'ORM, il appelle la
 * route `/my/configurateur/submit`. Chargé sur les pages frontend (assets_frontend) ;
 * inerte tant qu'aucun message du configurateur n'arrive.
 */
(function () {
    "use strict";

    window.addEventListener("message", function (ev) {
        var msg = ev.data;
        if (!msg || !msg.type) {
            return;
        }

        // Le configurateur est prêt → au portail, pas de routage d'atelier :
        // on renvoie des stations vides (le configurateur retombe sur ses
        // valeurs par défaut). PFF affecte les postes à la révision du devis.
        if (msg.type === "pff_ready") {
            if (ev.source) {
                ev.source.postMessage({ type: "pff_stations", stations: {} }, "*");
            }
            return;
        }

        // L'utilisateur valide sa configuration → on crée le devis brouillon.
        if (msg.type === "pff_valider") {
            var items = msg.items || [];
            fetch("/my/configurateur/submit", {
                method: "POST",
                credentials: "same-origin",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    jsonrpc: "2.0",
                    method: "call",
                    params: { items: items },
                }),
            })
                .then(function (r) {
                    return r.json();
                })
                .then(function (res) {
                    var data = (res && res.result) || {};
                    if (data.redirect) {
                        window.location = data.redirect;
                    } else {
                        alert("Votre soumission a été enregistrée.");
                    }
                })
                .catch(function () {
                    alert(
                        "Une erreur est survenue lors de l'enregistrement de votre soumission."
                    );
                });
        }
    });
})();
